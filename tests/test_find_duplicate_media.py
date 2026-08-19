"""Focused coverage for find_duplicate_media() and POST /api/llm/dedup (issue #365).

Endpoint dry-run/remove flows live in tests/test_llm_api.py; this file covers
direct grouping, size-first/hash-second dedup, limit behavior, the
LLM_DEDUP_MAX_REMOVALS guardrail, and unauthenticated access.
"""

import contextlib
import hashlib

import pytest

from conftest import _MINIMAL_PNG, auth_header, build_client


@pytest.fixture(autouse=True)
def reset_rate_limiters():
    """Reset rate limiter state before each test to avoid cross-test interference."""
    from security import FALLBACK_LIMITER, _primary_limiter

    FALLBACK_LIMITER.reset()
    if _primary_limiter is not None and _primary_limiter is not FALLBACK_LIMITER:
        client = getattr(_primary_limiter, "_client", None)
        prefix = getattr(_primary_limiter, "_prefix", "")
        if client is not None and prefix:
            with contextlib.suppress(Exception):
                keys = list(client.scan_iter(match=f"{prefix}:*"))
                if keys:
                    client.delete(*keys)


def test_direct_duplicate_pair_grouping(monkeypatch, tmp_path):
    _, _ = build_client(monkeypatch, tmp_path)
    import app as app_module

    app_module._invalidate_gallery_scan_cache()
    groups = app_module.find_duplicate_media()

    assert len(groups) == 1
    group = groups[0]
    assert set(group) == {"hash", "size", "keep", "duplicates", "all"}
    assert group["hash"] == hashlib.sha256(_MINIMAL_PNG).hexdigest()
    assert group["size"] == len(_MINIMAL_PNG)
    assert group["all"] == ["copy.png", "sample.png"]
    assert group["keep"] == group["all"][0]
    assert group["duplicates"] == group["all"][1:]


def test_unique_same_size_files_not_grouped(monkeypatch, tmp_path):
    _, data_dir = build_client(monkeypatch, tmp_path)
    (data_dir / "sample.png").write_bytes(b"a" * 128)
    (data_dir / "copy.png").write_bytes(b"b" * 128)
    import app as app_module

    app_module._invalidate_gallery_scan_cache()

    assert app_module.find_duplicate_media() == []


def test_direct_limit_caps_groups(monkeypatch, tmp_path):
    _, data_dir = build_client(monkeypatch, tmp_path)
    for i in range(5):
        payload = b"group-%d" % i + b"x" * 56
        (data_dir / f"pair_{i}_a.png").write_bytes(payload)
        (data_dir / f"pair_{i}_b.png").write_bytes(payload)
    import app as app_module

    app_module._invalidate_gallery_scan_cache()

    assert len(app_module.find_duplicate_media()) == 6

    groups = app_module.find_duplicate_media(limit=3)

    assert len(groups) == 3
    for group in groups:
        assert set(group) == {"hash", "size", "keep", "duplicates", "all"}
        assert len(group["all"]) == 2
        assert group["keep"] == group["all"][0]
        assert group["duplicates"] == group["all"][1:]


def test_endpoint_limit_caps_groups(monkeypatch, tmp_path):
    client, data_dir = build_client(monkeypatch, tmp_path)
    for i in range(5):
        payload = b"group-%d" % i + b"x" * 56
        (data_dir / f"pair_{i}_a.png").write_bytes(payload)
        (data_dir / f"pair_{i}_b.png").write_bytes(payload)

    resp = client.post("/api/llm/dedup", json={"limit": 3}, headers=auth_header())

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["dry_run"] is True
    assert payload["group_count"] == 3
    assert len(payload["duplicate_groups"]) == 3


def test_guardrail_rejects_oversized_removal_batch(monkeypatch, tmp_path):
    client, data_dir = build_client(monkeypatch, tmp_path, extra_env={"LLM_DEDUP_MAX_REMOVALS": "1"})
    payload = b"big-dup" + b"y" * 57
    for i in range(3):
        (data_dir / f"big_{i}.png").write_bytes(payload)

    resp = client.post("/api/llm/dedup", json={"remove": True}, headers=auth_header())

    assert resp.status_code == 422
    assert "Maximum is 1" in resp.get_json()["error"]
    for i in range(3):
        assert (data_dir / f"big_{i}.png").exists()
    assert (data_dir / "sample.png").exists()
    assert (data_dir / "copy.png").exists()


def test_unauthenticated_dedup_rejected(monkeypatch, tmp_path):
    client, _ = build_client(monkeypatch, tmp_path)

    resp = client.post("/api/llm/dedup", json={})

    assert resp.status_code == 401
    assert "error" in resp.get_json()
