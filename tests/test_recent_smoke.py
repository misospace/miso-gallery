import re

from PIL import Image

from conftest import build_client


def _build_client(monkeypatch, tmp_path):
    """Build client using shared bootstrap, then add per-test data."""
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")

    # Real image that can be thumbnailed (use same name as conftest fixture)
    img = Image.new("RGB", (64, 64), color="red")
    (data_dir / "cats").mkdir(parents=True, exist_ok=True)
    img.save(data_dir / "cats" / "cat.jpg")

    # Files that should never appear in /recent
    (data_dir / ".thumb_cache").mkdir(parents=True, exist_ok=True)
    img.save(data_dir / ".thumb_cache" / "cache.png")
    (data_dir / ".trash").mkdir(parents=True, exist_ok=True)
    img.save(data_dir / ".trash" / "trash.png")

    return client


def test_recent_cards_have_valid_view_and_thumb_links(monkeypatch, tmp_path):
    client = _build_client(monkeypatch, tmp_path)

    resp = client.get("/recent")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert 'id="refreshRecentBtn"' in html
    assert "↻ Refresh" in html

    # Should include real image from data folder
    assert "cats/cat.jpg" in html

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


def test_recent_cards_render_details_panel(monkeypatch, tmp_path):
    client = _build_client(monkeypatch, tmp_path)

    resp = client.get("/recent")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert "<summary>Details</summary>" in html
    assert 'Path</span><span class="image-details-value">cats/cat.jpg' in html
    assert "content-visibility:auto" in html
    assert "contain-intrinsic-size:260px 320px" in html
    assert "fetchpriority=\"low\"" in html
