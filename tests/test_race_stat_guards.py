"""Regression tests for issue #668: unguarded post-existence stat() races.

A media file that vanishes between an existence check (iterdir/is_servable_media/
hash phase) and a later stat() call must not 500 the request. Each listed call
site drops the vanished item (index, dedup, trash listing) or answers 404
(thumb image/video) instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

from conftest import build_client


def _fresh_app():
    return sys.modules["app"]


def _save_png(path: Path, color: str = "red") -> None:
    Image.new("RGB", (8, 8), color=color).save(path)


def _flaky_stat_for(victim: Path, original_stat, exc: type[BaseException]):
    """Return a Path.stat replacement that raises *exc* whenever *victim* is stat'ed."""

    def flaky_stat(self, *args, **kwargs):
        if self == victim:
            raise exc("simulated vanish (file deleted mid-request)")
        return original_stat(self, *args, **kwargs)

    return flaky_stat


def test_index_skips_file_vanished_after_iterdir(monkeypatch, tmp_path):
    """Deleting a.png between folder_path.iterdir() and item.stat() must drop the
    item and still render the surviving files instead of 500-ing the page."""
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")
    _save_png(data_dir / "a.png")
    _save_png(data_dir / "b.png")

    original_stat = Path.stat
    monkeypatch.setattr(Path, "stat", _flaky_stat_for(data_dir / "a.png", original_stat, OSError))

    resp = client.get("/")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    html = resp.get_data(as_text=True)
    assert "b.png" in html
    assert "a.png" not in html


def test_index_still_renders_sizes_when_files_exist(monkeypatch, tmp_path):
    """The guarded path still reports correct sizes when nothing vanishes."""
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")
    _save_png(data_dir / "b.png")
    expected_size = (data_dir / "b.png").stat().st_size

    resp = client.get("/")
    assert resp.status_code == 200
    assert f"{expected_size:.1f} B" in resp.get_data(as_text=True)


def test_check_thumbnails_skips_source_vanished_after_walk(monkeypatch, tmp_path):
    """run_thumbnail_integrity_check stats each walked image for the cache
    identity; a vanish between the walk and that stat is skipped instead of
    500-ing the maintenance route (same helper as the /thumb guard)."""
    import thumbnails

    build_client(monkeypatch, tmp_path, auth_type="none")
    data_dir = tmp_path / "data"
    victim = data_dir / "a.png"
    _save_png(victim)

    original_stat = Path.stat
    monkeypatch.setattr(Path, "stat", _flaky_stat_for(victim, original_stat, OSError))

    stats = thumbnails.run_thumbnail_integrity_check()
    assert stats["failed"] == 0
    assert stats["checked"] >= 0


def test_thumb_image_404_when_source_vanishes_after_existence_check(monkeypatch, tmp_path):
    """thumbnail_filename()'s stat runs right after is_servable_media() confirmed
    the source exists; a vanish in that window must 404 the route, not 500."""
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")
    _save_png(data_dir / "a.png")
    app_module = _fresh_app()

    original_stat = Path.stat
    victim = data_dir / "a.png"
    state = {"exists_confirmed": False}
    original_is_servable = app_module.is_servable_media

    def is_servable_then_vanish(path):
        ok = original_is_servable(path)
        if ok:
            state["exists_confirmed"] = True
        return ok

    def flaky_stat(self, *args, **kwargs):
        if self == victim and state["exists_confirmed"]:
            raise FileNotFoundError("simulated vanish after existence check")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(app_module, "is_servable_media", is_servable_then_vanish)
    monkeypatch.setattr(Path, "stat", flaky_stat)

    resp = client.get("/thumb/a.png")
    assert resp.status_code == 404, resp.get_data(as_text=True)


def test_thumb_video_404_when_source_vanishes_before_range_serve(monkeypatch, tmp_path):
    """_serve_video_with_range() stats the source before any guarded read; a
    vanish after the route's existence check must 404, not 500."""
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")
    victim = data_dir / "clip.mp4"
    victim.write_bytes(b"\x00" * 64)
    app_module = _fresh_app()

    original_stat = Path.stat
    state = {"exists_confirmed": False}
    original_thumbnail_filename = app_module.thumbnail_filename

    def thumbnail_filename_then_vanish(rel_path, source_path):
        name = original_thumbnail_filename(rel_path, source_path)
        state["exists_confirmed"] = True
        return name

    def flaky_stat(self, *args, **kwargs):
        if self == victim and state["exists_confirmed"]:
            raise FileNotFoundError("simulated vanish after existence check")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(app_module, "thumbnail_filename", thumbnail_filename_then_vanish)
    monkeypatch.setattr(Path, "stat", flaky_stat)

    resp = client.get("/thumb/clip.mp4")
    assert resp.status_code == 404, resp.get_data(as_text=True)


def test_find_duplicate_media_skips_group_when_keep_file_vanishes(monkeypatch, tmp_path):
    """matches[0].stat() runs on already-hashed files; a vanish between hashing
    and the group build must skip the group instead of 500-ing /api/llm/dedup."""
    import gallery_scan

    build_client(monkeypatch, tmp_path)
    data_dir = tmp_path / "data"
    victim = data_dir / "copy.png"

    original_stat = Path.stat
    state = {"hashed": False}
    original_file_sha256 = gallery_scan.file_sha256

    def hash_then_vanish(path):
        digest = original_file_sha256(path)
        state["hashed"] = True
        return digest

    def flaky_stat(self, *args, **kwargs):
        if self == victim and state["hashed"]:
            raise OSError("simulated vanish after hash phase")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(gallery_scan, "file_sha256", hash_then_vanish)
    monkeypatch.setattr(Path, "stat", flaky_stat)

    groups = gallery_scan.find_duplicate_media()
    assert groups == []


def test_find_duplicate_media_keeps_sizes_when_files_exist(monkeypatch, tmp_path):
    """The guarded tail still reports the keep file's size when nothing vanishes."""
    import gallery_scan

    build_client(monkeypatch, tmp_path)
    expected_size = (tmp_path / "data" / "sample.png").stat().st_size

    groups = gallery_scan.find_duplicate_media()
    assert len(groups) == 1
    assert groups[0]["size"] == expected_size


def test_list_trash_skips_item_reclaimed_mid_iteration(monkeypatch, tmp_path):
    """A trash entry reclaimed by a concurrent empty_trash/purge between
    td.iterdir() and item.stat() must be skipped instead of 500-ing /trash."""
    from trash import META_SUFFIX, list_trash, move_to_trash, trash_dir

    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")
    _save_png(data_dir / "a.png")
    _save_png(data_dir / "b.png")
    move_to_trash(data_dir / "a.png", data_dir)
    move_to_trash(data_dir / "b.png", data_dir)

    victim = next(
        p for p in trash_dir(data_dir).iterdir() if not p.name.endswith(META_SUFFIX) and p.name.endswith("_a.png")
    )

    original_stat = Path.stat
    monkeypatch.setattr(Path, "stat", _flaky_stat_for(victim, original_stat, OSError))

    entries = list_trash(data_dir)
    assert len(entries) == 1
    assert entries[0]["name"].endswith("_b.png")
    assert entries[0]["size"] > 0


def test_trash_page_renders_when_entry_vanishes_mid_listing(monkeypatch, tmp_path):
    """The /trash route itself must render 200 when list_trash drops a vanished
    entry, instead of turning the race into an HTTP 500."""
    from trash import META_SUFFIX, move_to_trash, trash_dir

    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")
    _save_png(data_dir / "a.png")
    move_to_trash(data_dir / "a.png", data_dir)

    victim = next(
        p for p in trash_dir(data_dir).iterdir() if not p.name.endswith(META_SUFFIX)
    )

    original_stat = Path.stat
    monkeypatch.setattr(Path, "stat", _flaky_stat_for(victim, original_stat, OSError))

    resp = client.get("/trash")
    assert resp.status_code == 200, resp.get_data(as_text=True)
