import importlib
import re
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _build_client(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Real image that can be thumbnailed
    img = Image.new("RGB", (64, 64), color="red")
    (data_dir / "cats").mkdir(parents=True, exist_ok=True)
    img.save(data_dir / "cats" / "cat.png")

    # Files that should never appear in /recent
    (data_dir / ".thumb_cache").mkdir(parents=True, exist_ok=True)
    img.save(data_dir / ".thumb_cache" / "cache.png")
    (data_dir / ".trash").mkdir(parents=True, exist_ok=True)
    img.save(data_dir / ".trash" / "trash.png")

    monkeypatch.setenv("DATA_FOLDER", str(data_dir))
    monkeypatch.setenv("AUTH_TYPE", "none")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("OIDC_ENABLED", "false")

    for mod in ("auth", "app"):
        if mod in sys.modules:
            del sys.modules[mod]

    app_module = importlib.import_module("app")
    app_module.DATA_FOLDER = data_dir
    app_module.THUMBNAIL_CACHE_DIR = data_dir / ".thumb_cache"
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_recent_cards_have_valid_view_and_thumb_links(monkeypatch, tmp_path):
    client = _build_client(monkeypatch, tmp_path)

    resp = client.get("/recent")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # Should include real image from data folder
    assert "cats/cat.png" in html

    # Should not include internal cache/trash files
    assert ".thumb_cache" not in html
    assert "/.trash/" not in html

    # Find view and thumb URLs rendered in cards
    view_urls = re.findall(r'href="(/view/[^"]+)"', html)
    thumb_urls = re.findall(r'src="(/thumb/[^"]+)"', html)

    assert view_urls, "No /view links rendered in /recent cards"
    assert thumb_urls, "No /thumb links rendered in /recent cards"

    # Smoke: each rendered thumbnail URL should resolve
    for url in thumb_urls:
        t = client.get(url)
        assert t.status_code == 200, f"Broken thumbnail URL: {url}"
        assert t.headers.get("Content-Type", "").startswith("image/")
