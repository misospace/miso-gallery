import threading
import time

from conftest import build_client


def setup_function():
    """Reset the in-memory rate limiter before each test to avoid cross-test
    pollution: the webhook endpoint is rate-limited at 20 req / 60 s, and
    the test file fires more than 20 webhook calls across its cases."""
    from security import FALLBACK_LIMITER
    FALLBACK_LIMITER.reset()


def _build_webhook_client(
    monkeypatch, tmp_path, *, webhook_enabled: str = "true", task_cmd: str | None = None,
    webhook_secret: str | None = "test-secret-123",
):
    """Build client with webhook settings using shared bootstrap."""
    extra_env = {
        "WEBHOOK_ENABLED": webhook_enabled,
    }
    if task_cmd is not None:
        extra_env["WEBHOOK_TASK_GENERATE"] = task_cmd
    if webhook_secret is not None:
        extra_env["WEBHOOK_SECRET"] = webhook_secret
    # Use auth_type="none" to match original behavior
    client, _ = build_client(monkeypatch, tmp_path, auth_type="none", extra_env=extra_env)
    return client


def test_webhook_task_runs_configured_command(monkeypatch, tmp_path):
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd='python3 -c "import sys;print(\'ok-\'+sys.argv[1])" {params.name}',
    )
    resp = client.post(
        "/api/webhook/run",
        json={"task": "generate", "params": {"name": "miso"}},
        headers={"Authorization": "Bearer test-secret-123"},
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["success"] is True
    assert payload["stdout"].strip() == "ok-miso"


def test_webhook_task_rejects_missing_template_params(monkeypatch, tmp_path):
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd="echo {params.name}",
    )
    resp = client.post(
        "/api/webhook/run",
        json={"task": "generate", "params": {}},
        headers={"Authorization": "Bearer test-secret-123"},
    )

    assert resp.status_code == 400
    payload = resp.get_json()
    assert "missing required params" in payload["error"]


def test_webhook_task_returns_403_when_disabled(monkeypatch, tmp_path):
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        webhook_enabled="false",
        task_cmd="echo hi",
    )
    resp = client.post("/api/webhook/run", json={"task": "generate", "params": {}})

    assert resp.status_code == 403


def test_webhook_task_rejects_special_characters(monkeypatch, tmp_path):
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd="echo hi",
    )
    resp = client.post(
        "/api/webhook/run",
        json={"task": "ANYTHING_GOES_HERE!!!", "params": {}},
        headers={"Authorization": "Bearer test-secret-123"},
    )

    assert resp.status_code == 400
    payload = resp.get_json()
    assert "Invalid task name" in payload["error"]


def test_webhook_task_rejects_spaces(monkeypatch, tmp_path):
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd="echo hi",
    )
    resp = client.post(
        "/api/webhook/run",
        json={"task": "my task name", "params": {}},
        headers={"Authorization": "Bearer test-secret-123"},
    )

    assert resp.status_code == 400


def test_webhook_task_rejects_dots(monkeypatch, tmp_path):
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd="echo hi",
    )
    resp = client.post(
        "/api/webhook/run",
        json={"task": "my.task.name", "params": {}},
        headers={"Authorization": "Bearer test-secret-123"},
    )

    assert resp.status_code == 400


def test_webhook_task_rejects_path_traversal(monkeypatch, tmp_path):
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd="echo hi",
    )
    resp = client.post(
        "/api/webhook/run",
        json={"task": "../etc/passwd", "params": {}},
        headers={"Authorization": "Bearer test-secret-123"},
    )

    assert resp.status_code == 400


def test_webhook_task_accepts_valid_characters(monkeypatch, tmp_path):
    """Test that task names with hyphens and underscores pass validation."""
    # The env key normalizes hyphens to underscores, so WEBHOOK_TASK_MY_VALID_TASK123
    # matches task name "my-valid_task123" after normalization.
    extra_env = {
        "WEBHOOK_ENABLED": "true",
        "WEBHOOK_SECRET": "test-secret-123",
        "WEBHOOK_TASK_MY_VALID_TASK123": 'python3 -c "import sys;print(\'ok-\'+sys.argv[1])" {params.name}',
    }
    client, _ = build_client(monkeypatch, tmp_path, auth_type="none", extra_env=extra_env)
    resp = client.post(
        "/api/webhook/run",
        json={"task": "my-valid_task123", "params": {"name": "test"}},
        headers={"Authorization": "Bearer test-secret-123"},
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["success"] is True
    assert payload["stdout"].strip() == "ok-test"


def test_webhook_task_rejects_at_sign(monkeypatch, tmp_path):
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd="echo hi",
    )
    resp = client.post(
        "/api/webhook/run",
        json={"task": "bad@task#name!", "params": {}},
        headers={"Authorization": "Bearer test-secret-123"},
    )

    assert resp.status_code == 400


def test_webhook_task_rejects_null_bytes(monkeypatch, tmp_path):
    """Explicitly test null byte rejection for defense-in-depth documentation."""
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd="echo hi",
    )
    resp = client.post(
        "/api/webhook/run",
        json={"task": "task\0evil", "params": {}},
        headers={"Authorization": "Bearer test-secret-123"},
    )

    assert resp.status_code == 400
    payload = resp.get_json()
    assert "Invalid task name" in payload["error"]


# ---------------------------------------------------------------------------
# Regression tests for issue #383: webhook fails closed without WEBHOOK_SECRET
# ---------------------------------------------------------------------------

def test_webhook_no_secret_returns_503(monkeypatch, tmp_path):
    """Webhook endpoint returns 503 when WEBHOOK_ENABLED but no WEBHOOK_SECRET."""
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        webhook_enabled="true",
        task_cmd="echo pwned",
        webhook_secret=None,
    )
    resp = client.post("/api/webhook/run", json={"task": "generate", "params": {}})

    assert resp.status_code == 503
    payload = resp.get_json()
    assert "WEBHOOK_SECRET" in payload["error"]


def test_webhook_no_secret_does_not_execute_task(monkeypatch, tmp_path):
    """Webhook endpoint does not execute task when WEBHOOK_SECRET is missing."""
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        webhook_enabled="true",
        task_cmd='python3 -c "import sys;print(\'executed\')" {params.name}',
        webhook_secret=None,
    )
    resp = client.post(
        "/api/webhook/run",
        json={"task": "generate", "params": {"name": "test"}},
    )

    assert resp.status_code == 503
    payload = resp.get_json()
    assert "success" not in payload
    assert "stdout" not in payload


def test_webhook_no_secret_anonymous_request(monkeypatch, tmp_path):
    """Anonymous request without auth header returns 503 when no WEBHOOK_SECRET."""
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        webhook_enabled="true",
        task_cmd="echo pwned",
        webhook_secret=None,
    )
    # No Authorization header — anonymous request
    resp = client.post("/api/webhook/run", json={"task": "generate", "params": {}})

    assert resp.status_code == 503
    payload = resp.get_json()
    assert "WEBHOOK_SECRET" in payload["error"]


def test_webhook_no_secret_with_auth_header(monkeypatch, tmp_path):
    """Even with an Authorization header, no secret means 503."""
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        webhook_enabled="true",
        task_cmd="echo pwned",
        webhook_secret=None,
    )
    resp = client.post(
        "/api/webhook/run",
        json={"task": "generate", "params": {}},
        headers={"Authorization": "Bearer some-random-value"},
    )

    assert resp.status_code == 503
    payload = resp.get_json()
    assert "WEBHOOK_SECRET" in payload["error"]


def test_webhook_requires_bearer_token(monkeypatch, tmp_path):
    """Webhook endpoint rejects requests without valid bearer token."""
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd="echo hi",
    )
    # No Authorization header
    resp = client.post("/api/webhook/run", json={"task": "generate", "params": {}})

    assert resp.status_code == 401
    payload = resp.get_json()
    assert "secret" in payload["error"].lower()


def test_webhook_requires_correct_bearer_token(monkeypatch, tmp_path):
    """Webhook endpoint rejects requests with wrong bearer token."""
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd="echo hi",
    )
    resp = client.post(
        "/api/webhook/run",
        json={"task": "generate", "params": {}},
        headers={"Authorization": "Bearer wrong-secret"},
    )

    assert resp.status_code == 401
    payload = resp.get_json()
    assert "secret" in payload["error"].lower()


def test_webhook_run_with_auth_enabled_and_bearer_secret(monkeypatch, tmp_path):
    """With auth enabled, /api/webhook/run must accept a valid bearer secret
    without a browser session or CSRF token. See issue #401."""
    extra_env = {
        "WEBHOOK_ENABLED": "true",
        "WEBHOOK_SECRET": "test-secret-123",
        "WEBHOOK_TASK_GENERATE": 'python3 -c "import sys;print(\'ok-\'+sys.argv[1])" {params.name}',
    }
    client, _ = build_client(
        monkeypatch, tmp_path, auth_type="local", extra_env=extra_env,
    )

    # No session cookie, no X-CSRF-Token header — only the bearer secret.
    resp = client.post(
        "/api/webhook/run",
        json={"task": "generate", "params": {"name": "miso"}},
        headers={"Authorization": "Bearer test-secret-123"},
    )

    assert resp.status_code == 200, resp.get_data(as_text=True)
    payload = resp.get_json()
    assert payload["success"] is True
    assert payload["stdout"].strip() == "ok-miso"


def test_webhook_run_auth_enabled_rejects_wrong_bearer(monkeypatch, tmp_path):
    """With auth enabled, a missing/incorrect bearer token still fails closed
    even though /api/webhook/run is exempt from the session-auth redirect."""
    extra_env = {
        "WEBHOOK_ENABLED": "true",
        "WEBHOOK_SECRET": "test-secret-123",
        "WEBHOOK_TASK_GENERATE": "echo hi",
    }
    client, _ = build_client(
        monkeypatch, tmp_path, auth_type="local", extra_env=extra_env,
    )

    # No session, no bearer → must 401, not redirect to /login.
    resp_missing = client.post(
        "/api/webhook/run",
        json={"task": "generate", "params": {}},
    )
    assert resp_missing.status_code == 401

    # Wrong bearer with auth enabled → also 401.
    resp_wrong = client.post(
        "/api/webhook/run",
        json={"task": "generate", "params": {}},
        headers={"Authorization": "Bearer wrong-secret"},
    )
    assert resp_wrong.status_code == 401


# ---------------------------------------------------------------------------
# Regression tests for issue #448: per-worker webhook task concurrency cap
# ---------------------------------------------------------------------------
#
# `run_configured_task()` used to invoke the configured command via
# `subprocess.run(...)` synchronously in the request thread. With
# WEBHOOK_TASK_TIMEOUT capped at 120s and gunicorn --timeout set to 130s, a
# single webhook call could occupy a worker for up to two minutes. With the
# shipped default 2-worker pool, a webhook-secret holder (or anyone with a
# write-scoped LLM API key) could submit enough concurrent long-running task
# requests to saturate the pool and stall every other route — a
# worker-exhaustion DoS that requires no credentials beyond the legitimate
# webhook secret or write API key.
#
# The fix caps concurrent in-flight task invocations at 1 per gunicorn worker
# via a module-level BoundedSemaphore. Concurrent requests that arrive while
# a task is in flight are rejected with 503 instead of blocking the worker
# (or the gunicorn pool) indefinitely.


def test_webhook_task_rejects_concurrent_when_pool_saturated(monkeypatch, tmp_path):
    """Three concurrent webhook tasks must not all block the same worker.

    The first task acquires the per-worker slot and runs for ~1.5s. The two
    concurrent follow-up requests must be rejected with 503 immediately
    rather than queued behind it, so the worker stays responsive for other
    routes. The whole batch must complete well under WEBHOOK_TASK_TIMEOUT —
    saturation rejection is not allowed to block.
    """
    # Task sleeps 1.5s so the first request still holds the slot when the
    # other two concurrent requests arrive.
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd=(
            'python3 -c "import time, sys; '
            'time.sleep(1.5); print(\'done-\' + sys.argv[1])" {params.name}'
        ),
    )

    statuses: list[int] = []
    bodies: list[dict] = []
    lock = threading.Lock()

    def fire(label: str) -> None:
        resp = client.post(
            "/api/webhook/run",
            json={"task": "generate", "params": {"name": label}},
            headers={"Authorization": "Bearer test-secret-123"},
        )
        with lock:
            statuses.append(resp.status_code)
            bodies.append(resp.get_json())

    threads = [
        threading.Thread(target=fire, args=(f"t{i}",)) for i in range(3)
    ]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    elapsed = time.monotonic() - start

    # Sanity: the worker must NOT block until WEBHOOK_TASK_TIMEOUT (default
    # 30s here) — saturation rejection returns ~immediately.
    assert elapsed < 10, (
        f"Concurrent requests blocked the worker for {elapsed:.1f}s; "
        "saturation rejection did not short-circuit the queue."
    )

    # Exactly one task wins the slot and runs to completion; the other two
    # are rejected with 503 instead of being queued.
    assert statuses.count(200) == 1, statuses
    assert statuses.count(503) == 2, statuses

    # The 503 responses explain the saturation so callers can retry.
    for code, body in zip(statuses, bodies, strict=True):
        if code == 503:
            assert "saturated" in body["error"].lower(), body


def test_webhook_task_slot_released_after_completion(monkeypatch, tmp_path):
    """After an in-flight task completes, the slot must be released so
    subsequent requests can run. Without slot release a single bad task
    would permanently disable webhook tasks in this worker.
    """
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd=(
            'python3 -c "import time, sys; '
            'time.sleep(0.3); print(\'done-\' + sys.argv[1])" {params.name}'
        ),
    )

    # Three sequential tasks must all succeed — each acquires the slot,
    # runs, releases the slot, then the next request acquires it.
    for i in range(3):
        resp = client.post(
            "/api/webhook/run",
            json={"task": "generate", "params": {"name": f"t{i}"}},
            headers={"Authorization": "Bearer test-secret-123"},
        )
        assert resp.status_code == 200, resp.get_json()
        payload = resp.get_json()
        assert payload["success"] is True
        assert payload["stdout"].strip() == f"done-t{i}"


def test_webhook_task_slot_released_after_timeout(monkeypatch, tmp_path):
    """A timed-out task must still release the slot. Otherwise a single
    misconfigured task with a hung subprocess would permanently disable
    webhook tasks in this worker (the slot would never become available
    again).
    """
    # The configured command blocks far longer than the per-task timeout,
    # so subprocess.run() raises TimeoutExpired. Reset the timeout for the
    # second request so it can complete normally.
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd=(
            'python3 -c "import time, sys; '
            'time.sleep(5); print(\'done-\' + sys.argv[1])" {params.name}'
        ),
    )
    # Force the per-task timeout to a short, predictable value so the first
    # request times out quickly.
    monkeypatch.setenv("WEBHOOK_TASK_TIMEOUT", "1")

    # First task hits the 1s timeout and returns 504.
    resp_timeout = client.post(
        "/api/webhook/run",
        json={"task": "generate", "params": {"name": "hung"}},
        headers={"Authorization": "Bearer test-secret-123"},
    )
    assert resp_timeout.status_code == 504
    assert "timed out" in resp_timeout.get_json()["error"].lower()

    # Restore the timeout so the follow-up task can finish within budget,
    # and verify the slot was released by the `finally` clause.
    monkeypatch.setenv("WEBHOOK_TASK_TIMEOUT", "10")
    resp_ok = client.post(
        "/api/webhook/run",
        json={"task": "generate", "params": {"name": "ok"}},
        headers={"Authorization": "Bearer test-secret-123"},
    )
    assert resp_ok.status_code == 200, resp_ok.get_json()
    assert resp_ok.get_json()["stdout"].strip() == "done-ok"
