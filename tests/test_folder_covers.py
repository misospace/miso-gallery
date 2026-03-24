import importlib
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _build_client(monkeypatch, tmp_path, auto_covers: bool):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    folder = data_dir / "albums" / "trip"
    folder.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (128, 128), color="purple")
    img.save(folder / "001.jpg")

    monkeypatch.setenv("DATA_FOLDER", str(data_dir))
    monkeypatch.setenv("AUTH_TYPE", "none")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("OIDC_ENABLED", "false")
    monkeypatch.setenv("GALLERY_AUTO_FOLDER_COVERS", "true" if auto_covers else "false")
    monkeypatch.setenv("GALLERY_COVER_CACHE_TTL", "3600")

    for mod in ("auth", "app"):
        if mod in sys.modules:
            del sys.modules[mod]

    app_module = importlib.import_module("app")
    app_module.DATA_FOLDER = data_dir
    app_module.THUMBNAIL_CACHE_DIR = data_dir / ".thumb_cache"
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_folder_card_uses_nested_image_preview_when_enabled(monkeypatch, tmp_path):
    client = _build_client(monkeypatch, tmp_path, auto_covers=True)

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert "folder-preview" in html
    assert '/thumb/albums/trip/001.jpg' in html


def test_folder_card_uses_icon_when_auto_cover_disabled(monkeypatch, tmp_path):
    client = _build_client(monkeypatch, tmp_path, auto_covers=False)

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert '<img class="folder-preview"' not in html
    assert "📁" in html


def test_folder_card_recovers_when_folder_gains_image_after_empty_cache(monkeypatch, tmp_path):
    client = _build_client(monkeypatch, tmp_path, auto_covers=True)

    empty_folder = tmp_path / "data" / "albums" / "fresh"
    empty_folder.mkdir(parents=True, exist_ok=True)

    first = client.get("/albums")
    assert first.status_code == 200
    first_html = first.get_data(as_text=True)
    assert '/thumb/albums/fresh/' not in first_html

    img = Image.new("RGB", (128, 128), color="orange")
    img.save(empty_folder / "cover.jpg")

    second = client.get("/albums")
    assert second.status_code == 200
    second_html = second.get_data(as_text=True)
    assert '/thumb/albums/fresh/cover.jpg' in second_html


def test_folder_card_recovers_when_cached_preview_image_is_deleted(monkeypatch, tmp_path):
    client = _build_client(monkeypatch, tmp_path, auto_covers=True)

    folder = tmp_path / "data" / "albums" / "trip"
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
