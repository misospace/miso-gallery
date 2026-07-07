"""Regression tests for the batched thumbnail cache cleanup helper (issue #249)."""

import app as app_module


def _make_cached_file(cache_dir, safe_name, suffix):
    """Create a fake cached thumbnail with the same on-disk shape the real code uses."""
    return cache_dir / f"{safe_name}.{suffix}.jpg"


def test_batch_remove_thumbnails_removes_all_matching_paths_in_one_walk(monkeypatch, tmp_path):
    cache = tmp_path / "thumbs"
    cache.mkdir()
    monkeypatch.setattr(app_module, "THUMBNAIL_CACHE_DIR", cache)
    # ensure_thumbnail_cache_dir() is a no-op for these tests — the cache already exists.
    monkeypatch.setattr(app_module, "ensure_thumbnail_cache_dir", lambda: None)

    target_a = _make_cached_file(cache, "cats__cat", "111")
    target_b = _make_cached_file(cache, "dogs__dog", "222")
    target_c = _make_cached_file(cache, "sub__dir__img", "333")
    survivor = _make_cached_file(cache, "unrelated__x", "444")
    for f in (target_a, target_b, target_c, survivor):
        f.write_bytes(b"x")

    app_module.batch_remove_thumbnails(["cats/cat", "dogs/dog", "sub/dir/img"])

    assert not target_a.exists()
    assert not target_b.exists()
    assert not target_c.exists()
    assert survivor.exists(), "unrelated cache file must not be deleted"


def test_batch_remove_thumbnails_noop_on_empty(monkeypatch, tmp_path):
    cache = tmp_path / "thumbs"
    cache.mkdir()
    monkeypatch.setattr(app_module, "THUMBNAIL_CACHE_DIR", cache)
    monkeypatch.setattr(app_module, "ensure_thumbnail_cache_dir", lambda: None)
    # Must not raise even when the path list is empty or the cache is empty.
    app_module.batch_remove_thumbnails([])
    app_module.batch_remove_thumbnails(["any/path"])


def test_remove_thumbnail_cache_for_delegates_to_batch(monkeypatch, tmp_path):
    cache = tmp_path / "thumbs"
    cache.mkdir()
    monkeypatch.setattr(app_module, "THUMBNAIL_CACHE_DIR", cache)
    monkeypatch.setattr(app_module, "ensure_thumbnail_cache_dir", lambda: None)

    target = _make_cached_file(cache, "cats__cat", "111")
    other = _make_cached_file(cache, "dogs__dog", "222")
    target.write_bytes(b"x")
    other.write_bytes(b"x")

    app_module.remove_thumbnail_cache_for("cats/cat")

    assert not target.exists()
    assert other.exists()
