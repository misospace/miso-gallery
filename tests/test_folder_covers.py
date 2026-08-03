
from unittest.mock import patch

from PIL import Image

from app import _FOLDER_COVER_MAX_SIZE, folder_cover_rel_path
from conftest import build_client


def _build_folder_client(monkeypatch, tmp_path, auto_covers: bool):
    """Build client with folder cover settings using shared bootstrap."""
    extra_env = {
        "GALLERY_AUTO_FOLDER_COVERS": str(auto_covers),
        "GALLERY_COVER_CACHE_TTL": "3600",
    }
    # Use auth_type="none" to match original behavior
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none", extra_env=extra_env)

    folder = data_dir / "albums" / "trip"
    folder.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (128, 128), color="purple")
    img.save(folder / "001.jpg")

    return client


def test_folder_card_uses_nested_image_preview_when_enabled(monkeypatch, tmp_path):
    client = _build_folder_client(monkeypatch, tmp_path, auto_covers=True)

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert "folder-preview" in html
    assert '/thumb/albums/trip/001.jpg' in html


def test_folder_card_uses_icon_when_auto_cover_disabled(monkeypatch, tmp_path):
    client = _build_folder_client(monkeypatch, tmp_path, auto_covers=False)

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert '<img class="folder-preview"' not in html
    assert "📁" in html


def test_folder_card_recovers_when_folder_gains_image_after_empty_cache(monkeypatch, tmp_path):
    """When an empty folder gets a new image, the cover should appear after TTL expiry."""
    extra_env = {
        "GALLERY_AUTO_FOLDER_COVERS": "true",
        "GALLERY_COVER_CACHE_TTL": "0",  # TTL=0 means every access re-validates
    }
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none", extra_env=extra_env)

    folder = data_dir / "albums" / "trip"
    folder.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (128, 128), color="purple")
    img.save(folder / "001.jpg")

    first = client.get("/")
    assert first.status_code == 200
    first_html = first.get_data(as_text=True)
    assert '/thumb/albums/trip/001.jpg' in first_html

    empty_folder = data_dir / "albums" / "fresh"
    empty_folder.mkdir(parents=True, exist_ok=True)

    second = client.get("/albums")
    assert second.status_code == 200
    second_html = second.get_data(as_text=True)
    assert '/thumb/albums/fresh/' not in second_html

    img2 = Image.new("RGB", (128, 128), color="orange")
    img2.save(empty_folder / "cover.jpg")

    third = client.get("/albums")
    assert third.status_code == 200
    third_html = third.get_data(as_text=True)
    assert '/thumb/albums/fresh/cover.jpg' in third_html


def test_folder_card_recovers_when_cached_preview_image_is_deleted(monkeypatch, tmp_path):
    extra_env = {
        "GALLERY_AUTO_FOLDER_COVERS": "true",
    }
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none", extra_env=extra_env)

    folder = data_dir / "albums" / "trip"
    folder.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (128, 128), color="purple")
    img.save(folder / "001.jpg")
    backup = Image.new("RGB", (128, 128), color="green")
    backup.save(folder / "002.jpg")

    first = client.get("/")
    assert first.status_code == 200
    first_html = first.get_data(as_text=True)
    assert '/thumb/albums/trip/001.jpg' in first_html

    (folder / "001.jpg").unlink()

    second = client.get("/")
    assert second.status_code == 200
    second_html = second.get_data(as_text=True)
    assert '/thumb/albums/trip/001.jpg' not in second_html
    assert '/thumb/albums/trip/002.jpg' in second_html


def test_folder_cover_cache_lru_eviction(monkeypatch, tmp_path):
    """Cache should evict oldest entries when exceeding max size."""
    extra_env = {
        "GALLERY_AUTO_FOLDER_COVERS": "true",
        "GALLERY_COVER_CACHE_TTL": "3600",
    }
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none", extra_env=extra_env)

    from app import _FOLDER_COVER_CACHE as cache

    cache.clear()

    # Create more folders than the max cache size.
    num_folders = _FOLDER_COVER_MAX_SIZE + 10
    folder_names = []
    for i in range(num_folders):
        folder = data_dir / f"folder_{i}"
        folder.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (64, 64), color="red")
        img.save(folder / "img.jpg")
        folder_names.append(f"folder_{i}")

    # Patch DATA_FOLDER so folder_cover_rel_path uses the test data dir.
    with patch.object(__import__("app", fromlist=["DATA_FOLDER"]), "DATA_FOLDER", data_dir):
        import app as app_mod

        # Populate cache by calling folder_cover_rel_path for each folder.
        for name in folder_names:
            result = app_mod.folder_cover_rel_path(name)
            assert result is not None, f"Expected cover for {name}"

        # Cache should be bounded to max size.
        assert len(cache) <= _FOLDER_COVER_MAX_SIZE

        # The oldest entries should have been evicted.
        # The most recently accessed folder should still be in cache.
        assert app_mod.folder_cover_rel_path(folder_names[-1]) is not None


def test_folder_card_symlink_escape_blocked(monkeypatch, tmp_path):
    """Symlinks pointing outside DATA_FOLDER must not be served as covers."""
    extra_env = {
        "GALLERY_AUTO_FOLDER_COVERS": "true",
        "GALLERY_COVER_CACHE_TTL": "3600",
    }
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none", extra_env=extra_env)

    # Create a target image outside DATA_FOLDER.
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_img = Image.new("RGB", (64, 64), color="blue")
    outside_img.save(outside_dir / "secret.jpg")

    # Create a folder inside DATA_FOLDER with a symlink to the outside image.
    folder = data_dir / "albums" / "gallery"
    folder.mkdir(parents=True, exist_ok=True)
    symlink_path = folder / "link.jpg"
    try:
        symlink_path.symlink_to(outside_dir / "secret.jpg")
    except OSError:
        # Symlinks may not be supported on this platform (e.g. Windows without admin).
        return

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # The symlinked image should NOT appear as a cover.
    assert "secret.jpg" not in html


def test_folder_cover_null_byte_path(monkeypatch, tmp_path):
    """Paths containing null bytes should be safely rejected."""
    extra_env = {
        "GALLERY_AUTO_FOLDER_COVERS": "true",
        "GALLERY_COVER_CACHE_TTL": "3600",
    }
    build_client(monkeypatch, tmp_path, auth_type="none", extra_env=extra_env)

    from app import _FOLDER_COVER_CACHE as cache

    cache.clear()

    # Null byte in path should return None (not crash or serve anything).
    result = folder_cover_rel_path("albums\0evil")
    assert result is None
