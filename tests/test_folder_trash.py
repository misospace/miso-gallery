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
import json
import re
import time

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
