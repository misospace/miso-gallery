"""Regression tests for ``find_duplicate_media()`` and the
``POST /api/llm/dedup`` endpoint.

These tests cover the two-layer grouping logic (size, then SHA-256),
the per-call ``limit`` parameter, the request-payload ``limit`` cap, and
the ``LLM_DEDUP_MAX_REMOVALS`` guardrail that prevents runaway bulk
trash operations.
"""

import pytest

from conftest import auth_header, build_client

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Reset the in-process rate-limit buckets so each test starts with a
    fresh budget. ``/api/llm/dedup`` is configured for 5 req/min and the
    fallback limiter is process-global, so without this the tests would
    exhaust each other."""
    from security import FALLBACK_LIMITER

    FALLBACK_LIMITER.reset()
    yield
    FALLBACK_LIMITER.reset()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_png_data(byte_value: int) -> bytes:
    """Return *n* distinct 1x1 PNG payloads that all share ``byte_value`` as a
    common byte, so they have identical length but differ in content."""

    # 1x1 red PNG payload from conftest. We append an extra byte to make
    # each variant distinguishable but the same length.
    base = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01"
        b"\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return base + bytes([byte_value])


def _set_data_folder(monkeypatch, tmp_path):
    """Point ``app.DATA_FOLDER`` and related globals at ``tmp_path``."""

    data_dir = tmp_path / "dedup_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / ".thumb_cache").mkdir(exist_ok=True)

    import app

    monkeypatch.setattr(app, "DATA_FOLDER", data_dir)
    return data_dir


# ---------------------------------------------------------------------------
# Tests for ``find_duplicate_media()``
# ---------------------------------------------------------------------------


def test_find_duplicate_media_detects_known_duplicate_pair(monkeypatch, tmp_path):
    """Two files with identical bytes must be grouped together."""

    client, data_dir = build_client(monkeypatch, tmp_path)

    from app import find_duplicate_media

    groups = find_duplicate_media()

    assert len(groups) == 1
    group = groups[0]
    assert set(group["all"]) == {"sample.png", "copy.png"}
    # One file is designated as the survivor; the other is a duplicate.
    assert group["keep"] in {"sample.png", "copy.png"}
    assert group["duplicates"] == [p for p in group["all"] if p != group["keep"]]
    assert isinstance(group["hash"], str) and len(group["hash"]) == 64
    assert group["size"] > 0


def test_find_duplicate_media_unique_files_returns_empty(monkeypatch, tmp_path):
    """A directory with only unique files must produce zero groups."""

    data_dir = _set_data_folder(monkeypatch, tmp_path)

    # Three files, three different payloads.
    (data_dir / "a.png").write_bytes(_make_png_data(0x01))
    (data_dir / "b.png").write_bytes(_make_png_data(0x02))
    (data_dir / "c.png").write_bytes(_make_png_data(0x03))

    from app import find_duplicate_media

    assert find_duplicate_media() == []


def test_find_duplicate_media_limit_caps_group_count(monkeypatch, tmp_path):
    """The ``limit`` argument must cap the number of returned groups."""

    data_dir = _set_data_folder(monkeypatch, tmp_path)

    # Three duplicate pairs → three groups.
    for idx in range(3):
        payload = _make_png_data(0x10 + idx)
        (data_dir / f"orig_{idx}.png").write_bytes(payload)
        (data_dir / f"dup_{idx}.png").write_bytes(payload)

    from app import find_duplicate_media

    all_groups = find_duplicate_media()
    assert len(all_groups) == 3

    limited = find_duplicate_media(limit=1)
    assert len(limited) == 1

    larger_limit = find_duplicate_media(limit=10)
    assert len(larger_limit) == 3


# ---------------------------------------------------------------------------
# Tests for the ``/api/llm/dedup`` endpoint
# ---------------------------------------------------------------------------


def test_llm_dedup_dry_run_returns_groups_without_removing(monkeypatch, tmp_path):
    """``dry_run`` must never modify the filesystem."""

    client, data_dir = build_client(monkeypatch, tmp_path)

    resp = client.post("/api/llm/dedup", json={}, headers=auth_header())
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["dry_run"] is True
    assert payload["group_count"] == 1
    assert "removed" not in payload
    assert payload["deleted_count"] == 0
    assert payload["skipped_count"] == 0

    # Both files must still exist on disk.
    assert (data_dir / "sample.png").exists()
    assert (data_dir / "copy.png").exists()


def test_llm_dedup_remove_actually_trashes_duplicate(monkeypatch, tmp_path):
    """``remove=true`` must move the duplicate to the trash directory."""

    client, data_dir = build_client(monkeypatch, tmp_path)

    resp = client.post("/api/llm/dedup", json={"remove": True}, headers=auth_header())
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["dry_run"] is False
    assert payload["group_count"] == 1
    assert "sample.png" in payload["removed"]

    remaining = {p.name for p in data_dir.iterdir()}
    # Exactly one of the duplicates must remain.
    assert len({"sample.png", "copy.png"} & remaining) == 1


def test_llm_dedup_payload_limit_caps_returned_groups(monkeypatch, tmp_path):
    """The ``limit`` field in the JSON body must cap the response groups."""

    client, data_dir = build_client(monkeypatch, tmp_path)

    # Add two more duplicate pairs to the existing fixture so we have three.
    for idx in range(2):
        payload_bytes = _make_png_data(0x20 + idx)
        (data_dir / f"photo_{idx}.png").write_bytes(payload_bytes)
        (data_dir / f"photo_{idx}_dup.png").write_bytes(payload_bytes)

    resp = client.post("/api/llm/dedup", json={"limit": 1}, headers=auth_header())
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["dry_run"] is True
    assert payload["group_count"] == 1

    # With no limit the endpoint should return all three groups.
    resp = client.post("/api/llm/dedup", json={}, headers=auth_header())
    assert resp.status_code == 200
    assert resp.get_json()["group_count"] == 3


def test_llm_dedup_max_removals_guardrail_blocks_oversized_batch(monkeypatch, tmp_path):
    """When removals exceed ``LLM_DEDUP_MAX_REMOVALS``, the endpoint must
    refuse the request with a 4xx response rather than trashing files."""

    client, data_dir = build_client(monkeypatch, tmp_path)

    # Add two more duplicate pairs so three groups exist.
    for idx in range(2):
        payload_bytes = _make_png_data(0x30 + idx)
        (data_dir / f"extra_{idx}.png").write_bytes(payload_bytes)
        (data_dir / f"extra_{idx}_dup.png").write_bytes(payload_bytes)

    import app

    # Force the guardrail down to 1 so the three pairs exceed it.
    monkeypatch.setattr(app, "LLM_DEDUP_MAX_REMOVALS", 1)

    resp = client.post("/api/llm/dedup", json={"remove": True}, headers=auth_header())

    # Endpoint must reject the request with 4xx rather than trash files.
    assert 400 <= resp.status_code < 500
    # All files must still be present because the cap rejected removal.
    assert (data_dir / "sample.png").exists()
    assert (data_dir / "copy.png").exists()
    assert (data_dir / "extra_0.png").exists()
    assert (data_dir / "extra_1.png").exists()


def test_llm_dedup_max_removals_allows_under_limit_batch(monkeypatch, tmp_path):
    """When removals are at or below ``LLM_DEDUP_MAX_REMOVALS``, the
    endpoint must proceed normally."""

    client, data_dir = build_client(monkeypatch, tmp_path)

    import app

    monkeypatch.setattr(app, "LLM_DEDUP_MAX_REMOVALS", 5)

    resp = client.post("/api/llm/dedup", json={"remove": True}, headers=auth_header())
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["dry_run"] is False
    assert payload["deleted_count"] == 1
    assert "sample.png" in payload["removed"] or "copy.png" in payload["removed"]


# ---------------------------------------------------------------------------
# Tests for ``batch_remove_thumbnails()``
# ---------------------------------------------------------------------------


def test_batch_remove_thumbnails_removes_matching_prefixes(monkeypatch, tmp_path):
    """``batch_remove_thumbnails()`` must delete only the cached thumbnails
    whose ``__``-prefixed stem corresponds to one of the supplied paths."""

    client, data_dir = build_client(monkeypatch, tmp_path)

    import app

    thumb_dir = data_dir / ".thumb_cache"
    thumb_dir.mkdir(parents=True, exist_ok=True)

    matching_a = thumb_dir / "sample__img.png.111.222.jpg"
    matching_b = thumb_dir / "cats__cat.jpg.333.444.jpg"
    unrelated = thumb_dir / "other__bar.png.555.666.jpg"
    matching_a.write_bytes(b"a")
    matching_b.write_bytes(b"b")
    unrelated.write_bytes(b"c")

    app.batch_remove_thumbnails(["sample/img.png", "cats/cat.jpg"])

    assert not matching_a.exists(), "matching prefix should have been removed"
    assert not matching_b.exists(), "matching nested prefix should have been removed"
    assert unrelated.exists(), "unrelated prefix must be preserved"


def test_batch_remove_thumbnails_handles_empty_input(monkeypatch, tmp_path):
    """An empty rel-path list must short-circuit without touching disk."""

    build_client(monkeypatch, tmp_path)

    import app

    # Should not raise even when the cache directory is freshly empty.
    app.batch_remove_thumbnails([])  # no exception


# ---------------------------------------------------------------------------
# Test for the LLM_DEDUP_MAX_REMOVALS env-var loading path
# ---------------------------------------------------------------------------


def test_llm_dedup_max_removals_env_var_is_honoured_at_import(monkeypatch, tmp_path):
    """The module-level ``LLM_DEDUP_MAX_REMOVALS`` constant must reflect the
    value of the ``LLM_DEDUP_MAX_REMOVALS`` environment variable at import
    time (this is the path used in production, not monkeypatching)."""

    import importlib
    import sys

    monkeypatch.setenv("LLM_DEDUP_MAX_REMOVALS", "7")

    sys.modules.pop("app", None)
    reloaded = importlib.import_module("app")

    assert reloaded.LLM_DEDUP_MAX_REMOVALS == 7
