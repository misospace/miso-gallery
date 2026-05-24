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

import re
import time

from conftest import build_client


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
