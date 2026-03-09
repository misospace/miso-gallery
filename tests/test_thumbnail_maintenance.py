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

    img = Image.new("RGB", (64, 64), color="blue")
    (data_dir / "cats").mkdir(parents=True, exist_ok=True)
    img.save(data_dir / "cats" / "cat.png")

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


def test_thumbnail_integrity_maintenance_regenerates_and_reports_counts(monkeypatch, tmp_path):
    client = _build_client(monkeypatch, tmp_path)

    settings = client.get("/settings")
    assert settings.status_code == 200
    html = settings.get_data(as_text=True)

    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "CSRF token not found in settings page"
    csrf = match.group(1)

    resp = client.post(
        "/maintenance/thumbnails/regenerate",
        data={"csrf_token": csrf},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    output = resp.get_data(as_text=True)

    assert "Checked: 1" in output
    assert "Regenerated: 1" in output
    assert "Failed: 0" in output
