"""Regression tests for issue #423.

The LLM enumeration endpoints (/api/llm/images, /api/llm/folders,
/api/llm/recent) must search and page across more than the first
GALLERY_SCAN_LIMIT items. Previously iter_gallery_items truncated its
collection at GALLERY_SCAN_LIMIT, so media sorting beyond the bound was
unreachable and has_more promised results the API could not return.
"""

import app


def test_iter_gallery_items_returns_items_beyond_scan_limit(tmp_path, monkeypatch):
    """A caller requesting LLM_ENUMERATION_LIMIT gets items past GALLERY_SCAN_LIMIT."""
    monkeypatch.setattr(app, "DATA_FOLDER", tmp_path)
    monkeypatch.setattr(app, "GALLERY_SCAN_LIMIT", 5)
    for i in range(8):
        (tmp_path / f"img_{i:03d}.jpg").write_bytes(b"\xff\xd8\xff\xe0")

    items = app.iter_gallery_items(kind="media", limit=app.LLM_ENUMERATION_LIMIT)

    assert len(items) == 8
    assert (tmp_path / "img_007.jpg") in items


def test_iter_gallery_items_default_limit_still_bounded(tmp_path, monkeypatch):
    """The default call still respects GALLERY_SCAN_LIMIT as a safety bound."""
    monkeypatch.setattr(app, "DATA_FOLDER", tmp_path)
    monkeypatch.setattr(app, "GALLERY_SCAN_LIMIT", 5)
    for i in range(8):
        (tmp_path / f"img_{i:03d}.jpg").write_bytes(b"\xff\xd8\xff\xe0")

    items = app.iter_gallery_items(kind="media")

    assert len(items) == 5
