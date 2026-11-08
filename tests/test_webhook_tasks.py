import json
import shlex
import threading
import time

from conftest import auth_header, build_client


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


def test_webhook_task_param_key_cannot_span_adjacent_placeholders(monkeypatch, tmp_path):
    """Issue #634: a crafted param key must not span two operator placeholders."""
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd="python3 -c \"import sys;print('|'.join(sys.argv[1:]))\" --src {params.a} --mode {params.b}",
    )
    resp = client.post(
        "/api/webhook/run",
        json={
            "task": "generate",
            "params": {"a} --mode {params.b": "--evil", "a": "s", "b": "m"},
        },
        headers={"Authorization": "Bearer test-secret-123"},
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["success"] is True
    # Both operator tokens and both substitutions survive; the crafted key
    # matches no placeholder and is ignored.
    assert payload["stdout"].strip() == "--src|s|--mode|m"


def test_webhook_task_rejects_placeholder_key_outside_safe_set(monkeypatch, tmp_path):
    """Issue #634: exotic placeholder keys no longer render; they fail as missing params."""
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd="echo {params.a.b}",
    )
    resp = client.post(
        "/api/webhook/run",
        json={"task": "generate", "params": {"a.b": "x"}},
        headers={"Authorization": "Bearer test-secret-123"},
    )

    assert resp.status_code == 400
    assert "missing required params" in resp.get_json()["error"]


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


# ---------------------------------------------------------------------------
# Regression tests for issue #574: /api/webhook/run forwarded
# ``request.get_json(silent=True) or {}`` into run_configured_task, so a
# present non-object JSON body (array, string, number, boolean) reached
# ``payload.get`` and raised AttributeError -> HTTP 500 instead of a client
# validation error, and null/empty fell through to "task is required".
# ---------------------------------------------------------------------------


def test_webhook_run_rejects_non_object_json_bodies(monkeypatch, tmp_path):
    """#574: a valid bearer secret must still 400 on non-object JSON bodies.

    The body-shape check runs after the secret gate (so unauthenticated
    callers get 401, not body-validation detail) and before
    run_configured_task touches payload fields or spawns a subprocess.
    """
    import webhook_tasks

    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd='python3 -c "print(\'executed\')"',
    )

    def _boom(*args, **kwargs):
        raise AssertionError(f"task subprocess must not run: {args}")

    monkeypatch.setattr(webhook_tasks.subprocess, "run", _boom)

    for raw in ['["not-an-object"]', '"just-a-string"', "123", "true", "false", "0", "null", "{"]:
        resp = client.post(
            "/api/webhook/run",
            data=raw,
            content_type="application/json",
            headers={"Authorization": "Bearer test-secret-123"},
        )
        assert resp.status_code == 400, (raw, resp.status_code)
        assert "must be an object" in resp.get_json()["error"], raw

    # A body sent with a non-JSON content type is not parsed as JSON, so it
    # keeps the pre-fix empty-payload fallback ("task is required"), not the
    # body-shape rejection.
    resp_text = client.post(
        "/api/webhook/run",
        data='{"task": "generate"}',
        content_type="text/plain",
        headers={"Authorization": "Bearer test-secret-123"},
    )
    assert resp_text.status_code == 400
    assert resp_text.get_json()["error"] == "task is required"

    # A blank body (whitespace only) with a JSON content type carries no JSON
    # value, so it is an absent body rather than a malformed one: it keeps
    # the empty-payload fallback, not the body-shape rejection.
    for blank in ("   ", "\n"):
        resp_blank = client.post(
            "/api/webhook/run",
            data=blank,
            content_type="application/json",
            headers={"Authorization": "Bearer test-secret-123"},
        )
        assert resp_blank.status_code == 400, (blank, resp_blank.status_code)
        assert resp_blank.get_json()["error"] == "task is required", blank

    # The bearer-secret check still precedes body validation.
    resp_unauth = client.post(
        "/api/webhook/run",
        data="[]",
        content_type="application/json",
    )
    assert resp_unauth.status_code == 401
    assert "secret" in resp_unauth.get_json()["error"].lower()


# ---------------------------------------------------------------------------
# Regression tests for issue #630: _render_task_command substituted params by
# repeatedly calling str.replace over the whole rendered string, so a param
# value containing the literal text "{params.<other-key>}" was substituted a
# second time inside its own shlex.quote region. The colliding quote
# characters let shlex.split split that value into extra argv entries,
# injecting arguments into the operator-configured command.
# ---------------------------------------------------------------------------


def test_render_task_command_nested_placeholder_stays_single_argv_entry():
    """A value that is another key's placeholder must not re-expand."""
    from webhook_tasks import _render_task_command

    template = "process.sh --src {params.name} --mode {params.mode}"

    rendered = _render_task_command(template, {"name": "{params.mode}", "mode": "a b"})
    assert shlex.split(rendered) == [
        "process.sh",
        "--src",
        "{params.mode}",
        "--mode",
        "a b",
    ]

    # The exploit shape from the issue: the attacker-supplied value must not
    # become extra argv entries ("--evil-flag" parsed as its own argument).
    rendered = _render_task_command(
        template, {"name": "{params.mode}", "mode": "--evil-flag payload"}
    )
    assert shlex.split(rendered) == [
        "process.sh",
        "--src",
        "{params.mode}",
        "--mode",
        "--evil-flag payload",
    ]


def test_render_task_command_value_with_sentinel_text_stays_single_argv_entry():
    """A value containing the renderer's sentinel token text stays opaque."""
    from webhook_tasks import _render_task_command

    template = "process.sh --a {params.a} --b {params.b}"
    rendered = _render_task_command(template, {"a": "\x00params1\x00", "b": "evil arg"})
    assert shlex.split(rendered) == [
        "process.sh",
        "--a",
        "\x00params1\x00",
        "--b",
        "evil arg",
    ]


def test_webhook_run_nested_placeholder_value_is_single_argv_entry(monkeypatch, tmp_path):
    """End-to-end: through /api/webhook/run a nested {params.*} value must
    reach the task process as one opaque argv element."""
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd=(
            'python3 -c "import sys, json; print(json.dumps(sys.argv[1:]))" '
            "--src {params.name} --mode {params.mode}"
        ),
    )
    resp = client.post(
        "/api/webhook/run",
        json={
            "task": "generate",
            "params": {"name": "{params.mode}", "mode": "--evil-flag payload"},
        },
        headers={"Authorization": "Bearer test-secret-123"},
    )

    assert resp.status_code == 200, resp.get_json()
    payload = resp.get_json()
    assert payload["success"] is True
    assert json.loads(payload["stdout"]) == [
        "--src",
        "{params.mode}",
        "--mode",
        "--evil-flag payload",
    ]


# ---------------------------------------------------------------------------
# Regression tests for issue #635: a param value containing a NUL byte
# rendered as one quoted argv element, but subprocess.run raises an uncaught
# ValueError("embedded null byte") for such argv — escaping the
# TimeoutExpired/OSError handling in run_configured_task and surfacing as an
# HTTP 500 to the client. The spawn boundary now rejects NUL-bearing argv
# with a 400 validation error before the concurrency slot is consumed or any
# spawn is attempted. The renderer still treats NUL-bearing values as opaque
# single argv entries (issue #630 contract, unchanged); the execve invariant
# is enforced where the spawn happens, in run_configured_task.
# ---------------------------------------------------------------------------


def test_webhook_run_rejects_null_byte_param_value_without_spawning(monkeypatch, tmp_path):
    """#635: a NUL byte in a param value must return 400 with a clear
    validation message from /api/webhook/run, with no subprocess spawned and
    no concurrency slot burned. NUL-free values keep running normally."""
    import webhook_tasks

    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd='python3 -c "import sys; print(sys.argv[1])" {params.name}',
    )

    # NUL-free values are unaffected by the guard.
    ok = client.post(
        "/api/webhook/run",
        json={"task": "generate", "params": {"name": "plain"}},
        headers={"Authorization": "Bearer test-secret-123"},
    )
    assert ok.status_code == 200, ok.get_json()
    assert ok.get_json()["stdout"].strip() == "plain"

    real_run = webhook_tasks.subprocess.run

    def _boom(*args, **kwargs):
        raise AssertionError(f"task subprocess must not run: {args}")

    monkeypatch.setattr(webhook_tasks.subprocess, "run", _boom)
    resp = client.post(
        "/api/webhook/run",
        json={"task": "generate", "params": {"name": "a\0b"}},
        headers={"Authorization": "Bearer test-secret-123"},
    )

    assert resp.status_code == 400
    assert resp.get_json() == {"error": "param values must not contain NUL bytes"}

    # The rejected request must not have leaked the concurrency slot: with the
    # real spawn path restored, a normal task runs again instead of hitting
    # the 503 pool-saturation guard.
    monkeypatch.setattr(webhook_tasks.subprocess, "run", real_run)
    after = client.post(
        "/api/webhook/run",
        json={"task": "generate", "params": {"name": "plain"}},
        headers={"Authorization": "Bearer test-secret-123"},
    )
    assert after.status_code == 200, after.get_json()
    assert after.get_json()["stdout"].strip() == "plain"


def test_llm_task_run_rejects_null_byte_param_value(monkeypatch, tmp_path):
    """#635: /api/llm/task/run must also return 400 (not 500) for a
    NUL-bearing param value, with no subprocess spawned."""
    import webhook_tasks

    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd='python3 -c "import sys; print(sys.argv[1])" {params.name}',
    )

    def _boom(*args, **kwargs):
        raise AssertionError(f"task subprocess must not run: {args}")

    monkeypatch.setattr(webhook_tasks.subprocess, "run", _boom)
    resp = client.post(
        "/api/llm/task/run",
        json={"task": "generate", "params": {"name": "a\0b"}},
        headers=auth_header(),
    )

    assert resp.status_code == 400
    assert resp.get_json() == {"error": "param values must not contain NUL bytes"}


# ---------------------------------------------------------------------------
# Regression tests for issue #656: _verify_webhook_secret compared the bearer
# token and WEBHOOK_SECRET as `str`, and secrets.compare_digest accepts ASCII
# text only — a non-ASCII token raised TypeError, which escaped the
# /api/webhook/run route as an HTTP 500 instead of the normal 401. The
# comparison now runs over the UTF-8 byte encodings of both sides, so it stays
# constant time and never raises on non-ASCII text.
# ---------------------------------------------------------------------------


def test_webhook_run_non_ascii_bearer_token_returns_401_not_500(monkeypatch, tmp_path):
    """#656: a non-ASCII bearer token must 401 with the normal unauthorized
    body, not 500."""
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd="echo hi",
    )
    # Render the unhandled TypeError as the 500 response it becomes in
    # production, so this asserts the status code instead of only "no raise".
    client.application.config["PROPAGATE_EXCEPTIONS"] = False

    resp = client.post(
        "/api/webhook/run",
        json={"task": "generate", "params": {}},
        headers={"Authorization": "Bearer sécret"},
    )

    assert resp.status_code == 401
    assert resp.get_json() == {"error": "Webhook secret required"}


def test_webhook_run_non_ascii_token_mismatch_returns_401(monkeypatch, tmp_path):
    """#656: a non-ASCII token must also 401 against a non-ASCII secret."""
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd="echo hi",
        webhook_secret="sécret",
    )
    client.application.config["PROPAGATE_EXCEPTIONS"] = False

    resp = client.post(
        "/api/webhook/run",
        json={"task": "generate", "params": {}},
        headers={"Authorization": "Bearer sécret-mismatch"},
    )

    assert resp.status_code == 401
    assert resp.get_json() == {"error": "Webhook secret required"}


def test_webhook_run_non_ascii_secret_authenticates_and_runs_task(monkeypatch, tmp_path):
    """#656: a non-ASCII secret must still authenticate and run its task."""
    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd="echo ok",
        webhook_secret="sécret",
    )
    client.application.config["PROPAGATE_EXCEPTIONS"] = False

    resp = client.post(
        "/api/webhook/run",
        json={"task": "generate", "params": {}},
        headers={"Authorization": "Bearer sécret"},
    )

    assert resp.status_code == 200, resp.get_json()
    payload = resp.get_json()
    assert payload["success"] is True
    assert payload["stdout"].strip() == "ok"


def test_webhook_secret_comparison_runs_over_bytes(monkeypatch):
    """#656: the secret comparison must be made over byte encodings."""
    import secrets as secrets_module

    import webhook_tasks

    calls = []
    real_compare_digest = secrets_module.compare_digest

    def spy(left, right):
        calls.append((left, right))
        return real_compare_digest(left, right)

    monkeypatch.setattr(webhook_tasks.secrets, "compare_digest", spy)

    assert webhook_tasks._webhook_secret_matches("sécret", "sécret") is True
    assert webhook_tasks._webhook_secret_matches("sécret", "other-secret") is False

    assert len(calls) == 2
    for left, right in calls:
        assert isinstance(left, bytes)
        assert isinstance(right, bytes)
    assert calls[0][0] == "sécret".encode("utf-8")
    assert calls[0][1] == "sécret".encode("utf-8")


def test_webhook_secret_with_non_utf8_env_byte_fails_closed(monkeypatch, tmp_path):
    """#656: a secret holding a non-UTF-8 environment byte must not raise.

    Such a secret carries a lone surrogate (POSIX decodes environment values
    with surrogateescape); encoding it must not blow up, and no request token
    can match it, so the request fails closed with 401.
    """
    import webhook_tasks

    secret = "\udcff"
    assert webhook_tasks._webhook_secret_matches("sécret", secret) is False

    client = _build_webhook_client(
        monkeypatch, tmp_path,
        task_cmd="echo hi",
        webhook_secret=secret,
    )
    client.application.config["PROPAGATE_EXCEPTIONS"] = False

    resp = client.post(
        "/api/webhook/run",
        json={"task": "generate", "params": {}},
        headers={"Authorization": "Bearer sécret"},
    )

    assert resp.status_code == 401
    assert resp.get_json() == {"error": "Webhook secret required"}
