import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _build_client(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

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


def test_webhook_task_runs_configured_command(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBHOOK_ENABLED", "true")
    monkeypatch.setenv(
        "WEBHOOK_TASK_GENERATE",
        "python3 -c \"import sys;print('ok-'+sys.argv[1])\" {params.name}",
    )

    client = _build_client(monkeypatch, tmp_path)
    resp = client.post("/api/webhook/run", json={"task": "generate", "params": {"name": "miso"}})

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["success"] is True
    assert payload["stdout"].strip() == "ok-miso"


def test_webhook_task_rejects_missing_template_params(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("WEBHOOK_TASK_GENERATE", "echo {params.name}")

    client = _build_client(monkeypatch, tmp_path)
    resp = client.post("/api/webhook/run", json={"task": "generate", "params": {}})

    assert resp.status_code == 400
    payload = resp.get_json()
    assert "missing required params" in payload["error"]


def test_webhook_task_returns_404_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBHOOK_ENABLED", "false")
    monkeypatch.setenv("WEBHOOK_TASK_GENERATE", "echo hi")

    client = _build_client(monkeypatch, tmp_path)
    resp = client.post("/api/webhook/run", json={"task": "generate", "params": {}})

    assert resp.status_code == 404
