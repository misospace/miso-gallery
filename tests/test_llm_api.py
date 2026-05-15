from conftest import build_client, auth_header


def test_llm_api_requires_configured_valid_bearer_token(monkeypatch, tmp_path):
    client, _ = build_client(monkeypatch, tmp_path)

    assert client.get("/api/llm/images").status_code == 401
    assert client.get("/api/llm/images", headers=auth_header("wrong")).status_code == 401
    assert client.get("/api/llm/images", headers=auth_header()).status_code == 200


def test_llm_api_reports_unconfigured_keys(monkeypatch, tmp_path):
    client, _ = build_client(monkeypatch, tmp_path, api_keys=None, auth_type="none")

    resp = client.get("/api/llm/images")

    assert resp.status_code == 403
    assert resp.get_json()["error"] == "LLM API keys are not configured"


def test_llm_images_search_metadata_recent_and_folders(monkeypatch, tmp_path):
    client, _ = build_client(monkeypatch, tmp_path)

    images = client.get("/api/llm/images?q=cat", headers=auth_header())
    assert images.status_code == 200
    payload = images.get_json()
    assert payload["count"] == 1
    assert payload["images"][0]["rel_path"] == "cats/cat.jpg"

    image = client.get("/api/llm/image/cats/cat.jpg", headers=auth_header())
    assert image.status_code == 200
    assert image.get_json()["media_type"] == "image"

    recent = client.get("/api/llm/recent?limit=2", headers=auth_header())
    assert recent.status_code == 200
    assert recent.get_json()["count"] == 2

    folders = client.get("/api/llm/folders", headers=auth_header())
    assert folders.status_code == 200
    assert any(folder["rel_path"] == "cats" for folder in folders.get_json()["folders"])


def test_llm_delete_and_bulk_delete_do_not_require_csrf(monkeypatch, tmp_path):
    client, data_dir = build_client(monkeypatch, tmp_path)

    delete = client.post("/api/llm/delete", json={"rel_path": "cats/cat.jpg"}, headers=auth_header())
    assert delete.status_code == 200
    assert delete.get_json()["deleted"] is True
    assert not (data_dir / "cats" / "cat.jpg").exists()

    bulk = client.post("/api/llm/bulk-delete", json={"rel_paths": ["sample.png"]}, headers=auth_header())
    assert bulk.status_code == 200
    assert bulk.get_json()["deleted"] == ["sample.png"]
    assert not (data_dir / "sample.png").exists()


def test_llm_dedup_dry_run_and_remove(monkeypatch, tmp_path):
    client, data_dir = build_client(monkeypatch, tmp_path)

    dry_run = client.post("/api/llm/dedup", json={}, headers=auth_header())
    assert dry_run.status_code == 200
    dry_payload = dry_run.get_json()
    assert dry_payload["dry_run"] is True
    assert dry_payload["group_count"] == 1
    assert dry_payload["removed"] == []

    remove = client.post("/api/llm/dedup", json={"remove": True}, headers=auth_header())
    assert remove.status_code == 200
    payload = remove.get_json()
    assert payload["dry_run"] is False
    assert payload["removed"] == ["sample.png"]
    assert not (data_dir / "sample.png").exists()


def test_llm_tags_and_task_run(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("WEBHOOK_TASK_ECHO", "python3 -c \"import sys;print(sys.argv[1])\" {params.value}")
    client, _ = build_client(monkeypatch, tmp_path)

    tags = client.post(
        "/api/llm/tags",
        json={"rel_path": "sample.png", "tag": "miso", "action": "add"},
        headers=auth_header(),
    )
    assert tags.status_code == 200
    assert tags.get_json()["updated"] == ["sample.png"]

    task = client.post(
        "/api/llm/task/run",
        json={"task": "echo", "params": {"value": "hello"}},
        headers=auth_header(),
    )
    assert task.status_code == 200
    assert task.get_json()["stdout"].strip() == "hello"
def test_write_key_can_access_read_endpoints(monkeypatch, tmp_path):
    """Write-scoped keys should be accepted on read endpoints."""
    client, _ = build_client(monkeypatch, tmp_path, api_keys="write-only-key")

    # Write key should work on read endpoints
    images = client.get("/api/llm/images", headers=auth_header("write-only-key"))
    assert images.status_code == 200


def test_read_key_rejected_from_write_endpoints(monkeypatch, tmp_path):
    """Read-scoped keys should be rejected from write endpoints."""
    client, data_dir = build_client(
        monkeypatch,
        tmp_path,
        auth_type="local",
        extra_env={
            "LLM_READ_API_KEYS": "read-only-key",
            "LLM_WRITE_API_KEYS": "write-secret-key",
        },
    )

    # Read key should NOT work on delete endpoint (requires write scope)
    delete = client.post("/api/llm/delete", json={"rel_path": "cats/cat.jpg"}, headers=auth_header("read-only-key"))
    assert delete.status_code == 401

    # Read key should NOT work on bulk-delete
    bulk = client.post("/api/llm/bulk-delete", json={"rel_paths": ["cats/cat.jpg"]}, headers=auth_header("read-only-key"))
    assert bulk.status_code == 401


def test_llm_bulk_delete_dry_run(monkeypatch, tmp_path):
    """Bulk delete dry_run mode should report targets without deleting."""
    client, data_dir = build_client(monkeypatch, tmp_path)

    # Verify files exist before dry run
    assert (data_dir / "cats" / "cat.jpg").exists()

    # Dry run should report what would be deleted but not delete anything
    dry_run = client.post("/api/llm/bulk-delete", json={"rel_paths": ["cats/cat.jpg"], "dry_run": True}, headers=auth_header())
    assert dry_run.status_code == 200
    payload = dry_run.get_json()
    assert payload["dry_run"] is True
    assert "cats/cat.jpg" in payload["deleted"]
    assert (data_dir / "cats" / "cat.jpg").exists()

    # Actual delete should remove the file
    actual = client.post("/api/llm/bulk-delete", json={"rel_paths": ["cats/cat.jpg"]}, headers=auth_header())
    assert actual.status_code == 200
    assert not (data_dir / "cats" / "cat.jpg").exists()


def test_llm_browser_session_rejected_on_llm_endpoints(monkeypatch, tmp_path):
    """Browser session auth should be rejected on LLM API endpoints."""
    client, _ = build_client(monkeypatch, tmp_path, api_keys="test-key", auth_type="local")

    # Simulate browser session by setting session cookie
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["user_id"] = "test-user"

    # Should be rejected on read endpoint
    resp = client.get("/api/llm/images")
    assert resp.status_code == 403
    assert "Browser sessions are not accepted" in resp.get_json()["error"]

    # Should also be rejected on write endpoint
    delete_resp = client.post("/api/llm/delete", json={"rel_path": "cats/cat.jpg"})
    assert delete_resp.status_code == 403


def test_llm_dedup_dry_run_default(monkeypatch, tmp_path):
    """Dedup without remove flag should default to dry_run mode."""
    client, data_dir = build_client(monkeypatch, tmp_path)

    # Verify files exist
    assert (data_dir / "sample.png").exists()
    assert (data_dir / "copy.png").exists()

    # No remove flag - should be dry_run
    resp = client.post("/api/llm/dedup", json={}, headers=auth_header())
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["dry_run"] is True
    # Files should still exist
    assert (data_dir / "sample.png").exists()
    assert (data_dir / "copy.png").exists()


def test_llm_delete_dry_run(monkeypatch, tmp_path):
    """Single delete dry_run mode should report without deleting."""
    client, data_dir = build_client(monkeypatch, tmp_path)

    # Verify file exists
    assert (data_dir / "cats" / "cat.jpg").exists()

    # Dry run should not delete
    dry_run = client.post("/api/llm/delete", json={"rel_path": "cats/cat.jpg", "dry_run": True}, headers=auth_header())
    assert dry_run.status_code == 200
    payload = dry_run.get_json()
    assert payload.get("dry_run") is True
    # File should still exist
    assert (data_dir / "cats" / "cat.jpg").exists()

    # Actual delete should remove
    actual = client.post("/api/llm/delete", json={"rel_path": "cats/cat.jpg"}, headers=auth_header())
    assert actual.status_code == 200
    assert not (data_dir / "cats" / "cat.jpg").exists()
