import re

from PIL import Image

from conftest import build_client


def _build_client(monkeypatch, tmp_path):
    """Build client using shared bootstrap, then add per-test data."""
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")

    img = Image.new("RGB", (64, 64), color="blue")
    (data_dir / "cats").mkdir(parents=True, exist_ok=True)
    img.save(data_dir / "cats" / "cat.png")

    return client


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

    # Maintenance runs successfully and reports counts
    assert "Checked:" in output
    assert "Regenerated:" in output
    assert "Failed: 0" in output
