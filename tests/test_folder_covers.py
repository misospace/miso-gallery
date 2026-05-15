from pathlib import Path

from PIL import Image

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
    extra_env = {
        "GALLERY_AUTO_FOLDER_COVERS": "true",
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
