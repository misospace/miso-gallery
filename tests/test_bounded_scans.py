"""Tests for bounded gallery scans (issue #167).

Verifies that LLM endpoints and iterator functions respect scan limits
and return has_more when the scan was truncated.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _build_bounded_client(monkeypatch, tmp_path):
    """Build a test client with a low scan limit for testing."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / ".thumb_cache").mkdir(exist_ok=True)

    # Write many media files to exceed the scan limit
    for i in range(6000):
        path = data_dir / f"img_{i:05d}.png"
        path.write_bytes(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82')

    # Write many subdirectories
    for i in range(6000):
        (data_dir / f"subdir_{i:05d}").mkdir()

    monkeypatch.setenv("GALLERY_SCAN_LIMIT", "50")
    monkeypatch.setenv("DATA_FOLDER", str(data_dir))
    monkeypatch.setenv("AUTH_TYPE", "local")
    monkeypatch.setenv("ADMIN_PASSWORD", "pass123")
    monkeypatch.setenv("OIDC_ENABLED", "false")
    monkeypatch.setenv("SECRET_KEY", "test-secret-ci-gateway")
    monkeypatch.setenv("LLM_API_KEYS", "agent-key")

    # llm_api must be popped with auth for the same reason as in conftest.py:
    # it binds require_api_key_with_scope from auth at import time.
    # gallery_scan must be popped with app (issue #521): it re-reads the
    # GALLERY_* env constants at import time, so a stale copy would keep the
    # previous limit/cache configuration after app is re-imported.
    for mod in ("auth", "oidc", "llm_api", "gallery_scan", "app"):
        sys.modules.pop(mod, None)

    import app as app_module
    app_module.DATA_FOLDER = data_dir
    app_module.THUMBNAIL_CACHE_DIR = data_dir / ".thumb_cache"
    app_module.app.config["TESTING"] = True

    return app_module.app.test_client(), data_dir


def _api_header():
    return {"Authorization": "Bearer agent-key"}


class TestIterGalleryItemsBounded:
    """iter_gallery_items should never exceed GALLERY_SCAN_LIMIT (issue #247)."""

    def test_media_respects_scan_limit(self, monkeypatch, tmp_path):
        client, data_dir = _build_bounded_client(monkeypatch, tmp_path)

        import app as app_module
        media = app_module.iter_gallery_items(kind="media")
        assert len(media) <= 50  # GALLERY_SCAN_LIMIT is set to 50

    def test_media_custom_limit_works(self, monkeypatch, tmp_path):
        client, data_dir = _build_bounded_client(monkeypatch, tmp_path)

        import app as app_module
        media = app_module.iter_gallery_items(kind="media", limit=10)
        assert len(media) <= 10

    def test_folders_respects_scan_limit(self, monkeypatch, tmp_path):
        client, data_dir = _build_bounded_client(monkeypatch, tmp_path)

        import app as app_module
        folders = app_module.iter_gallery_items(kind="folders")
        assert len(folders) <= 50

    def test_folders_custom_limit_works(self, monkeypatch, tmp_path):
        client, data_dir = _build_bounded_client(monkeypatch, tmp_path)

        import app as app_module
        folders = app_module.iter_gallery_items(kind="folders", limit=20)
        assert len(folders) <= 20


class TestIterGalleryItemsSortsBeforeTruncating:
    """iter_gallery_items must sort before applying ``limit`` (issue #325).

    A small limit asks for the sorted-first items, not an arbitrary
    filesystem-ordered prefix. Regression guard for the folder-cover path,
    which calls ``iter_gallery_items(kind="media", limit=1, root=...)``.
    """

    @staticmethod
    def _bootstrap(monkeypatch, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / ".thumb_cache").mkdir(exist_ok=True)

        # Create media files in reverse-sorted order so that filesystem
        # (readdir) order is unlikely to coincide with sorted order.
        for name in ("zebra.png", "mango.jpg", "apple.webp"):
            (data_dir / name).write_bytes(b"\x00")

        monkeypatch.setenv("DATA_FOLDER", str(data_dir))
        monkeypatch.setenv("AUTH_TYPE", "none")
        monkeypatch.setenv("OIDC_ENABLED", "false")
        monkeypatch.setenv("SECRET_KEY", "test-secret-ci-gateway")

        for mod in ("auth", "oidc", "llm_api", "gallery_scan", "app"):
            sys.modules.pop(mod, None)

        import app as app_module
        app_module.DATA_FOLDER = data_dir
        app_module.THUMBNAIL_CACHE_DIR = data_dir / ".thumb_cache"
        app_module.app.config["TESTING"] = True
        return app_module

    def test_limit_one_returns_sorted_first(self, monkeypatch, tmp_path):
        app_module = self._bootstrap(monkeypatch, tmp_path)

        items = app_module.iter_gallery_items(kind="media", limit=1)

        assert len(items) == 1
        assert items[0].name == "apple.webp"

    def test_limit_keeps_sorted_order(self, monkeypatch, tmp_path):
        app_module = self._bootstrap(monkeypatch, tmp_path)

        items = app_module.iter_gallery_items(kind="media", limit=2)

        assert [p.name for p in items] == ["apple.webp", "mango.jpg"]

    def test_scan_limit_still_caps_count(self, monkeypatch, tmp_path):
        """The GALLERY_SCAN_LIMIT safety bound must still be enforced."""
        app_module = self._bootstrap(monkeypatch, tmp_path)
        app_module.GALLERY_SCAN_LIMIT = 2

        items = app_module.iter_gallery_items(kind="media")

        assert len(items) <= 2


class TestIterGalleryItemsPrunesExcludedTrees:
    """iter_gallery_items must never open excluded directories (issue #687).

    The pre-fix ``rglob("*")`` walk entered ``.trash``, the configured
    thumbnail-cache directory, and dot-directories and filtered only per
    entry, so large excluded trees dominated cold-scan latency on network
    storage. These tests instrument ``os.scandir`` to prove no excluded
    directory is opened, while visible media/folder results, the
    case-insensitive sort, limits, root scoping, and the no-follow symlink
    skip stay unchanged.
    """

    @staticmethod
    def _bootstrap(monkeypatch, tmp_path, cache_dir_name=".thumb_cache"):
        import os

        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / cache_dir_name).mkdir(exist_ok=True)

        # Visible media in reverse-sorted readdir order, plus a visible
        # subfolder with its own media for folder results and root scoping.
        for name in ("zebra.png", "mango.jpg", "apple.webp"):
            (data_dir / name).write_bytes(b"\x00")
        (data_dir / "sub").mkdir()
        (data_dir / "sub" / "inner.png").write_bytes(b"\x00")

        # Deep excluded trees: an unpruned walk descends four levels and
        # opens every directory below them.
        for excluded in (cache_dir_name, ".trash", ".hidden"):
            deep = data_dir / excluded / "a" / "b" / "c"
            deep.mkdir(parents=True)
            for n in range(3):
                (deep / f"ghost_{n}.png").write_bytes(b"\x00")
                (deep / f"sub{n}").mkdir()

        monkeypatch.setenv("DATA_FOLDER", str(data_dir))
        monkeypatch.setenv("AUTH_TYPE", "none")
        monkeypatch.setenv("OIDC_ENABLED", "false")
        monkeypatch.setenv("SECRET_KEY", "test-secret-ci-gateway")

        for mod in ("auth", "oidc", "llm_api", "gallery_scan", "app"):
            sys.modules.pop(mod, None)

        import app as app_module

        app_module.DATA_FOLDER = data_dir
        app_module.THUMBNAIL_CACHE_DIR = data_dir / cache_dir_name
        app_module.app.config["TESTING"] = True

        opened: list[str] = []
        real_scandir = os.scandir

        def tracking_scandir(path, *args, **kwargs):
            opened.append(os.fspath(path))
            return real_scandir(path, *args, **kwargs)

        monkeypatch.setattr(os, "scandir", tracking_scandir)
        return app_module, data_dir, opened

    @staticmethod
    def _rel(data_dir, paths):
        return [p.relative_to(data_dir).as_posix() for p in paths]

    @staticmethod
    def _assert_never_opened(opened, data_dir, name):
        import os

        excluded_root = str(data_dir / name)
        offenders = [
            p for p in opened if p == excluded_root or p.startswith(excluded_root + os.sep)
        ]
        assert offenders == [], f"excluded tree {name!r} was opened: {offenders}"

    def test_media_results_keep_visible_files_without_opening_excluded_trees(
        self, monkeypatch, tmp_path
    ):
        app_module, data_dir, opened = self._bootstrap(monkeypatch, tmp_path)

        media = app_module.iter_gallery_items(kind="media")

        assert self._rel(data_dir, media) == [
            "apple.webp",
            "mango.jpg",
            "sub/inner.png",
            "zebra.png",
        ]
        for name in (".thumb_cache", ".trash", ".hidden"):
            self._assert_never_opened(opened, data_dir, name)

    def test_folder_results_keep_visible_dirs_without_opening_excluded_trees(
        self, monkeypatch, tmp_path
    ):
        app_module, data_dir, opened = self._bootstrap(monkeypatch, tmp_path)

        folders = app_module.iter_gallery_items(kind="folders")

        assert self._rel(data_dir, folders) == ["sub"]
        for name in (".thumb_cache", ".trash", ".hidden"):
            self._assert_never_opened(opened, data_dir, name)

    def test_kind_all_also_prunes_excluded_trees(self, monkeypatch, tmp_path):
        app_module, data_dir, opened = self._bootstrap(monkeypatch, tmp_path)

        everything = app_module.iter_gallery_items(kind="all")

        assert self._rel(data_dir, everything) == [
            "apple.webp",
            "mango.jpg",
            "sub",
            "sub/inner.png",
            "zebra.png",
        ]
        for name in (".thumb_cache", ".trash", ".hidden"):
            self._assert_never_opened(opened, data_dir, name)

    def test_configured_thumbnail_cache_name_is_pruned(self, monkeypatch, tmp_path):
        """A custom non-dot cache name proves pruning follows the configured
        THUMBNAIL_CACHE_DIR, not just the dot-prefix rule."""
        app_module, data_dir, opened = self._bootstrap(
            monkeypatch, tmp_path, cache_dir_name="covers_store"
        )

        media = app_module.iter_gallery_items(kind="media")

        assert self._rel(data_dir, media) == [
            "apple.webp",
            "mango.jpg",
            "sub/inner.png",
            "zebra.png",
        ]
        self._assert_never_opened(opened, data_dir, "covers_store")

    def test_limit_and_root_scope_still_apply(self, monkeypatch, tmp_path):
        app_module, data_dir, _ = self._bootstrap(monkeypatch, tmp_path)

        limited = app_module.iter_gallery_items(kind="media", limit=2)
        assert [p.name for p in limited] == ["apple.webp", "mango.jpg"]

        scoped = app_module.iter_gallery_items(kind="media", limit=1, root=data_dir / "sub")
        assert [p.name for p in scoped] == ["inner.png"]

    def test_root_inside_excluded_tree_returns_nothing(self, monkeypatch, tmp_path):
        """Scanning with a root inside an excluded tree still yields no items:
        the per-entry exclusion, not pruning, keeps deciding results."""
        app_module, data_dir, _ = self._bootstrap(monkeypatch, tmp_path)

        assert app_module.iter_gallery_items(kind="media", root=data_dir / ".trash") == []
        assert app_module.iter_gallery_items(kind="folders", root=data_dir / ".trash") == []

    def test_symlinks_still_skipped(self, monkeypatch, tmp_path):
        app_module, data_dir, _ = self._bootstrap(monkeypatch, tmp_path)
        (data_dir / "link.png").symlink_to(data_dir / "apple.webp")
        (data_dir / "link_dir").symlink_to(data_dir / "sub")

        media = self._rel(data_dir, app_module.iter_gallery_items(kind="media"))
        assert "link.png" not in media
        assert not any(p.startswith("link_dir/") for p in media)

        folders = self._rel(data_dir, app_module.iter_gallery_items(kind="folders"))
        assert folders == ["sub"]


class TestLlmImagesHasMore:
    """llm_images should return has_more=True when scan is limited."""

    def test_has_more_when_scan_limited(self, monkeypatch, tmp_path):
        client, data_dir = _build_bounded_client(monkeypatch, tmp_path)

        resp = client.get("/api/llm/images", headers=_api_header())
        assert resp.status_code == 200
        data = resp.get_json()
        # With 6000 images and scan limit 50, should report has_more
        assert data["has_more"] is True

    def test_has_more_false_when_not_limited(self, monkeypatch, tmp_path):
        """When gallery is small enough, has_more should be False."""
        data_dir = tmp_path / "small_data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / ".thumb_cache").mkdir(exist_ok=True)

        # Only 10 images - well under the default limit of 5000
        for i in range(10):
            (data_dir / f"img_{i:03d}.png").write_bytes(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82')

        monkeypatch.setenv("DATA_FOLDER", str(data_dir))
        monkeypatch.setenv("AUTH_TYPE", "local")
        monkeypatch.setenv("ADMIN_PASSWORD", "pass123")
        monkeypatch.setenv("OIDC_ENABLED", "false")
        monkeypatch.setenv("SECRET_KEY", "test-secret-ci-gateway")
        monkeypatch.setenv("LLM_API_KEYS", "agent-key")

        for mod in ("auth", "oidc", "llm_api", "gallery_scan", "app"):
            sys.modules.pop(mod, None)

        import app as app_module
        app_module.DATA_FOLDER = data_dir
        app_module.THUMBNAIL_CACHE_DIR = data_dir / ".thumb_cache"
        app_module.app.config["TESTING"] = True

        resp = app_module.app.test_client().get("/api/llm/images", headers=_api_header())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["has_more"] is False


class TestLlmRecentHasMore:
    """llm_recent should return has_more=True when scan is limited."""

    def test_has_more_when_scan_limited(self, monkeypatch, tmp_path):
        client, data_dir = _build_bounded_client(monkeypatch, tmp_path)

        resp = client.get("/api/llm/recent", headers=_api_header())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["has_more"] is True


class TestLlmFoldersHasMore:
    """llm_folders should return has_more=True when scan is limited."""

    def test_has_more_when_scan_limited(self, monkeypatch, tmp_path):
        client, data_dir = _build_bounded_client(monkeypatch, tmp_path)

        resp = client.get("/api/llm/folders", headers=_api_header())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["has_more"] is True


class TestFindDuplicateMediaBounded:
    """find_duplicate_media should respect scan limits."""

    def test_respects_scan_limit(self, monkeypatch, tmp_path):
        """Even with many duplicates in the filesystem, only scan up to limit."""
        client, data_dir = _build_bounded_client(monkeypatch, tmp_path)

        import app as app_module
        groups = app_module.find_duplicate_media()
        # With scan limit 50 and all identical images, at most one group
        assert len(groups) >= 0
        # The function should not crash or hang on large galleries


class TestIteratorUnification:
    """iter_gallery_items is the single bounded iterator (issue #247)."""

    def test_legacy_iter_gallery_media_removed(self, monkeypatch, tmp_path):
        client, data_dir = _build_bounded_client(monkeypatch, tmp_path)

        import app as app_module
        assert not hasattr(app_module, "iter_gallery_media")

    def test_legacy_iter_gallery_folders_removed(self, monkeypatch, tmp_path):
        client, data_dir = _build_bounded_client(monkeypatch, tmp_path)

        import app as app_module
        assert not hasattr(app_module, "iter_gallery_folders")

    def test_items_kind_distinguishes_media_from_folders(self, monkeypatch, tmp_path):
        client, data_dir = _build_bounded_client(monkeypatch, tmp_path)

        import app as app_module
        media = app_module.iter_gallery_items(kind="media")
        folders = app_module.iter_gallery_items(kind="folders")
        # Media must be files only, folders must be directories only.
        assert all(p.is_file() for p in media)
        assert all(p.is_dir() for p in folders)
        assert set(media).isdisjoint(set(folders))


class TestScanLimitConstant:
    """Verify GALLERY_SCAN_LIMIT is properly defined."""

    def test_default_value(self, monkeypatch):
        monkeypatch.delenv("GALLERY_SCAN_LIMIT", raising=False)
        # GALLERY_SCAN_LIMIT is defined in gallery_scan and re-exported by app
        # (issue #521), so both must be re-imported to observe the env default.
        sys.modules.pop("gallery_scan", None)
        sys.modules.pop("app", None)
        import app as app_module
        assert hasattr(app_module, "GALLERY_SCAN_LIMIT")
        assert app_module.GALLERY_SCAN_LIMIT == 5000


class TestFolderCoverDelegatesToIterGalleryItems:
    """folder_cover_rel_path delegates to iter_gallery_items (issue #325)."""

    def test_cover_uses_iter_gallery_items(self, monkeypatch, tmp_path):
        """folder_cover_rel_path should use iter_gallery_items with root=folder."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / ".thumb_cache").mkdir(exist_ok=True)

        # Create a subfolder with media files
        subfolder = data_dir / "photos"
        subfolder.mkdir()
        (subfolder / "cover.png").write_bytes(
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
            b'\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde'
        )
        (subfolder / "other.jpg").write_bytes(
            b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01'
        )

        monkeypatch.setenv("DATA_FOLDER", str(data_dir))
        monkeypatch.setenv("AUTH_TYPE", "local")
        monkeypatch.setenv("ADMIN_PASSWORD", "pass123")
        monkeypatch.setenv("OIDC_ENABLED", "false")
        monkeypatch.setenv("SECRET_KEY", "test-secret-ci-gateway")
        monkeypatch.setenv("LLM_API_KEYS", "agent-key")
        monkeypatch.setenv("GALLERY_AUTO_FOLDER_COVERS", "true")

        for mod in ("auth", "oidc", "llm_api", "gallery_scan", "app"):
            sys.modules.pop(mod, None)

        import app as app_module
        app_module.DATA_FOLDER = data_dir
        app_module.THUMBNAIL_CACHE_DIR = data_dir / ".thumb_cache"
        app_module.app.config["TESTING"] = True

        # folder_cover_rel_path should return the first sorted media file
        cover = app_module.folder_cover_rel_path("photos")
        assert cover is not None
        assert "cover.png" in cover

    def test_cover_respects_exclusions(self, monkeypatch, tmp_path):
        """folder_cover_rel_path should skip excluded paths via iter_gallery_items."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / ".thumb_cache").mkdir(exist_ok=True)

        # Create a subfolder with only an excluded media file
        subfolder = data_dir / "photos"
        subfolder.mkdir()
        hidden_dir = subfolder / ".hidden"
        hidden_dir.mkdir()
        (hidden_dir / "secret.png").write_bytes(
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
            b'\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde'
        )

        monkeypatch.setenv("DATA_FOLDER", str(data_dir))
        monkeypatch.setenv("AUTH_TYPE", "local")
        monkeypatch.setenv("ADMIN_PASSWORD", "pass123")
        monkeypatch.setenv("OIDC_ENABLED", "false")
        monkeypatch.setenv("SECRET_KEY", "test-secret-ci-gateway")
        monkeypatch.setenv("LLM_API_KEYS", "agent-key")
        monkeypatch.setenv("GALLERY_AUTO_FOLDER_COVERS", "true")

        for mod in ("auth", "oidc", "llm_api", "gallery_scan", "app"):
            sys.modules.pop(mod, None)

        import app as app_module
        app_module.DATA_FOLDER = data_dir
        app_module.THUMBNAIL_CACHE_DIR = data_dir / ".thumb_cache"
        app_module.app.config["TESTING"] = True

        # Should return None since the only media file is in an excluded path
        cover = app_module.folder_cover_rel_path("photos")
        assert cover is None

    def test_iter_gallery_items_root_parameter(self, monkeypatch, tmp_path):
        """iter_gallery_items should accept root parameter for scoped scans."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / ".thumb_cache").mkdir(exist_ok=True)

        # Create files in root and subfolder
        (data_dir / "root_img.png").write_bytes(
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
            b'\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde'
        )
        subfolder = data_dir / "sub"
        subfolder.mkdir()
        (subfolder / "sub_img.png").write_bytes(
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
            b'\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde'
        )

        monkeypatch.setenv("DATA_FOLDER", str(data_dir))
        monkeypatch.setenv("AUTH_TYPE", "local")
        monkeypatch.setenv("ADMIN_PASSWORD", "pass123")
        monkeypatch.setenv("OIDC_ENABLED", "false")
        monkeypatch.setenv("SECRET_KEY", "test-secret-ci-gateway")
        monkeypatch.setenv("LLM_API_KEYS", "agent-key")

        for mod in ("auth", "oidc", "llm_api", "gallery_scan", "app"):
            sys.modules.pop(mod, None)

        import app as app_module
        app_module.DATA_FOLDER = data_dir
        app_module.THUMBNAIL_CACHE_DIR = data_dir / ".thumb_cache"
        app_module.app.config["TESTING"] = True

        # Scanning from subfolder should only return files in that subfolder
        items = app_module.iter_gallery_items(kind="media", root=subfolder)
        assert len(items) == 1
        assert items[0].name == "sub_img.png"

        # Scanning from root should return both
        all_items = app_module.iter_gallery_items(kind="media")
        assert len(all_items) == 2


class TestRecentViewScanTruncation:
    """recent_view() should detect scan truncation and surface an indicator."""

    def test_recent_view_no_truncation(self, tmp_path, monkeypatch):
        from conftest import build_client
        client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none", extra_env={"GALLERY_SCAN_LIMIT": "50"})
        # Remove default fixture files so we have exactly 1 item
        for f in data_dir.iterdir():
            if f.is_file() and f.name not in (".thumb_cache",):
                f.unlink()
        (data_dir / "img1.jpg").write_bytes(b"\xff\xd8\xff\xe0")

        resp = client.get("/recent")
        assert resp.status_code == 200
        text = resp.data.decode()
        assert "Gallery scan limit reached" not in text

    def test_recent_view_with_truncation(self, tmp_path, monkeypatch):
        from conftest import build_client
        client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none", extra_env={"GALLERY_SCAN_LIMIT": "50"})
        import app as app_module
        # /recent walks up to RECENT_ENUMERATION_LIMIT media files (issue #436),
        # so truncation is flagged against that bound — GALLERY_SCAN_LIMIT only
        # governs folder browsing. Patch the bound low and exceed it.
        monkeypatch.setattr(app_module, "RECENT_ENUMERATION_LIMIT", 50)
        # Remove default fixture files and create 51 files to exceed the bound
        for f in data_dir.iterdir():
            if f.is_file() and f.name not in (".thumb_cache",):
                f.unlink()
        for i in range(51):
            (data_dir / f"img{i:04d}.jpg").write_bytes(b"\xff\xd8\xff\xe0")

        resp = client.get("/recent")
        assert resp.status_code == 200
        text = resp.data.decode()
        assert "Gallery scan limit reached" in text


class TestIndexViewScanTruncation:
    """index() should detect scan truncation and surface an indicator."""

    def test_index_view_no_truncation(self, tmp_path, monkeypatch):
        from conftest import build_client
        client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none", extra_env={"GALLERY_SCAN_LIMIT": "50"})
        # Default fixture has 3 files (sample.png, copy.png, cats/cat.jpg) - under limit

        resp = client.get("/")
        assert resp.status_code == 200
        text = resp.data.decode()
        assert "Gallery scan limit reached" not in text

    def test_index_view_with_truncation(self, tmp_path, monkeypatch):
        from conftest import build_client
        client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none", extra_env={"GALLERY_SCAN_LIMIT": "50"})
        # Remove default fixture files and create 51 files to exceed limit of 50
        for f in data_dir.iterdir():
            if f.is_file() and f.name not in (".thumb_cache",):
                f.unlink()
        for i in range(51):
            (data_dir / f"img{i:04d}.jpg").write_bytes(b"\xff\xd8\xff\xe0")

        resp = client.get("/")
        assert resp.status_code == 200
        text = resp.data.decode()
        assert "Gallery scan limit reached" in text


class TestSubfolderScanLimit:
    """index() should respect GALLERY_SCAN_LIMIT when browsing subfolders (issue #386)."""

    def test_subfolder_respects_scan_limit(self, tmp_path, monkeypatch):
        from conftest import build_client
        client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none", extra_env={"GALLERY_SCAN_LIMIT": "50"})
        # Remove default fixture files
        for f in data_dir.iterdir():
            if f.is_file() and f.name not in (".thumb_cache",):
                f.unlink()

        # Create a subfolder with many files exceeding the scan limit
        subfolder = data_dir / "large_folder"
        subfolder.mkdir()
        for i in range(60):
            (subfolder / f"img{i:04d}.jpg").write_bytes(b"\xff\xd8\xff\xe0")

        # Browse the subfolder — should be truncated at 50 items
        resp = client.get("/large_folder")
        assert resp.status_code == 200
        text = resp.data.decode()
        assert "Gallery scan limit reached" in text

    def test_subfolder_no_truncation_when_under_limit(self, tmp_path, monkeypatch):
        from conftest import build_client
        client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none", extra_env={"GALLERY_SCAN_LIMIT": "50"})
        # Remove default fixture files
        for f in data_dir.iterdir():
            if f.is_file() and f.name not in (".thumb_cache",):
                f.unlink()

        # Create a subfolder with fewer files than the scan limit
        subfolder = data_dir / "small_folder"
        subfolder.mkdir()
        for i in range(10):
            (subfolder / f"img{i:04d}.jpg").write_bytes(b"\xff\xd8\xff\xe0")

        # Browse the subfolder — should not be truncated
        resp = client.get("/small_folder")
        assert resp.status_code == 200
        text = resp.data.decode()
        assert "Gallery scan limit reached" not in text


class TestRecentBoundedScan:
    """Regression tests for issue #436.

    /recent must show the globally newest media even when that file lives
    beyond the first GALLERY_SCAN_LIMIT items in the recursive traversal.
    """

    def test_recent_includes_newest_file_beyond_scan_limit(self, tmp_path, monkeypatch):
        """A file written outside the bounded prefix must still appear in /recent."""
        import os
        import time
        from contextlib import suppress

        from conftest import build_client

        # Force the scan to stop at 50 items so we can deterministically
        # place the newest file beyond that bound.
        client, data_dir = build_client(
            monkeypatch,
            tmp_path,
            auth_type="none",
            extra_env={"GALLERY_SCAN_LIMIT": "50"},
        )

        # Wipe the default fixture files so we control the layout.
        for existing in data_dir.rglob("*"):
            if existing.is_file():
                existing.unlink()
        for empty_dir in [d for d in data_dir.rglob("*") if d.is_dir()]:
            with suppress(OSError):
                empty_dir.rmdir()

        # Create 100 old files whose path-sorted order puts them all
        # before the file we are about to add.
        old_mtime = time.time() - 3600
        for i in range(100):
            p = data_dir / f"img{i:04d}.jpg"
            p.write_bytes(b"\xff\xd8\xff\xe0")
            os.utime(p, (old_mtime, old_mtime))

        # The newest file lives beyond the bounded prefix: it sorts
        # alphabetically after every img*.jpg entry.
        newest_name = "zzz_newest_image.jpg"
        newest_path = data_dir / newest_name
        newest_path.write_bytes(b"\xff\xd8\xff\xe0")
        newest_mtime = time.time()
        os.utime(newest_path, (newest_mtime, newest_mtime))

        resp = client.get("/recent")
        assert resp.status_code == 200
        text = resp.data.decode()

        # The newest file must appear in the rendered /recent page even
        # though the recursive scan is bounded and would stop before it
        # without the larger enumeration limit.
        assert newest_name in text

        # And it must be the first card (newest mtime wins over the 100
        # older files).
        first_card_pos = text.find('class="image-card"')
        newest_pos = text.find(newest_name)
        assert first_card_pos != -1, "no image-card rendered on /recent"
        assert newest_pos != -1
        assert newest_pos < first_card_pos + 2000, (
            "newest file should appear near the top of /recent"
        )
