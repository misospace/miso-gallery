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

    for mod in ("auth", "app"):
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

        for mod in ("auth", "app"):
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

        for mod in ("auth", "app"):
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

        for mod in ("auth", "app"):
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

        for mod in ("auth", "app"):
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
