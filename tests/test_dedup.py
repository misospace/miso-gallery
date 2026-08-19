"""Tests for find_duplicate_media() and the /api/llm/dedup endpoint (issue #365).

Covers:
- find_duplicate_media() with known duplicate files (same content, different names)
- find_duplicate_media() with unique files (no duplicates expected)
- find_duplicate_media() with the limit parameter
- llm_dedup() endpoint with dry_run=true and dry_run=false
- Guardrail enforcement: LLM_DEDUP_MAX_REMOVALS cap
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from conftest import auth_header, build_client

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# A second, distinct PNG (different bytes from conftest's _MINIMAL_PNG)
_OTHER_PNG = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\x00\x00\x00\x03\x00\x00\x05\xfe\xff\xff\x00\x00\x00\x00IEND\xaeB`\x82'


def test_find_duplicate_media_groups_same_content_different_names(monkeypatch, tmp_path):
    client, data_dir = build_client(monkeypatch, tmp_path)
    app_module = importlib.import_module("app")

    groups = app_module.find_duplicate_media()
    # sample.png and copy.png share identical bytes -> one group of 2
    assert len(groups) == 1
    group = groups[0]
    assert group["keep"] in {"sample.png", "copy.png"}
    assert set(group["duplicates"]) == {"sample.png", "copy.png"} - {group["keep"]}
    assert set(group["all"]) == {"sample.png", "copy.png"}
    # hash must be a 64-char hex digest
    assert len(group["hash"]) == 64
    int(group["hash"], 16)  # raises if not hex


def test_find_duplicate_media_no_duplicates_for_unique_files(monkeypatch, tmp_path):
    client, data_dir = build_client(monkeypatch, tmp_path)
    app_module = importlib.import_module("app")

    # Remove the duplicate so only unique files remain (sample.png, cats/cat.jpg)
    (data_dir / "copy.png").unlink()
    groups = app_module.find_duplicate_media()
    assert groups == []


def test_find_duplicate_media_limit(monkeypatch, tmp_path):
    client, data_dir = build_client(monkeypatch, tmp_path)
    app_module = importlib.import_module("app")

    # Add a second duplicate pair with distinct content -> two groups total
    (data_dir / "dup_a.png").write_bytes(_OTHER_PNG)
    (data_dir / "dup_b.png").write_bytes(_OTHER_PNG)

    all_groups = app_module.find_duplicate_media()
    assert len(all_groups) == 2

    limited = app_module.find_duplicate_media(limit=1)
    assert len(limited) == 1
    # limit must not alter grouping, just truncate the list
    assert limited[0]["all"] == all_groups[0]["all"]


def test_llm_dedup_dry_run_true(monkeypatch, tmp_path):
    client, data_dir = build_client(monkeypatch, tmp_path)
    resp = client.post(
        "/api/llm/dedup",
        json={"dry_run": True},
        headers=auth_header(),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["dry_run"] is True
    assert body["group_count"] == 1
    assert set(body["duplicate_groups"][0]["all"]) == {"sample.png", "copy.png"}
    # dry run must not remove anything
    assert (data_dir / "sample.png").exists()
    assert (data_dir / "copy.png").exists()


def test_llm_dedup_dry_run_false_removes_duplicates(monkeypatch, tmp_path):
    client, data_dir = build_client(monkeypatch, tmp_path)
    resp = client.post(
        "/api/llm/dedup",
        json={"dry_run": False, "remove": True},
        headers=auth_header(),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["dry_run"] is False
    assert body["group_count"] == 1
    # exactly the duplicate (not the kept original) is removed
    assert len(body["removed"]) == 1
    assert body["deleted_count"] == 1
    removed = body["removed"][0]
    kept = body["duplicate_groups"][0]["keep"]
    assert removed != kept
    assert (data_dir / kept).exists()
    assert not (data_dir / removed).exists()


def test_llm_dedup_guardrail_max_removals(monkeypatch, tmp_path):
    client, data_dir = build_client(monkeypatch, tmp_path)
    app_module = importlib.import_module("app")

    # Cap removals at 0; the single group has 1 duplicate -> must be rejected
    monkeypatch.setattr(app_module, "LLM_DEDUP_MAX_REMOVALS", 0)
    resp = client.post(
        "/api/llm/dedup",
        json={"dry_run": False, "remove": True},
        headers=auth_header(),
    )
    assert resp.status_code == 422
    body = resp.get_json()
    assert "Maximum is 0" in body["error"]
    # nothing removed
    assert (data_dir / "sample.png").exists()
    assert (data_dir / "copy.png").exists()
