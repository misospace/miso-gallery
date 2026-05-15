from conftest import build_client


def _build_webhook_client(monkeypatch, tmp_path, *, webhook_enabled: str = "true", task_cmd: str | None = None):
    """Build client with webhook settings using shared bootstrap."""
    extra_env = {
        "WEBHOOK_ENABLED": webhook_enabled,
    }
    if task_cmd is not None:
        extra_env["WEBHOOK_TASK_GENERATE"] = task_cmd
    # Use auth_type="none" to match original behavior
    client, _ = build_client(monkeypatch, tmp_path, auth_type="none", extra_env=extra_env)
    return client


def test_webhook_task_runs_configured_command(monkeypatch, tmp_path):
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd='python3 -c "import sys;print(\'ok-\'+sys.argv[1])" {params.name}',
    )
    resp = client.post("/api/webhook/run", json={"task": "generate", "params": {"name": "miso"}})

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["success"] is True
    assert payload["stdout"].strip() == "ok-miso"


def test_webhook_task_rejects_missing_template_params(monkeypatch, tmp_path):
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd="echo {params.name}",
    )
    resp = client.post("/api/webhook/run", json={"task": "generate", "params": {}})

    assert resp.status_code == 400
    payload = resp.get_json()
    assert "missing required params" in payload["error"]


def test_webhook_task_returns_404_when_disabled(monkeypatch, tmp_path):
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        webhook_enabled="false",
        task_cmd="echo hi",
    )
    resp = client.post("/api/webhook/run", json={"task": "generate", "params": {}})

    assert resp.status_code == 404
