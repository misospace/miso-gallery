"""Regression tests for the batched thumbnail cache cleanup helper (issue #249).

The cleanup matcher uses the leading path-identity digest written by
``thumbnails.thumbnail_filename`` (issue #515), so these tests construct cache
files using the same digest helper to stay aligned with the on-disk shape.
"""
import thumbnails
from thumbnails import _thumbnail_path_identity


def _make_cached_file(cache_dir, rel_path, stat_value):
    """Create a fake cached thumbnail with the same on-disk shape the real code uses."""
    identity = _thumbnail_path_identity(rel_path)
    return cache_dir / f"{identity}.{stat_value}.1.jpg"


def test_batch_remove_thumbnails_removes_all_matching_paths_in_one_walk(monkeypatch, tmp_path):
    import app as app_module

    cache = tmp_path / "thumbs"
    cache.mkdir()
    monkeypatch.setattr(app_module, "THUMBNAIL_CACHE_DIR", cache)
    # ensure_thumbnail_cache_dir() is a no-op for these tests — the cache already exists.
    monkeypatch.setattr(thumbnails, "ensure_thumbnail_cache_dir", lambda: None)

    target_a = _make_cached_file(cache, "cats/cat", "111")
    target_b = _make_cached_file(cache, "dogs/dog", "222")
    target_c = _make_cached_file(cache, "sub/dir/img", "333")
    survivor = _make_cached_file(cache, "unrelated__x", "444")
    for f in (target_a, target_b, target_c, survivor):
        f.write_bytes(b"x")

    app_module.batch_remove_thumbnails(["cats/cat", "dogs/dog", "sub/dir/img"])

    assert not target_a.exists()
    assert not target_b.exists()
    assert not target_c.exists()
    assert survivor.exists(), "unrelated cache file must not be deleted"


def test_batch_remove_thumbnails_noop_on_empty(monkeypatch, tmp_path):
    import app as app_module

    cache = tmp_path / "thumbs"
    cache.mkdir()
    monkeypatch.setattr(app_module, "THUMBNAIL_CACHE_DIR", cache)
    monkeypatch.setattr(thumbnails, "ensure_thumbnail_cache_dir", lambda: None)
    # Must not raise even when the path list is empty or the cache is empty.
    app_module.batch_remove_thumbnails([])
    app_module.batch_remove_thumbnails(["any/path"])


def test_remove_thumbnail_cache_for_delegates_to_batch(monkeypatch, tmp_path):
    import app as app_module

    cache = tmp_path / "thumbs"
    cache.mkdir()
    monkeypatch.setattr(app_module, "THUMBNAIL_CACHE_DIR", cache)
    monkeypatch.setattr(thumbnails, "ensure_thumbnail_cache_dir", lambda: None)

    target = _make_cached_file(cache, "cats/cat", "111")
    other = _make_cached_file(cache, "dogs/dog", "222")
    target.write_bytes(b"x")
    other.write_bytes(b"x")

    app_module.remove_thumbnail_cache_for("cats/cat")

    assert not target.exists()
    assert other.exists()


def test_batch_remove_thumbnails_does_not_delete_legacy_path_swapped_files(monkeypatch, tmp_path):
    """Issue #515: cache files written by the old ``<path-swap>.<mtime>.<size>.jpg``
    contract must NOT be deleted by ``batch_remove_thumbnails`` matching the new
    digest-based identity. The two formats share no prefix (the legacy one has
    no hex-digest segment), so a delete for any current path must leave legacy
    files alone and vice versa — the cleanup contract is intentionally scoped to
    files it actually authored.
    """
    import app as app_module

    cache = tmp_path / "thumbs"
    cache.mkdir()
    monkeypatch.setattr(app_module, "THUMBNAIL_CACHE_DIR", cache)
    monkeypatch.setattr(thumbnails, "ensure_thumbnail_cache_dir", lambda: None)

    # Legacy file shape: ``<rel_path with / swapped for __>.<mtime_ns>.<size>.jpg``.
    legacy = cache / "cats__cat.111.1.jpg"
    legacy.write_bytes(b"legacy")

    # Deleting the modern cache entry for the same rel_path must not touch the
    # legacy file (the cleanup contract does not own legacy entries — they are
    # orphans handled by future regeneration, never by unsafe bulk delete).
    app_module.remove_thumbnail_cache_for("cats/cat")
    assert legacy.exists(), "legacy cache file must NOT be deleted by modern cleanup"


def test_batch_remove_thumbnails_isolates_colliding_path_names(monkeypatch, tmp_path):
    """Issue #515: ``a/b.png`` and ``a__b.png`` are distinct paths whose legacy
    cache names collapsed to ``a__b.png``; the new identity digest must keep
    each path's cache files fully isolated under arbitrary stat values.
    """
    import app as app_module

    cache = tmp_path / "thumbs"
    cache.mkdir()
    monkeypatch.setattr(app_module, "THUMBNAIL_CACHE_DIR", cache)
    monkeypatch.setattr(thumbnails, "ensure_thumbnail_cache_dir", lambda: None)

    nested = _make_cached_file(cache, "a/b.png", "100")
    flat = _make_cached_file(cache, "a__b.png", "100")
    nested.write_bytes(b"n")
    flat.write_bytes(b"f")

    app_module.batch_remove_thumbnails(["a/b.png"])

    assert not nested.exists(), "the nested-path cache entry must be deleted"
    assert flat.exists(), "the flat-name cache entry must NOT be deleted (distinct path)"

    app_module.batch_remove_thumbnails(["a__b.png"])

    assert not flat.exists()
    assert not nested.exists()
