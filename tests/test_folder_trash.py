"""Behavioral tests for folder selection, deletion, and trash behavior.

Covers:
- Single file move to trash (existing path)
- Single directory move to trash (fixed path)
- Bulk delete with mixed files and folders
- Restore from trash for both files and directories
- Trash listing includes directories
- Nested folder deletion
- Empty folder deletion
- Form-based bulk delete (web UI path)
"""

from __future__ import annotations

import contextlib
import errno
import json
import re
import time
from datetime import timedelta
from pathlib import Path

import pytest

import trash
from conftest import build_client
from trash import dir_size, list_trash, move_to_trash, purge_old_trash, restore_from_trash


def test_dir_size_ignores_stat_errors_and_symlinks(tmp_path, monkeypatch):
    root = tmp_path / "folder"
    root.mkdir()
    readable = root / "readable.txt"
    readable.write_text("ok")
    (root / "outside-link").symlink_to(tmp_path / "outside.txt")
    original_stat = trash.Path.stat

    def failing_stat(path, *args, **kwargs):
        if path == readable:
            raise OSError("unreadable")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(trash.Path, "stat", failing_stat)
    assert dir_size(root) == 0


def test_move_to_trash_falls_back_to_copy_and_cleans_partial_copy(tmp_path, monkeypatch):
    data = tmp_path / "data"
    source = data / "album"
    source.mkdir(parents=True)
    (source / "photo.txt").write_text("photo")
    real_rename = trash.Path.rename
    calls = []

    def cross_device_rename(path, destination):
        calls.append((path, destination))
        if path == source:
            raise OSError("cross-device")
        return real_rename(path, destination)

    monkeypatch.setattr(trash.Path, "rename", cross_device_rename)
    assert move_to_trash(source, data) is True
    assert not source.exists()
    assert calls

    source = data / "broken"
    source.mkdir()
    (source / "photo.txt").write_text("photo")
    original_copytree = trash.shutil.copytree

    def partial_copy(*args, **kwargs):
        destination = args[1]
        destination.mkdir()
        (destination / "partial.txt").write_text("partial")
        raise OSError("copy failed")

    monkeypatch.setattr(trash.Path, "rename", cross_device_rename)
    monkeypatch.setattr(trash.shutil, "copytree", partial_copy)
    assert move_to_trash(source, data) is False
    assert source.exists()
    assert not any(path.name.startswith("broken") for path in trash.trash_dir(data).iterdir())
    monkeypatch.setattr(trash.shutil, "copytree", original_copytree)


def test_list_trash_ignores_corrupt_metadata_and_unreadable_entries(tmp_path, monkeypatch):
    data = tmp_path / "data"
    td = trash.trash_dir(data)
    item = td / "item"
    item.write_text("contents")
    item.with_name(item.name + trash.META_SUFFIX).write_text("not-json")
    original_stat = trash.Path.stat

    def failing_stat(path, *args, **kwargs):
        if path == item:
            raise OSError("unreadable")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(trash.Path, "stat", failing_stat)
    with pytest.raises(OSError):
        list_trash(data)


def test_restore_rejects_traversal_and_renames_collisions(tmp_path):
    data = tmp_path / "data"
    td = trash.trash_dir(data)
    item = td / "deleted.txt"
    item.write_text("deleted")
    item.with_name(item.name + trash.META_SUFFIX).write_text(
        json.dumps({"original": "albums/photo.txt"})
    )
    (data / "albums").mkdir()
    (data / "albums/photo.txt").write_text("existing")
    assert restore_from_trash(item.name, data) is True
    assert (data / "albums/photo.txt").read_text() == "existing"
    restored = list((data / "albums").glob("photo_*.txt"))
    assert len(restored) == 1
    assert restored[0].read_text() == "deleted"

    for name in ("../item", "nested/item", "\\\\item"):
        with pytest.raises(ValueError):
            restore_from_trash(name, data)


def test_restore_rejects_escaping_original_and_purge_falls_back_to_mtime(tmp_path):
    data = tmp_path / "data"
    td = trash.trash_dir(data)
    item = td / "unsafe"
    item.write_text("unsafe")
    item.with_name(item.name + trash.META_SUFFIX).write_text(
        json.dumps({"original": "../outside.txt"})
    )
    with pytest.raises(ValueError):
        restore_from_trash(item.name, data)

    old = td / "old.txt"
    old.write_text("old")
    old_meta = old.with_name(old.name + trash.META_SUFFIX)
    old_meta.write_text(json.dumps({"deleted_at": "legacy-invalid"}))
    old_time = time.time() - 3 * 86400
    import os
    os.utime(old, (old_time, old_time))
    assert purge_old_trash(data, retention_days=1) == 1
    assert not old.exists()


def test_purge_old_trash_survives_item_vanishing_mid_sweep(tmp_path, monkeypatch):
    """A restore renaming an expired entry away mid-sweep skips it (#565).

    ``purge_old_trash`` stats each entry *before* acquiring the source
    lock, so a concurrent ``restore_from_trash`` fast-path rename landing
    between ``iterdir()`` and ``stat()`` used to raise FileNotFoundError
    out of the sweep. The interleaving is made deterministic: the victim's
    stat unlinks the entry first (exactly what the racing restore does to
    it), so the pre-lock probe sees the same FileNotFoundError the race
    produces. A second expired entry proves the sweep continues past the
    vanished one, and the victim's orphaned meta stays behind for
    ``empty_trash`` (the purge loop skips meta entries entirely).
    """
    data = tmp_path / "data"
    td = trash.trash_dir(data)
    old_iso = (trash._utcnow() - timedelta(days=40)).isoformat()

    victim = td / "victim.txt"
    victim.write_text("victim")
    victim_meta = victim.with_name(victim.name + trash.META_SUFFIX)
    victim_meta.write_text(json.dumps({"original": "victim.txt", "deleted_at": old_iso}))
    expired = td / "expired.txt"
    expired.write_text("expired")
    expired.with_name(expired.name + trash.META_SUFFIX).write_text(
        json.dumps({"original": "expired.txt", "deleted_at": old_iso})
    )

    original_stat = trash.Path.stat

    def racy_stat(path, *args, **kwargs):
        if path == victim:
            # The concurrent restore's rename wins the race against this
            # entry's stat: by the time the sweep examines it, it is gone.
            victim.unlink(missing_ok=True)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(trash.Path, "stat", racy_stat)
    assert purge_old_trash(data, retention_days=1) == 1

    assert not expired.exists()
    assert not victim.exists()
    assert victim_meta.exists()


class TestMoveToTrashSingleFile:
    """Verify single-file trash behavior (regression guard)."""

    def test_single_file_to_trash(self, monkeypatch, tmp_path):
        client, data_dir = build_client(monkeypatch, tmp_path)
        from trash import list_trash, move_to_trash

        item = data_dir / "cats" / "cat.jpg"
        assert item.exists() and item.is_file()

        result = move_to_trash(item, data_dir)
        assert result is True
        assert not item.exists()

        trash_items = list_trash(data_dir)
        assert len(trash_items) == 1
        assert trash_items[0]["original"] == "cats/cat.jpg"


class TestMoveToTrashDirectory:
    """Verify directory move to trash (the bug fix)."""

    def test_single_folder_to_trash(self, monkeypatch, tmp_path):
        from trash import list_trash, move_to_trash

        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = data_dir / "cats"
        assert folder.exists() and folder.is_dir()

        result = move_to_trash(folder, data_dir)
        assert result is True
        assert not folder.exists()

        trash_items = list_trash(data_dir)
        assert len(trash_items) == 1
        assert trash_items[0]["original"] == "cats"
        assert trash_items[0]["size"] > 0

    def test_nested_folder_to_trash(self, monkeypatch, tmp_path):
        from trash import list_trash, move_to_trash

        client, data_dir = build_client(monkeypatch, tmp_path)
        nested = data_dir / "photos" / "vacation" / "beach"
        nested.mkdir(parents=True)
        (nested / "photo1.jpg").write_bytes(b"\xff\xd8\xff\xe0fake_jpeg")
        (nested / "photo2.jpg").write_bytes(b"\xff\xd8\xff\xe0fake_jpeg2")

        result = move_to_trash(nested, data_dir)
        assert result is True
        assert not nested.exists()

        trash_items = list_trash(data_dir)
        assert len(trash_items) == 1
        # The original path should reflect the full relative path
        assert trash_items[0]["original"] == "photos/vacation/beach"

    def test_empty_folder_to_trash(self, monkeypatch, tmp_path):
        from trash import list_trash, move_to_trash

        client, data_dir = build_client(monkeypatch, tmp_path)
        empty_folder = data_dir / "empty_dir"
        empty_folder.mkdir()

        result = move_to_trash(empty_folder, data_dir)
        assert result is True
        assert not empty_folder.exists()

        trash_items = list_trash(data_dir)
        assert len(trash_items) == 1
        assert trash_items[0]["size"] == 0

    def test_nonexistent_path_returns_false(self, monkeypatch, tmp_path):
        from trash import move_to_trash

        client, data_dir = build_client(monkeypatch, tmp_path)
        fake = data_dir / "does_not_exist"

        result = move_to_trash(fake, data_dir)
        assert result is False


class TestBulkDelete:
    """Verify bulk delete handles both files and folders correctly."""

    def test_bulk_delete_mixed_files_and_folders(self, monkeypatch, tmp_path):
        from trash import list_trash, move_to_trash

        client, data_dir = build_client(monkeypatch, tmp_path)
        (data_dir / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0fake_jpeg")
        folder1 = data_dir / "folder_a"
        folder1.mkdir()
        (folder1 / "file_in_a.jpg").write_bytes(b"\xff\xd8\xff\xe0fake_jpeg")
        folder2 = data_dir / "folder_b"
        folder2.mkdir()
        (folder2 / "file_in_b.jpg").write_bytes(b"\xff\xd8\xff\xe0fake_jpeg2")

        for rel in ["photo.jpg", "folder_a", "folder_b"]:
            path = data_dir / rel
            if path.is_file() or path.is_dir():
                move_to_trash(path, data_dir)

        trash_items = list_trash(data_dir)
        assert len(trash_items) == 3
        originals = {t["original"] for t in trash_items}
        assert "photo.jpg" in originals
        assert "folder_a" in originals
        assert "folder_b" in originals

    def test_bulk_delete_folders_via_web_form(self, monkeypatch, tmp_path):
        """Simulate the actual bulk-delete form submission with folder selection."""
        client, data_dir = build_client(monkeypatch, tmp_path)

        folder = data_dir / "test_folder"
        folder.mkdir()
        (folder / "img1.jpg").write_bytes(b"\xff\xd8\xff\xe0fake")
        (folder / "img2.jpg").write_bytes(b"\xff\xd8\xff\xe0fake2")

        (data_dir / "standalone.jpg").write_bytes(b"\xff\xd8\xff\xe0standalone")

        # Login to get session cookie
        login_resp = client.get("/login")
        csrf_match = re.search(
            r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']',
            login_resp.data.decode(),
        )
        assert csrf_match, "CSRF token not found in login form"
        login_csrf = csrf_match.group(1)

        auth_resp = client.post(
            "/auth",
            data={"password": "pass123", "next": "/", "csrf_token": login_csrf},
            follow_redirects=False,
        )
        assert auth_resp.status_code == 302

        # Now get the main page to extract CSRF for bulk-delete form
        main_resp = client.get("/")
        main_html = main_resp.data.decode()
        csrf_match2 = re.search(
            r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']',
            main_html,
        )
        assert csrf_match2, "CSRF token not found in main page"
        bulk_csrf = csrf_match2.group(1)

        resp = client.post(
            "/bulk-delete",
            data={
                "csrf_token": bulk_csrf,
                "current_subpath": "",
                "filenames": ["standalone.jpg"],
                "folders": ["test_folder"],
            },
            follow_redirects=True,
        )

        assert resp.status_code == 200
        body = resp.data.decode()
        # Should show success feedback (moved_files=1 and moved_folders=1)
        assert "success" in body.lower() or "moved" in body.lower()

    def test_bulk_delete_no_selection_is_nop(self, monkeypatch, tmp_path):
        """Bulk delete with no files/folders selected should be a noop."""
        client, data_dir = build_client(monkeypatch, tmp_path)

        # Login first
        login_resp = client.get("/login")
        csrf_match = re.search(
            r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']',
            login_resp.data.decode(),
        )
        assert csrf_match
        _ = client.post(
            "/auth",
            data={"password": "pass123", "next": "/", "csrf_token": csrf_match.group(1)},
            follow_redirects=False,
        )

        main_resp = client.get("/")
        csrf_match2 = re.search(
            r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']',
            main_resp.data.decode(),
        )
        assert csrf_match2

        resp = client.post(
            "/bulk-delete",
            data={
                "csrf_token": csrf_match2.group(1),
                "current_subpath": "",
                "filenames": [],
                "folders": [],
            },
            follow_redirects=True,
        )

        assert resp.status_code == 200
        body = resp.data.decode()
        # Should indicate noop (no items moved)
        assert "no selected items" in body.lower()


class TestTrashRestore:
    """Verify restore from trash works for both files and directories."""

    def test_restore_file_from_trash(self, monkeypatch, tmp_path):
        from trash import list_trash, move_to_trash, restore_from_trash

        client, data_dir = build_client(monkeypatch, tmp_path)
        item = data_dir / "sample.png"
        original_content = item.read_bytes()

        move_to_trash(item, data_dir)
        assert not item.exists()

        trash_items = list_trash(data_dir)
        trash_entry = trash_items[0]["name"]

        result = restore_from_trash(trash_entry, data_dir)
        assert result is True
        assert item.exists()
        assert item.read_bytes() == original_content

    def test_restore_folder_from_trash(self, monkeypatch, tmp_path):
        from trash import list_trash, move_to_trash, restore_from_trash

        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = data_dir / "restore_test"
        folder.mkdir()
        (folder / "file1.jpg").write_bytes(b"\xff\xd8\xff\xe0content1")
        nested = folder / "subdir"
        nested.mkdir()
        (nested / "file2.jpg").write_bytes(b"\xff\xd8\xff\xe0content2")

        content_before = {
            "file1.jpg": (folder / "file1.jpg").read_bytes(),
            "subdir/file2.jpg": (nested / "file2.jpg").read_bytes(),
        }

        move_to_trash(folder, data_dir)
        assert not folder.exists()

        trash_items = list_trash(data_dir)
        trash_entry = trash_items[0]["name"]

        result = restore_from_trash(trash_entry, data_dir)
        assert result is True
        assert folder.exists()
        assert (folder / "file1.jpg").read_bytes() == content_before["file1.jpg"]
        assert (nested / "file2.jpg").read_bytes() == content_before["subdir/file2.jpg"]


class TestRestoreConsumesTrashedDirectory:
    """Regression tests for orphaned directory copies after restore (#516).

    A successful directory restore must consume the trash source and its
    metadata, matching file-restore semantics; a failed restore must keep
    both recoverable.
    """

    def _make_folder(self, data_dir):
        folder = data_dir / "album"
        folder.mkdir()
        (folder / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0a")
        (folder / "sub").mkdir()
        (folder / "sub" / "b.jpg").write_bytes(b"\xff\xd8\xff\xe0b")
        return folder

    def test_restore_directory_removes_trash_source_and_metadata(self, monkeypatch, tmp_path):
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_folder(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item = trash.trash_dir(data_dir) / entry
        meta_file = trash.trash_dir(data_dir) / (entry + trash.META_SUFFIX)
        assert trash_item.is_dir() and meta_file.exists()

        assert restore_from_trash(entry, data_dir) is True
        assert (folder / "a.jpg").read_bytes() == b"\xff\xd8\xff\xe0a"
        assert (folder / "sub" / "b.jpg").read_bytes() == b"\xff\xd8\xff\xe0b"
        assert not trash_item.exists()
        assert not meta_file.exists()
        assert list_trash(data_dir) == []

    def test_restore_directory_collision_renames_and_consumes_trash_entry(self, monkeypatch, tmp_path):
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_folder(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]

        # Recreate a colliding destination holding different bytes.
        folder.mkdir()
        (folder / "new.txt").write_text("new")

        assert restore_from_trash(entry, data_dir) is True
        assert (folder / "new.txt").read_text() == "new"
        renamed = [p for p in data_dir.iterdir() if p.is_dir() and p.name.startswith("album_")]
        assert len(renamed) == 1
        assert (renamed[0] / "a.jpg").read_bytes() == b"\xff\xd8\xff\xe0a"
        assert (renamed[0] / "sub" / "b.jpg").read_bytes() == b"\xff\xd8\xff\xe0b"
        assert list_trash(data_dir) == []

    def test_repeated_trash_restore_cycles_do_not_accumulate_entries(self, monkeypatch, tmp_path):
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_folder(data_dir)

        for _ in range(3):
            assert move_to_trash(folder, data_dir) is True
            entries = list_trash(data_dir)
            assert len(entries) == 1
            assert restore_from_trash(entries[0]["name"], data_dir) is True
            assert list_trash(data_dir) == []
            assert (folder / "a.jpg").read_bytes() == b"\xff\xd8\xff\xe0a"

        unknown = [t for t in list_trash(data_dir) if t["original"] == "unknown"]
        assert unknown == []

    def test_cross_device_directory_restore_fallback_consumes_source(self, monkeypatch, tmp_path):
        """The copytree fallback in restore must consume the source like rename.

        The fallback now stages the copy on the *destination* filesystem
        (under ``dest.parent``); if it staged on the source filesystem
        (under ``td``), the staged ``stage.rename(dest)`` would cross the
        same boundary that just failed the original ``item.rename(dest)``
        and the fallback could never complete. This test fails on the
        old staging-on-source implementation: the staged rename is mocked
        to fail too, so the old code returns False while the new code
        successfully completes the staged rename and the source-metadata
        consume.
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_folder(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item = trash.trash_dir(data_dir) / entry
        meta_file = trash.trash_dir(data_dir) / (entry + trash.META_SUFFIX)

        real_rename = trash.Path.rename
        trash_root_resolved = trash.trash_dir(data_dir).resolve()

        def cross_device_rename(path, destination):
            # Simulate a true cross-filesystem restore: any rename whose
            # source lives under the trash filesystem fails with EXDEV,
            # including the staged rename if the stage is (incorrectly)
            # placed back under td. Renames whose source is under the
            # destination filesystem — which is what the new staging
            # produces — succeed.
            try:
                resolved = path.resolve()
            except OSError:
                return real_rename(path, destination)
            if trash_root_resolved in resolved.parents or resolved == trash_root_resolved:
                raise OSError("cross-device")
            return real_rename(path, destination)

        monkeypatch.setattr(trash.Path, "rename", cross_device_rename)

        assert restore_from_trash(entry, data_dir) is True
        assert (folder / "a.jpg").read_bytes() == b"\xff\xd8\xff\xe0a"
        assert (folder / "sub" / "b.jpg").read_bytes() == b"\xff\xd8\xff\xe0b"
        assert not trash_item.exists()
        assert not meta_file.exists()
        assert list_trash(data_dir) == []

    def test_failed_directory_restore_keeps_source_and_metadata(self, monkeypatch, tmp_path):
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_folder(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item = trash.trash_dir(data_dir) / entry
        meta_file = trash.trash_dir(data_dir) / (entry + trash.META_SUFFIX)

        real_rename = trash.Path.rename

        def cross_device_rename(path, destination):
            if path == trash_item:
                raise OSError("cross-device")
            return real_rename(path, destination)

        def failing_copytree(src, dest, *args, **kwargs):
            # Leave a partial copy behind, like a real interrupted copytree.
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "partial.jpg").write_bytes(b"partial")
            raise OSError("disk full")

        monkeypatch.setattr(trash.Path, "rename", cross_device_rename)
        monkeypatch.setattr(trash.shutil, "copytree", failing_copytree)

        assert restore_from_trash(entry, data_dir) is False
        assert trash_item.is_dir()
        assert (trash_item / "a.jpg").read_bytes() == b"\xff\xd8\xff\xe0a"
        assert meta_file.exists()
        assert not folder.exists()
        # The partial copy must not linger at the destination.
        entries = list_trash(data_dir)
        assert len(entries) == 1
        assert entries[0]["original"] != "unknown"
        # No staging directory left behind after the failed restore. The
        # stage lives on the destination filesystem (under dest.parent),
        # not under the trash root, so scan the whole data folder.
        stages = [p for p in data_dir.rglob(".restore-stage-*") if p.is_dir()]
        assert stages == [], f"stage directory leaked: {stages}"

    def test_concurrent_restore_does_not_delete_winners_destination(
        self, monkeypatch, tmp_path
    ):
        """Regression for review blocker on #516: when two restores of the
        same entry race, the loser's fallback cleanup must not delete the
        winner's restored directory.

        The cross-device fallback now stages the copy on the destination
        filesystem (under ``dest.parent``) and only ever rmtree's that
        privately-owned stage — never the function's computed dest.
        Under the previous code, an unconditional
        ``shutil.rmtree(dest)`` in the copytree failure handler would
        delete any directory that happened to occupy ``dest`` (including
        another concurrent restore's restored media).
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_folder(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item = trash.trash_dir(data_dir) / entry
        meta_file = trash.trash_dir(data_dir) / (entry + trash.META_SUFFIX)

        # Track every rmtree call inside the trash module so we can
        # prove no cleanup ever targets the function's computed dest.
        rmtree_targets: list[str] = []
        real_rmtree = trash.shutil.rmtree

        def tracking_rmtree(path, *args, **kwargs):
            rmtree_targets.append(str(path))
            return real_rmtree(path, *args, **kwargs)

        # Force the rename branch to fail (cross-device or already-moved
        # source) so the fallback runs.
        real_rename = trash.Path.rename

        def failing_rename(path, destination):
            if path == trash_item:
                raise OSError("source no longer present")
            return real_rename(path, destination)

        # Force copytree to fail with FileExistsError, simulating the
        # race where another restore has already populated the computed
        # destination path.
        def racing_copytree(src, dest, *args, **kwargs):
            raise FileExistsError(f"dest {dest} already exists")

        monkeypatch.setattr(trash.shutil, "rmtree", tracking_rmtree)
        monkeypatch.setattr(trash.Path, "rename", failing_rename)
        monkeypatch.setattr(trash.shutil, "copytree", racing_copytree)

        assert restore_from_trash(entry, data_dir) is False

        # The trash source and metadata must remain for a retry.
        assert trash_item.is_dir()
        assert meta_file.exists()
        # No .restore-stage-* directory leaks across calls. The stage lives
        # on the destination filesystem (under dest.parent), not under the
        # trash root, so scan the whole data folder.
        stages = [p for p in data_dir.rglob(".restore-stage-*") if p.is_dir()]
        assert stages == [], f"stage directory leaked: {stages}"

        # The critical guarantee: no rmtree call ever targeted the
        # destination ``dest``. Under the old code, the failure path
        # would have rmtree'd the function's computed dest, clobbering
        # a concurrent winner's restored media. The stage lives on the
        # destination filesystem (not under the trash root), so the
        # invariant can no longer be "every rmtree target is inside the
        # trash root"; it must be the more targeted
        # "rmtree never targets a path owned by another caller", i.e.
        # never the function's computed dest.
        dest_resolved = folder.resolve()
        for target in rmtree_targets:
            try:
                resolved = Path(target).resolve()
            except OSError:
                continue
            assert resolved != dest_resolved, (
                f"rmtree targeted the destination {target}; this is the "
                f"data-loss race the staging fix prevents."
            )

    def test_concurrent_restore_does_not_delete_winners_destination_end_to_end(
        self, monkeypatch, tmp_path
    ):
        """End-to-end regression for the concurrent restore race.

        Simulates two restores of the same entry interleaved: Request A
        has already populated the destination (``album``), and Request
        B observes the source still in trash (passes the exists check)
        but its rename fails (source has been moved concurrently) and
        falls into the copytree fallback. B's fallback cleanup must not
        delete A's restored directory.
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_folder(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item = trash.trash_dir(data_dir) / entry
        meta_file = trash.trash_dir(data_dir) / (entry + trash.META_SUFFIX)

        # Pre-populate the destination as if Request A had already
        # completed its restore. This is the "rival" the loser's
        # cleanup must not delete.
        rival_dest = data_dir / "album"
        rival_dest.mkdir()
        (rival_dest / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0a")
        (rival_dest / "sub").mkdir()
        (rival_dest / "sub" / "b.jpg").write_bytes(b"\xff\xd8\xff\xe0b")
        rival_inode = (rival_dest / "a.jpg").stat().st_ino

        # Force the function's dest-uniqueness check to ignore the
        # pre-existing rival so the function computes dest = album
        # (not album_<ts>). The OLD code's rmtree(dest) on a populated
        # album is what wipes the rival; we want to reproduce that
        # exact failure mode.
        real_exists = trash.Path.exists

        def exists_ignores_rival(self_path):
            if str(self_path) == str(rival_dest):
                return False
            return real_exists(self_path)

        # Force rename to fail for the trash source (B observes
        # "source gone" because A moved it).
        real_rename = trash.Path.rename

        def failing_rename(path, destination):
            if path == trash_item:
                raise OSError("source no longer present")
            return real_rename(path, destination)

        # Force copytree to fail because dest is occupied by A.
        def racing_copytree(src, dest, *args, **kwargs):
            raise FileExistsError(f"dest {dest} already exists")

        monkeypatch.setattr(trash.Path, "exists", exists_ignores_rival)
        monkeypatch.setattr(trash.Path, "rename", failing_rename)
        monkeypatch.setattr(trash.shutil, "copytree", racing_copytree)

        # Request B runs: rename fails, fallback copytree fails,
        # cleanup runs. B must return False without clobbering the
        # rival's restored directory.
        assert restore_from_trash(entry, data_dir) is False

        # A's restored media is intact (same inode).
        assert rival_dest.is_dir()
        rival_a = rival_dest / "a.jpg"
        assert rival_a.read_bytes() == b"\xff\xd8\xff\xe0a"
        assert rival_a.stat().st_ino == rival_inode
        assert (rival_dest / "sub" / "b.jpg").read_bytes() == b"\xff\xd8\xff\xe0b"
        # Trash source preserved (B's rename failed, so the trash entry
        # remains for a retry).
        assert trash_item.is_dir()
        assert meta_file.exists()
        # No leftover staging directory. The stage lives on the
        # destination filesystem (under dest.parent), not under the
        # trash root, so scan the whole data folder.
        stages = [p for p in data_dir.rglob(".restore-stage-*") if p.is_dir()]
        assert stages == [], f"stage directory leaked: {stages}"

class TestActiveRestoreStageProtection:
    """Regression tests for issue #557: trash maintenance must never
    reclaim a ``.restore-stage-*`` directory that an in-flight cross-device
    restore is copying into, while genuinely abandoned stages stay
    reclaimable under an explicit policy.

    The distinguishing signal is an exclusive ``flock`` on
    ``<stage>.lock`` held by the restoring process for the whole copy.
    ``flock`` is honored across processes (and treated independently per
    open file description even within one process, which is what these
    deterministic single-process interleavings exploit), and the kernel
    drops it when the owner crashes — so any worker's maintenance can tell
    an active stage from an abandoned one. A per-process set could not.
    """

    def _make_album(self, data_dir):
        folder = data_dir / "album"
        folder.mkdir()
        (folder / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0a")
        (folder / "sub").mkdir()
        (folder / "sub" / "b.jpg").write_bytes(b"\xff\xd8\xff\xe0b")
        return folder

    def _interleave_maintenance(self, monkeypatch, data_dir, maintenance):
        """Force the cross-device fallback and run *maintenance* mid-restore.

        Renames whose source lives under the trash fail with EXDEV (a true
        cross-filesystem restore), and *maintenance* is invoked between the
        real ``copytree`` and the staged ``rename`` — the window in which a
        second worker's ordinary maintenance used to delete the live stage.
        The assertion inside the copytree wrapper fails loudly if the sweep
        removes the stage the restore still owns.
        """
        real_rename = trash.Path.rename
        trash_root = trash.trash_dir(data_dir).resolve()

        def cross_device_rename(path, destination):
            try:
                resolved = path.resolve()
            except OSError:
                return real_rename(path, destination)
            if trash_root in resolved.parents or resolved == trash_root:
                raise OSError(errno.EXDEV, "cross-device link")
            return real_rename(path, destination)

        real_copytree = trash.shutil.copytree

        def copytree_with_maintenance(src, dest, *args, **kwargs):
            result = real_copytree(src, dest, *args, **kwargs)
            # shutil.copytree converts to str and recurses through this same
            # attribute; only the top-level call from restore_from_trash
            # passes Path objects, so inject maintenance exactly once.
            if isinstance(dest, Path):
                maintenance()
                assert dest.is_dir(), (
                    "concurrent trash maintenance deleted an active restore stage"
                )
            return result

        monkeypatch.setattr(trash.Path, "rename", cross_device_rename)
        monkeypatch.setattr(trash.shutil, "copytree", copytree_with_maintenance)

    def test_purge_during_active_cross_device_restore_preserves_stage(
        self, monkeypatch, tmp_path
    ):
        """The issue #557 repro: ``purge_old_trash`` with the default
        30-day retention used to delete a stage created milliseconds
        earlier. Maintenance must skip the locked stage and the restore
        must complete."""
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_album(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item = trash.trash_dir(data_dir) / entry
        meta_file = trash.trash_dir(data_dir) / (entry + trash.META_SUFFIX)

        self._interleave_maintenance(
            monkeypatch, data_dir, lambda: trash.purge_old_trash(data_dir, retention_days=30)
        )

        assert restore_from_trash(entry, data_dir) is True
        assert (folder / "a.jpg").read_bytes() == b"\xff\xd8\xff\xe0a"
        assert (folder / "sub" / "b.jpg").read_bytes() == b"\xff\xd8\xff\xe0b"
        assert not trash_item.exists()
        assert not meta_file.exists()
        assert list_trash(data_dir) == []
        leftovers = [p for p in data_dir.rglob(f"{trash.STAGE_PREFIX}*")]
        assert leftovers == [], f"stage artifacts leaked: {leftovers}"

    def test_empty_during_active_cross_device_restore_preserves_stage(
        self, monkeypatch, tmp_path
    ):
        """``empty_trash`` run mid-restore must not delete the live stage.

        Emptying the trash removes the restore's trash source from under
        it; the staged copy is already complete at that point, so the
        restore still reports success with the full tree at its
        destination rather than failing after the media was restored.
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_album(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]

        self._interleave_maintenance(
            monkeypatch, data_dir, lambda: trash.empty_trash(data_dir)
        )

        assert restore_from_trash(entry, data_dir) is True
        assert (folder / "a.jpg").read_bytes() == b"\xff\xd8\xff\xe0a"
        assert (folder / "sub" / "b.jpg").read_bytes() == b"\xff\xd8\xff\xe0b"
        assert list_trash(data_dir) == []
        leftovers = [p for p in data_dir.rglob(f"{trash.STAGE_PREFIX}*")]
        assert leftovers == [], f"stage artifacts leaked: {leftovers}"

    def test_crashed_owner_stage_reclaimed_immediately(self, monkeypatch, tmp_path):
        """A stage plus a released lock file is abandoned, not active.

        A crashed owner's ``flock`` is dropped by the kernel, so the lock
        file is present but held by nobody. Maintenance reclaims such a
        stage immediately under both entry points, without waiting out the
        grace period (the orphans here carry fresh timestamps).
        """
        client, data_dir = build_client(monkeypatch, tmp_path)

        orphan_parent = data_dir / "album"
        orphan_parent.mkdir()
        orphan = orphan_parent / ".restore-stage-deadbeef"
        orphan.mkdir()
        (orphan / "partial.jpg").write_bytes(b"partial")
        lock = trash._stage_lock_path(orphan)
        lock.write_text("")

        trash.empty_trash(data_dir)
        assert not orphan.exists(), "empty_trash must reclaim a stage whose lock is released"
        assert not lock.exists(), "the released lock file must be reclaimed with its stage"

        # Same again under purge_old_trash with a fresh crashed orphan.
        orphan.mkdir()
        (orphan / "partial.jpg").write_bytes(b"partial")
        lock.write_text("")
        trash.purge_old_trash(data_dir, retention_days=30)
        assert not orphan.exists(), "purge_old_trash must reclaim a stage whose lock is released"

        # And a crashed orphan at the data folder root, not under a subdir.
        orphan_root = data_dir / ".restore-stage-abcdef"
        orphan_root.mkdir()
        trash._stage_lock_path(orphan_root).write_text("")
        trash.empty_trash(data_dir)
        assert not orphan_root.exists(), (
            "empty_trash must reclaim orphan stages at the data folder root"
        )

    def test_held_lock_protects_stage_even_past_grace(self, monkeypatch, tmp_path):
        """A lock held by a live process outranks the age policy.

        With the grace period forced negative (everything looks old), a
        stage whose lock this process still holds must survive both
        maintenance entry points — this is what distinguishes an active
        stage from an abandoned one.
        """
        import fcntl

        client, data_dir = build_client(monkeypatch, tmp_path)
        album = data_dir / "album"
        album.mkdir()
        # The directory name must match the sweep glob, or the sweep would
        # never inspect it and the test would pass vacuously.
        stage = album / ".restore-stage-held"
        stage.mkdir()
        (stage / "partial.jpg").write_bytes(b"partial")
        lock = trash._stage_lock_path(stage)
        fh = open(lock, "w")  # noqa: SIM115 - held open to keep the flock live
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            monkeypatch.setattr(trash, "STAGE_ABANDON_GRACE_SECONDS", -1)
            trash.empty_trash(data_dir)
            trash.purge_old_trash(data_dir, retention_days=1)
            assert stage.exists(), "maintenance must not reclaim a stage under a held lock"
            assert lock.exists()
        finally:
            fh.close()

        # Control: release the lock (simulating the owner's death) and the
        # very same stage becomes reclaimable, proving the sweep did see it.
        trash.empty_trash(data_dir)
        assert not stage.exists(), "released stage must be reclaimed once lock is free"

    def test_lock_free_stage_respects_explicit_grace_policy(self, monkeypatch, tmp_path):
        """Stages with no verifiable lock are abandoned only by age.

        Lock-free stages are legacy leftovers (written before the lock
        protocol) or restores on filesystems without flock support. They
        are preserved while they could still be a live copy, and reclaimed
        once older than ``STAGE_ABANDON_GRACE_SECONDS``.
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        orphan_parent = data_dir / "album"
        orphan_parent.mkdir()
        orphan = orphan_parent / ".restore-stage-legacy"
        orphan.mkdir()
        (orphan / "partial.jpg").write_bytes(b"partial")

        # Fresh and unverifiable: keep it (it could be a live copy).
        trash.empty_trash(data_dir)
        trash.purge_old_trash(data_dir, retention_days=1)
        assert orphan.exists(), "a fresh lock-free stage must survive the grace period"

        # Past the grace period: abandoned, reclaimed by both entry points.
        monkeypatch.setattr(trash, "STAGE_ABANDON_GRACE_SECONDS", -1)
        trash.empty_trash(data_dir)
        assert not orphan.exists(), "empty_trash must reclaim a grace-expired lock-free stage"

        orphan.mkdir()
        monkeypatch.setattr(trash, "STAGE_ABANDON_GRACE_SECONDS", -1)
        trash.purge_old_trash(data_dir, retention_days=1)
        assert not orphan.exists(), "purge_old_trash must reclaim a grace-expired stage"

        # A grace-expired orphan at the data folder root too.
        orphan_root = data_dir / ".restore-stage-abcdef"
        orphan_root.mkdir()
        trash.empty_trash(data_dir)
        assert not orphan_root.exists(), (
            "empty_trash must reclaim orphan stages at the data folder root"
        )

    def test_leaked_lock_file_lifecycle(self, monkeypatch, tmp_path):
        """A lock file whose stage directory never appeared is reclaimed
        only under the grace policy, and never while a live restore holds
        it.

        A free lock without a directory proves nothing: the owner may be
        moments from creating the stage (the restore takes the flock just
        before copytree creates the directory). Unlinking it would strip a
        live restore of its protection mid-copy, so freshness preserves it
        even when it looks free.
        """
        import fcntl

        client, data_dir = build_client(monkeypatch, tmp_path)
        leak = data_dir / "album"
        leak.mkdir()
        lock = leak / f"{trash.STAGE_PREFIX}ghost{trash.STAGE_LOCK_SUFFIX}"

        # Fresh free lock-only entry: looks abandoned, is not provably so.
        lock.write_text("")
        trash.empty_trash(data_dir)
        trash.purge_old_trash(data_dir, retention_days=30)
        assert lock.exists(), "a fresh orphan lock must survive the grace period"

        # Past the grace period with no stage ever appearing: a real leak.
        monkeypatch.setattr(trash, "STAGE_ABANDON_GRACE_SECONDS", -1)
        trash.empty_trash(data_dir)
        assert not lock.exists(), "empty_trash must reclaim a grace-expired orphan lock file"

        # Held lock (owner mid-restore): preserved even with grace negative.
        lock.write_text("")
        fh = open(lock, "w")  # noqa: SIM115 - held open to keep the flock live
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            monkeypatch.setattr(trash, "STAGE_ABANDON_GRACE_SECONDS", -1)
            trash.empty_trash(data_dir)
            assert lock.exists(), "maintenance must not unlink a lock an active restore holds"
        finally:
            fh.close()

    def test_lock_acquisition_failure_leaves_no_abandon_signal(self, monkeypatch, tmp_path):
        """A failed lock attempt must clean up after itself.

        If flock is unsupported (or the open itself fails), no lock file
        may remain: a present-but-free lock file is the signal for a
        *crashed* owner, and leaving one behind for a live restore would
        invite immediate reclaim by the very maintenance it is meant to
        deter. Without a lock file, the stage only faces the conservative
        grace policy.
        """

        def unsupported_flock(*args, **kwargs):
            raise OSError(errno.EINVAL, "flock unsupported")

        monkeypatch.setattr(trash.fcntl, "flock", unsupported_flock)

        assert trash._acquire_stage_lock(tmp_path / ".restore-stage-x") is None
        assert not (tmp_path / ".restore-stage-x.lock").exists(), (
            "failed acquisition must leave no lock file behind"
        )

        def boom_open(*args, **kwargs):
            raise OSError(errno.EROFS, "read-only filesystem")

        monkeypatch.setattr("builtins.open", boom_open)
        assert trash._acquire_stage_lock(tmp_path / ".restore-stage-y") is None

    def test_unverifiable_lock_state_falls_back_to_grace_policy(self, monkeypatch, tmp_path):
        """Lock states other than held/free classify as unknown, not abandoned.

        On a filesystem where flock errors (not EAGAIN), maintenance must
        not read the lock as released-and-reclaim-now; the stage is only
        reclaimed once past the explicit grace period.
        """

        def unsupported_flock(*args, **kwargs):
            raise OSError(errno.EINVAL, "flock unsupported")

        client, data_dir = build_client(monkeypatch, tmp_path)
        stage = data_dir / ".restore-stage-nolockfs"
        stage.mkdir()
        lock = trash._stage_lock_path(stage)
        lock.write_text("")

        monkeypatch.setattr(trash.fcntl, "flock", unsupported_flock)
        assert trash._stage_lock_state(lock) is None

        trash.empty_trash(data_dir)
        assert stage.exists(), "unverifiable fresh stage must survive the grace period"

        monkeypatch.setattr(trash, "STAGE_ABANDON_GRACE_SECONDS", -1)
        trash.empty_trash(data_dir)
        assert not stage.exists(), "unverifiable grace-expired stage must be reclaimed"
        assert not lock.exists()

    def test_failed_source_cleanup_with_source_present_keeps_retry_semantics(
        self, monkeypatch, tmp_path
    ):
        """The vanished-source success is narrowly scoped.

        When the trash source still exists but cannot be removed (e.g.
        permission error), the restore must keep its historical behavior:
        return False with the trash entry and metadata preserved so a
        retry stays possible. Only a source that is *gone* (reclaimed by
        concurrent maintenance after the copy completed) counts as done.
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_album(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item = trash.trash_dir(data_dir) / entry
        meta_file = trash.trash_dir(data_dir) / (entry + trash.META_SUFFIX)

        real_rename = trash.Path.rename
        trash_root = trash.trash_dir(data_dir).resolve()

        def cross_device_rename(path, destination):
            try:
                resolved = path.resolve()
            except OSError:
                return real_rename(path, destination)
            if trash_root in resolved.parents or resolved == trash_root:
                raise OSError(errno.EXDEV, "cross-device link")
            return real_rename(path, destination)

        real_rmtree = trash.shutil.rmtree

        def stuck_rmtree(path, *args, **kwargs):
            if Path(path) == trash_item:
                raise OSError("directory busy")
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(trash.Path, "rename", cross_device_rename)
        monkeypatch.setattr(trash.shutil, "rmtree", stuck_rmtree)

        assert restore_from_trash(entry, data_dir) is False
        assert trash_item.is_dir(), "unremovable trash source must stay for a retry"
        assert meta_file.exists()
        assert (folder / "a.jpg").read_bytes() == b"\xff\xd8\xff\xe0a"
        leftovers = [p for p in data_dir.rglob(f"{trash.STAGE_PREFIX}*")]
        assert leftovers == [], f"stage artifacts leaked: {leftovers}"


class TestSourceLockProtectsTrashSource:
    """Regression tests for the #557 review blocker: the trash SOURCE.

    The stage lock only protects the staged copy on the destination
    filesystem; ``empty_trash``/``purge_old_trash`` could still delete the
    trash item itself while a cross-device ``copytree`` was reading it,
    letting a partial restore report success after the source was gone.

    The distinguishing signal is now an exclusive ``flock`` on the item's
    ``<item>.meta.json`` (the meta file doubles as the source lock, so no
    extra lock file is needed). The restore holds it for the whole restore;
    maintenance must ACQUIRE it — not merely probe it — before deleting, so
    a held lock skips the item entirely and a crashed restore leaves a free
    lock (the kernel drops flocks on process death) that any sweep can
    reclaim immediately.
    """

    def _make_album(self, data_dir):
        folder = data_dir / "album"
        folder.mkdir()
        (folder / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0a")
        (folder / "sub").mkdir()
        (folder / "sub" / "b.jpg").write_bytes(b"\xff\xd8\xff\xe0b")
        return folder

    def _trash_paths(self, data_dir, entry):
        trash_item = trash.trash_dir(data_dir) / entry
        meta_file = trash.trash_dir(data_dir) / (entry + trash.META_SUFFIX)
        return trash_item, meta_file

    def _hold_meta_lock(self, meta_file):
        """Open the meta file and flock it like an external worker would.

        ``flock`` is treated independently per open file description even
        within one process, so this fd makes the item look in-use to the
        restore/sweep code running in the same test process.
        """
        import fcntl

        fh = open(meta_file, "r")  # noqa: SIM115 - held open to keep the flock live
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh

    def _force_cross_device_restore(self, monkeypatch, data_dir):
        """Make every rename whose source lives under the trash fail EXDEV."""
        real_rename = trash.Path.rename
        trash_root = trash.trash_dir(data_dir).resolve()

        def cross_device_rename(path, destination):
            try:
                resolved = path.resolve()
            except OSError:
                return real_rename(path, destination)
            if trash_root in resolved.parents or resolved == trash_root:
                raise OSError(errno.EXDEV, "cross-device link")
            return real_rename(path, destination)

        monkeypatch.setattr(trash.Path, "rename", cross_device_rename)

    def test_empty_mid_copytree_preserves_trash_source(self, monkeypatch, tmp_path):
        """The #557 data-loss repro: ``empty_trash`` running mid-``copytree``.

        The restore holds the source lock for the whole copy, so the sweep
        must skip the trash item and its meta entirely — the wrapper below
        runs ``empty_trash`` between the real ``copytree`` and the staged
        rename and asserts, mid-copy, that the sweep counted nothing and
        the source still exists. The restore then completes with the full
        tree at its destination and consumes item+meta.
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_album(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item, meta_file = self._trash_paths(data_dir, entry)

        self._force_cross_device_restore(monkeypatch, data_dir)
        real_copytree = trash.shutil.copytree

        def copytree_with_maintenance(src, dest, *args, **kwargs):
            result = real_copytree(src, dest, *args, **kwargs)
            # shutil.copytree recurses through this same attribute; only the
            # top-level call from restore_from_trash passes Path objects.
            if isinstance(dest, Path):
                deleted = trash.empty_trash(data_dir)
                assert deleted == 0, (
                    "empty_trash deleted entries while a restore was copying"
                )
                assert trash_item.is_dir(), "the trash source was deleted mid-copytree"
                assert meta_file.exists(), "the trash metadata was deleted mid-copytree"
            return result

        monkeypatch.setattr(trash.shutil, "copytree", copytree_with_maintenance)

        assert restore_from_trash(entry, data_dir) is True
        assert (folder / "a.jpg").read_bytes() == b"\xff\xd8\xff\xe0a"
        assert (folder / "sub" / "b.jpg").read_bytes() == b"\xff\xd8\xff\xe0b"
        assert not trash_item.exists()
        assert not meta_file.exists()
        assert list_trash(data_dir) == []
        leftovers = [p for p in data_dir.rglob(f"{trash.STAGE_PREFIX}*")]
        assert leftovers == [], f"stage artifacts leaked: {leftovers}"

    def test_maintenance_skips_items_with_externally_held_source_lock(
        self, monkeypatch, tmp_path
    ):
        """Both sweeps skip an item whose meta lock another worker holds.

        While the lock is held, ``empty_trash``/``purge_old_trash`` must
        leave item and meta untouched and count nothing. Once the external
        holder releases, the same sweeps delete normally with the exact
        legacy counts: item and meta each count 1 under ``empty_trash``,
        and the pair counts once under ``purge_old_trash``.
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_album(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item, meta_file = self._trash_paths(data_dir, entry)

        # Make the item look long expired for the purge half of the test.
        old_iso = (trash._utcnow() - timedelta(days=40)).isoformat()
        meta_file.write_text(json.dumps({"original": "album", "deleted_at": old_iso}))

        fh = self._hold_meta_lock(meta_file)
        try:
            assert trash.empty_trash(data_dir) == 0
            assert trash_item.is_dir()
            assert meta_file.exists()
            assert trash.purge_old_trash(data_dir, retention_days=1) == 0
            assert trash_item.is_dir()
            assert meta_file.exists()
        finally:
            fh.close()

        # Released: the item is reclaimable again with legacy counts
        # (empty_trash counts the item and its meta separately).
        assert trash.empty_trash(data_dir) == 2
        assert not trash_item.exists()
        assert not meta_file.exists()

        # Same for purge_old_trash on a fresh expired item.
        folder = self._make_album(data_dir)
        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item, meta_file = self._trash_paths(data_dir, entry)
        old_iso = (trash._utcnow() - timedelta(days=40)).isoformat()
        meta_file.write_text(json.dumps({"original": "album", "deleted_at": old_iso}))

        fh = self._hold_meta_lock(meta_file)
        try:
            assert trash.purge_old_trash(data_dir, retention_days=1) == 0
            assert trash_item.is_dir()
            assert meta_file.exists()
        finally:
            fh.close()

        assert trash.purge_old_trash(data_dir, retention_days=1) == 1
        assert not trash_item.exists()
        assert not meta_file.exists()

    def test_restore_fails_fast_when_source_lock_held(self, monkeypatch, tmp_path):
        """A held source lock makes the restore return False before copying.

        If maintenance (or another worker) holds the item's meta flock, the
        restore must fail fast — before any copy starts — instead of
        racing a concurrent empty/purge. The rename is forced to fail with
        EXDEV so the restore would take the cross-device ``copytree`` path,
        and ``copytree`` records any invocation: on pre-fix code the copy
        runs (and a mid-copy ``empty_trash`` deletes the source); with the
        fix the invocation list stays empty.
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_album(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item, meta_file = self._trash_paths(data_dir, entry)

        self._force_cross_device_restore(monkeypatch, data_dir)
        real_copytree = trash.shutil.copytree
        copy_calls = []

        def must_not_copy(*args, **kwargs):
            copy_calls.append(True)
            raise AssertionError("restore must fail fast on a held source lock")

        monkeypatch.setattr(trash.shutil, "copytree", must_not_copy)

        fh = self._hold_meta_lock(meta_file)
        try:
            assert restore_from_trash(entry, data_dir) is False
            assert copy_calls == [], "restore started copying despite a held source lock"
            # The held item and its metadata are untouched.
            assert trash_item.is_dir()
            assert meta_file.exists()
            assert not folder.exists()
        finally:
            fh.close()

        # Once the lock is free the very same item restores normally — the
        # EXDEV forcing stays active, so this exercises the real cross-device
        # staging path with the genuine copytree restored.
        monkeypatch.setattr(trash.shutil, "copytree", real_copytree)
        assert restore_from_trash(entry, data_dir) is True
        assert (folder / "a.jpg").read_bytes() == b"\xff\xd8\xff\xe0a"
        assert list_trash(data_dir) == []

    def test_released_source_lock_item_deleted_normally(self, monkeypatch, tmp_path):
        """Crash semantics: a released lock leaves the item reclaimable.

        A crashed owner's flock is dropped by the kernel; holding the lock
        and releasing it must leave item+meta in exactly the state any
        sweep treats as ordinary trash. Both entry points must delete it
        immediately, with the legacy counts.
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_album(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item, meta_file = self._trash_paths(data_dir, entry)

        fh = self._hold_meta_lock(meta_file)
        fh.close()  # kernel releases the flock: owner "died"

        assert trash.empty_trash(data_dir) == 2
        assert not trash_item.exists()
        assert not meta_file.exists()

        # Same for purge_old_trash on a fresh expired item.
        folder = self._make_album(data_dir)
        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item, meta_file = self._trash_paths(data_dir, entry)
        old_iso = (trash._utcnow() - timedelta(days=40)).isoformat()
        meta_file.write_text(json.dumps({"original": "album", "deleted_at": old_iso}))

        fh = self._hold_meta_lock(meta_file)
        fh.close()
        assert trash.purge_old_trash(data_dir, retention_days=1) == 1
        assert not trash_item.exists()
        assert not meta_file.exists()

    def test_stale_meta_without_item_is_reclaimed_by_empty(self, monkeypatch, tmp_path):
        """A meta file with no paired item is stale: unlink it, count 1.

        Deleting the meta before the item would strip the item of its lock
        file, so a meta whose item still exists is left for the item's own
        iteration; a lone meta needs no lock check.
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        td = trash.trash_dir(data_dir)
        stale = td / "ghost.txt.meta.json"
        stale.write_text(json.dumps({"original": "ghost.txt"}))

        assert trash.empty_trash(data_dir) == 1
        assert not stale.exists()

    def test_entry_named_exactly_meta_suffix_is_deleted_as_item(
        self, monkeypatch, tmp_path
    ):
        """A trashed entry literally named ".meta.json" is an item, not a meta.

        Stripping META_SUFFIX would leave an empty paired name, so this
        entry must take the ordinary item path (deleted and counted) rather
        than being misclassified as a stale meta and skipped forever.
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        td = trash.trash_dir(data_dir)
        odd = td / ".meta.json"
        odd.write_bytes(b"data")

        assert trash.empty_trash(data_dir) == 1
        assert not odd.exists()


class TestRestorePathTraversal:
    """Regression tests for path traversal in restore_from_trash (#382).

    The <path:> converter in Flask allows / and .., so a crafted item_name
    could escape the trash directory. These tests verify that traversal
    attempts are rejected.
    """

    def test_restore_rejects_dotdot_in_name(self, monkeypatch, tmp_path):
        from trash import restore_from_trash

        client, data_dir = build_client(monkeypatch, tmp_path)

        # Create a file outside the trash dir that an attacker might target
        secret = data_dir / "secret.txt"
        secret.write_text("top_secret")

        with pytest.raises(ValueError, match="Invalid trash item name"):
            restore_from_trash("../secret.txt", data_dir)

        # Verify the secret file was not moved
        assert secret.exists()
        assert secret.read_text() == "top_secret"

    def test_restore_rejects_double_dotdot(self, monkeypatch, tmp_path):
        from trash import restore_from_trash

        client, data_dir = build_client(monkeypatch, tmp_path)

        # Create a file outside the data dir entirely
        external = tmp_path / "external_secret.txt"
        external.write_text("external_data")

        with pytest.raises(ValueError, match="Invalid trash item name"):
            restore_from_trash("../../external_secret.txt", data_dir)

        # Verify the external file was not touched
        assert external.exists()
        assert external.read_text() == "external_data"

    def test_restore_rejects_slash_in_name(self, monkeypatch, tmp_path):
        from trash import restore_from_trash

        client, data_dir = build_client(monkeypatch, tmp_path)

        with pytest.raises(ValueError, match="Invalid trash item name"):
            restore_from_trash("subdir/file.txt", data_dir)

    def test_restore_rejects_backslash_in_name(self, monkeypatch, tmp_path):
        from trash import restore_from_trash

        client, data_dir = build_client(monkeypatch, tmp_path)

        with pytest.raises(ValueError, match="Invalid trash item name"):
            restore_from_trash("subdir\\file.txt", data_dir)

    def test_restore_rejects_mixed_traversal(self, monkeypatch, tmp_path):
        from trash import restore_from_trash

        client, data_dir = build_client(monkeypatch, tmp_path)

        with pytest.raises(ValueError, match="Invalid trash item name"):
            restore_from_trash("foo/../../../etc/passwd", data_dir)

    def test_restore_rejects_null_byte_in_name(self, monkeypatch, tmp_path):
        """An embedded null byte must be rejected before any path syscall.

        ``Path("bad\\x00name").exists()`` would raise a bare
        ``ValueError: embedded null byte`` from os.stat; the explicit guard
        raises the canonical "Invalid trash item name" error instead and
        never touches the filesystem.
        """
        from trash import restore_from_trash, trash_dir

        client, data_dir = build_client(monkeypatch, tmp_path)

        td = trash_dir(data_dir)
        with pytest.raises(ValueError, match="Invalid trash item name"):
            restore_from_trash("bad\x00name", data_dir)

        # Nothing was read, created, or deleted in the trash directory.
        assert list(td.iterdir()) == []

    def test_restore_rejects_malicious_meta_original(self, monkeypatch, tmp_path):
        """Even if item_name is valid, a crafted .meta.json 'original' must not escape."""
        from trash import restore_from_trash, trash_dir

        client, data_dir = build_client(monkeypatch, tmp_path)

        td = trash_dir(data_dir)
        td.mkdir(parents=True, exist_ok=True)

        # Create a valid-looking item in trash with a malicious meta
        fake_item = td / "fake_item.txt"
        fake_item.write_text("harmless")
        meta_file = td / "fake_item.txt.meta.json"
        meta_file.write_text(
            json.dumps({"original": "../../../etc/passwd", "deleted_at": 0})
        )

        with pytest.raises(ValueError, match="Invalid original path"):
            restore_from_trash("fake_item.txt", data_dir)

    def test_restore_rejects_meta_original_dotdot(self, monkeypatch, tmp_path):
        """Meta 'original' with .. segments must be rejected."""
        from trash import restore_from_trash, trash_dir

        client, data_dir = build_client(monkeypatch, tmp_path)

        td = trash_dir(data_dir)
        td.mkdir(parents=True, exist_ok=True)

        fake_item = td / "fake2.txt"
        fake_item.write_text("harmless")
        meta_file = td / "fake2.txt.meta.json"
        meta_file.write_text(
            json.dumps({"original": "../outside_trash.txt", "deleted_at": 0})
        )

        with pytest.raises(ValueError, match="Invalid original path"):
            restore_from_trash("fake2.txt", data_dir)


class TestRestoreSymlinkSafety:
    """Regression tests for symlink inlining on restore (#455).

    shutil.copytree defaults to symlinks=False, which follows symlinks and
    copies target contents as regular files. A trashed directory containing a
    symlink (e.g. passwd_link.png -> /etc/passwd) must therefore be refused
    on restore so external-target bytes never land under DATA_FOLDER.
    """

    def test_restore_refuses_directory_containing_symlink(self, monkeypatch, tmp_path):
        from trash import list_trash, move_to_trash, restore_from_trash

        client, data_dir = build_client(monkeypatch, tmp_path)

        # External secret that a symlink inside the trashed dir would expose
        secret = tmp_path / "secret_target.txt"
        secret.write_text("TOP_SECRET_BYTES")

        folder = data_dir / "photos"
        folder.mkdir()
        (folder / "real.jpg").write_bytes(b"\xff\xd8\xff\xe0real")
        (folder / "passwd_link.png").symlink_to(secret)

        assert move_to_trash(folder, data_dir) is True
        assert not folder.exists()

        trash_items = list_trash(data_dir)
        trash_entry = trash_items[0]["name"]

        with pytest.raises(ValueError, match="contains symlinks"):
            restore_from_trash(trash_entry, data_dir)

        # Nothing may have been restored into DATA_FOLDER
        assert not folder.exists()
        for path in data_dir.rglob("*"):
            if path.is_file() and not path.is_symlink():
                assert path.read_bytes() != b"TOP_SECRET_BYTES"

        # The trashed item must remain in trash (not consumed by the refusal)
        assert any(t["name"] == trash_entry for t in list_trash(data_dir))

    def test_restore_refuses_symlinked_directory_item(self, monkeypatch, tmp_path):
        """A trashed item that is itself a symlink must also be refused."""
        from trash import restore_from_trash, trash_dir

        client, data_dir = build_client(monkeypatch, tmp_path)

        external = tmp_path / "external_dir"
        external.mkdir()
        (external / "leak.png").write_bytes(b"EXTERNAL")

        td = trash_dir(data_dir)
        link = td / "linked_dir"
        link.symlink_to(external)
        link.with_name(link.name + trash.META_SUFFIX).write_text(
            json.dumps({"original": "linked_dir"})
        )

        with pytest.raises(ValueError, match="contains symlinks"):
            restore_from_trash("linked_dir", data_dir)

        assert not (data_dir / "linked_dir").exists()

    def test_restore_symlink_free_directory_still_works(self, monkeypatch, tmp_path):
        """Legitimate symlink-free restores must not regress."""
        from trash import move_to_trash, restore_from_trash

        client, data_dir = build_client(monkeypatch, tmp_path)

        folder = data_dir / "clean"
        folder.mkdir()
        (folder / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0a")
        (folder / "sub").mkdir()
        (folder / "sub" / "b.jpg").write_bytes(b"\xff\xd8\xff\xe0b")

        assert move_to_trash(folder, data_dir) is True
        entry = [t["name"] for t in trash.list_trash(data_dir)][0]

        assert restore_from_trash(entry, data_dir) is True
        assert (folder / "a.jpg").read_bytes() == b"\xff\xd8\xff\xe0a"
        assert (folder / "sub" / "b.jpg").read_bytes() == b"\xff\xd8\xff\xe0b"

    def test_cross_device_trash_fallback_preserves_symlinks(self, monkeypatch, tmp_path):
        """The copytree fallback in move_to_trash must not inline symlink targets."""
        from trash import move_to_trash, trash_dir

        client, data_dir = build_client(monkeypatch, tmp_path)

        secret = tmp_path / "secret.txt"
        secret.write_text("SECRET")
        source = data_dir / "album"
        source.mkdir()
        (source / "photo.txt").write_text("photo")
        (source / "link.txt").symlink_to(secret)

        real_rename = trash.Path.rename

        def cross_device_rename(path, destination):
            if path == source:
                raise OSError("cross-device")
            return real_rename(path, destination)

        monkeypatch.setattr(trash.Path, "rename", cross_device_rename)
        assert move_to_trash(source, data_dir) is True

        trashed = next(
            p for p in trash_dir(data_dir).iterdir()
            if p.is_dir() and not p.name.endswith(trash.META_SUFFIX)
        )
        link = trashed / "link.txt"
        assert link.is_symlink(), "fallback copy must preserve symlinks as links"
        # The link must still point at the external target, not a copied file
        assert link.resolve() == secret.resolve()


class TestTrashListIncludesDirs:
    """Verify trash listing correctly shows directories with sizes."""

    def test_list_trash_shows_directory_size(self, monkeypatch, tmp_path):
        from trash import list_trash, move_to_trash

        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = data_dir / "listed_folder"
        folder.mkdir()
        (folder / "big.jpg").write_bytes(b"\xff\xd8\xff\xe0" * 100)

        move_to_trash(folder, data_dir)
        trash_items = list_trash(data_dir)

        assert len(trash_items) == 1
        entry = trash_items[0]
        assert trash_items[0]["original"] == "listed_folder"
        assert entry["size"] > 0


class TestConflictingNames:
    """Verify handling of name collisions in trash."""

    def test_trash_handles_name_collision(self, monkeypatch, tmp_path):
        from trash import list_trash, move_to_trash

        client, data_dir = build_client(monkeypatch, tmp_path)

        for _ in range(2):
            folder = data_dir / "collision_test"
            folder.mkdir()
            (folder / "file.jpg").write_bytes(b"fakesize")
            move_to_trash(folder, data_dir)
            time.sleep(0.01)

        trash_items = list_trash(data_dir)
        assert len(trash_items) == 2
        originals = {t["original"] for t in trash_items}
        assert "collision_test" in originals


class TestDirSizeSymlinkConsistency:
    """Verify dir_size() consistently skips symlinks (regression for #266).

    Previously, _dir_size() did not skip symlinks while dir_size() did.
    After standardization, all size calculations should use dir_size() which
    skips symlinks for security.
    """

    def test_dir_size_skips_symlinks(self, monkeypatch, tmp_path):
        from trash import dir_size

        # Create a directory with a real file and a symlink to a large external file
        folder = tmp_path / "test_folder"
        folder.mkdir()
        (folder / "real.txt").write_bytes(b"x" * 100)

        # Create an external file outside the folder
        external = tmp_path / "external_large.txt"
        external.write_bytes(b"y" * 10000)

        # Create a symlink inside the folder pointing to the external file
        (folder / "link.txt").symlink_to(external)

        # dir_size should only count the real file, not follow symlinks
        size = dir_size(folder)
        assert size == 100, f"Expected 100 bytes (real file only), got {size}"

    def test_move_to_trash_uses_dir_size_not_private(self, monkeypatch, tmp_path):
        """Verify move_to_trash records size using symlink-safe dir_size()."""
        from trash import list_trash, move_to_trash

        client, data_dir = build_client(monkeypatch, tmp_path)

        folder = data_dir / "symlink_test"
        folder.mkdir()
        (folder / "real.txt").write_bytes(b"a" * 50)

        # Create external file and symlink inside the folder
        external = data_dir / "external.txt"
        external.write_bytes(b"b" * 5000)
        (folder / "link.txt").symlink_to(external)

        move_to_trash(folder, data_dir)

        trash_items = list_trash(data_dir)
        assert len(trash_items) == 1
        # Size should be 50 (real file only), not 5050 (which would include symlink target)
        assert trash_items[0]["size"] == 50, (
            f"Expected 50 bytes (symlinks skipped), got {trash_items[0]['size']}"
        )


class TestTrashPurgeValidation:
    """Verify trash_purge() validates days input and provides user feedback (regression for #364)."""

    @pytest.fixture(autouse=True)
    def reset_rate_limiters(self):
        """Reset rate limiter state before each test to avoid cross-test interference."""
        from security import FALLBACK_LIMITER, _primary_limiter

        FALLBACK_LIMITER.reset()
        if _primary_limiter is not None:
            with contextlib.suppress(Exception):
                _primary_limiter._client.flushdb()

    def test_trash_purge_invalid_days_logs_warning_and_flashes(self, monkeypatch, tmp_path, caplog):
        client, data_dir = build_client(monkeypatch, tmp_path)

        # Create a trashed item to purge
        folder = data_dir / "purge_me"
        folder.mkdir()
        (folder / "file.jpg").write_bytes(b"fakesize")
        from trash import move_to_trash

        move_to_trash(folder, data_dir)

        # Login first
        login_resp = client.get("/login")
        csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', login_resp.data.decode())
        assert csrf_match, "CSRF token not found in login form"
        login_csrf = csrf_match.group(1)
        auth_resp = client.post(
            "/auth",
            data={"password": "pass123", "next": "/", "csrf_token": login_csrf},
            follow_redirects=False,
        )
        assert auth_resp.status_code == 302

        # Get CSRF token from trash page
        trash_resp = client.get("/trash")
        csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', trash_resp.data.decode())
        assert csrf_match, "CSRF token not found on trash page"
        csrf_token = csrf_match.group(1)

        # Submit invalid days value
        with caplog.at_level("WARNING"):
            response = client.post(
                "/trash/purge",
                data={"csrf_token": csrf_token, "days": "abc"},
                follow_redirects=True,
            )

        assert response.status_code == 200
        # Verify flash message is present in the rendered page
        assert b"Invalid retention period" in response.data
        # Verify a warning was logged about the invalid input
        assert any("Invalid 'days' value" in record.message for record in caplog.records)

    def test_trash_purge_valid_days(self, monkeypatch, tmp_path):
        client, data_dir = build_client(monkeypatch, tmp_path)

        # Create a trashed item
        folder = data_dir / "purge_me2"
        folder.mkdir()
        (folder / "file.jpg").write_bytes(b"fakesize")
        from trash import move_to_trash

        move_to_trash(folder, data_dir)

        # Login first
        login_resp = client.get("/login")
        csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', login_resp.data.decode())
        assert csrf_match, "CSRF token not found in login form"
        login_csrf = csrf_match.group(1)
        auth_resp = client.post(
            "/auth",
            data={"password": "pass123", "next": "/", "csrf_token": login_csrf},
            follow_redirects=False,
        )
        assert auth_resp.status_code == 302

        # Get CSRF token from trash page
        trash_resp = client.get("/trash")
        csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', trash_resp.data.decode())
        assert csrf_match, "CSRF token not found on trash page"
        csrf_token = csrf_match.group(1)

        response = client.post(
            "/trash/purge",
            data={"csrf_token": csrf_token, "days": "7"},
            follow_redirects=False,
        )
        assert response.status_code == 302
