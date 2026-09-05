import re
from unittest.mock import patch

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


def test_recent_view_uses_iter_gallery_items(monkeypatch, tmp_path):
    """Regression test: recent_view must delegate to iter_gallery_items, not duplicate scan logic."""
    client = _build_client(monkeypatch, tmp_path)

    with patch("app.iter_gallery_items", wraps=None) as mock_iter:
        mock_iter.return_value = []  # Return empty so no items are processed
        resp = client.get("/recent")
        assert resp.status_code == 200
        # /recent must enumerate beyond GALLERY_SCAN_LIMIT so that the newest
        # media is reachable when it sorts past the first traversal prefix
        # (issue #436).
        mock_iter.assert_called_once_with(kind="media", limit=50000)


def test_recent_banner_hidden_when_gallery_exceeds_gallery_scan_limit(monkeypatch, tmp_path):
    """Regression: /recent walks RECENT_ENUMERATION_LIMIT (issue #436), so a
    gallery larger than GALLERY_SCAN_LIMIT must not show the truncation banner
    — the old code compared the enumerated count against GALLERY_SCAN_LIMIT
    and flagged every library over 5000 files as truncated."""
    client = _build_client(monkeypatch, tmp_path)
    data_dir = tmp_path / "data"
    import app as app_module  # after _build_client: conftest re-imports the module

    monkeypatch.setattr(app_module, "GALLERY_SCAN_LIMIT", 2)
    for i in range(5):
        img = Image.new("RGB", (8, 8), color="blue")
        img.save(data_dir / f"img_{i}.png")

    html = client.get("/recent").get_data(as_text=True)
    assert "Gallery scan limit reached" not in html


def test_recent_banner_shown_when_enumeration_bound_hit(monkeypatch, tmp_path):
    """The truncation banner must still fire — naming the real bound — when the
    enumeration stops at RECENT_ENUMERATION_LIMIT with media left unseen."""
    client = _build_client(monkeypatch, tmp_path)
    data_dir = tmp_path / "data"
    import app as app_module  # after _build_client: conftest re-imports the module

    monkeypatch.setattr(app_module, "RECENT_ENUMERATION_LIMIT", 3)
    for i in range(5):
        img = Image.new("RGB", (8, 8), color="green")
        img.save(data_dir / f"img_{i}.png")

    html = client.get("/recent").get_data(as_text=True)
    assert "Gallery scan limit reached" in html
    assert "first 3 items" in html
