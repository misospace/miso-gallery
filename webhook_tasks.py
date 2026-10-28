"""Webhook task execution extracted from app.py (issue #497).

``run_configured_task`` and its helpers (task-name validation, env-key
resolution, command-template rendering, bearer-secret verification, and the
per-worker concurrency slot) keep their original behavior. The
``/api/webhook/run`` route stays in ``app.py`` and passes its
``log_security_event`` audit logger into :func:`run_configured_task` rather
than this module importing ``app``, so importing ``webhook_tasks`` never
becomes circular. ``DATA_FOLDER`` (the subprocess working directory) is
resolved through the live ``app`` module at call time so monkeypatching of
``app`` attributes stays effective.
"""

from __future__ import annotations

import os
import re
import secrets
import shlex
import subprocess
import threading

WEBHOOK_TASK_PREFIX = "WEBHOOK_TASK_"
_VALID_TASK_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# Per-process concurrency cap for webhook task subprocesses (issue #448).
#
# `run_configured_task()` invokes a configured command via `subprocess.run(...)`
# synchronously in the request thread. With `WEBHOOK_TASK_TIMEOUT` capped at 120s
# and gunicorn `--timeout` set to 130s, a single webhook call can occupy a worker
# for up to two minutes. The shipped default worker count is 2
# (entrypoint.sh, Dockerfile); a webhook-secret holder (or anyone with a
# write-scoped LLM API key) can submit enough concurrent long-running task
# requests to saturate the worker pool and stall every other route.
#
# Capping concurrent in-flight task invocations at 1 per gunicorn worker
# ensures any concurrent request that arrives while a task is already running
# is rejected with 503 immediately rather than blocking the worker (or the
# gunicorn pool) indefinitely. Each gunicorn worker process gets its own
# semaphore instance because module-level state is per-process.
WEBHOOK_TASK_MAX_CONCURRENT = 1
_webhook_task_slot = threading.BoundedSemaphore(WEBHOOK_TASK_MAX_CONCURRENT)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()


def _webhook_enabled() -> bool:
    return os.environ.get("WEBHOOK_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}

def _verify_webhook_secret(token: str) -> bool:
    """Verify Bearer token matches WEBHOOK_SECRET if configured."""
    if not WEBHOOK_SECRET:
        return False
    return secrets.compare_digest(token, WEBHOOK_SECRET)


def _task_env_key(task_name: str) -> str:
    normalized = "".join(ch if ch.isalnum() else "_" for ch in task_name).strip("_").upper()
    return f"{WEBHOOK_TASK_PREFIX}{normalized}" if normalized else ""


def _validate_task_name(task_name: str) -> str | None:
    """Validate that a task name only contains safe characters.

    Returns an error message string if validation fails, or None if valid.
    Only [a-zA-Z0-9_-] are permitted to prevent env var key collision and injection.
    """
    if not _VALID_TASK_RE.match(task_name):
        return (
            f"Invalid task name: '{task_name}'. "
            "Task names may only contain letters, digits, hyphens, and underscores."
        )
    return None


def _render_task_command(template: str, params: dict[str, object]) -> str:
    rendered = template
    for key, value in params.items():
        if isinstance(value, (dict, list)):
            raise ValueError(f"params.{key} must be a scalar value")
        rendered = rendered.replace(f"{{params.{key}}}", shlex.quote(str(value)))

    if "{params." in rendered:
        raise ValueError("missing required params for command template")

    return rendered


def _app():
    """Return the live ``app`` module.

    ``run_configured_task`` runs each task with ``cwd=str(DATA_FOLDER)``.
    ``DATA_FOLDER`` stays an ``app`` module attribute (tests rebind it), so
    resolving it through ``app`` at call time keeps monkeypatching effective
    and avoids a circular import.
    """
    import app

    return app


def run_configured_task(
    payload: dict[str, object], log_security_event
) -> tuple[dict[str, object], int]:
    if not _webhook_enabled():
        return {"error": "Webhook tasks are disabled"}, 404

    # Defense-in-depth (issue #574): both HTTP callers validate the body shape
    # with llm_api.json_object_payload() first; this keeps any future caller
    # from forwarding a parsed array/string/number into payload.get below.
    if not isinstance(payload, dict):
        return {"error": "JSON body must be an object"}, 400

    task = str(payload.get("task", "")).strip()
    params = payload.get("params") or {}

    if not task:
        return {"error": "task is required"}, 400
    if not isinstance(params, dict):
        return {"error": "params must be an object"}, 400

    validation_error = _validate_task_name(task)
    if validation_error is not None:
        return {"error": validation_error}, 400

    env_key = _task_env_key(task)
    if not env_key:
        return {"error": "invalid task name"}, 400

    template = os.environ.get(env_key, "").strip()
    if not template:
        return {"error": f"task '{task}' is not configured"}, 404

    try:
        command = _render_task_command(template, params)
        argv = shlex.split(command)
    except ValueError as exc:
        return {"error": str(exc)}, 400

    if not argv:
        return {"error": "configured task produced an empty command"}, 500

    try:
        timeout = max(1, min(120, int(os.environ.get("WEBHOOK_TASK_TIMEOUT", "30"))))
    except ValueError:
        timeout = 30

    # Per-worker concurrency cap (issue #448): refuse with 503 if a task is
    # already in flight in this gunicorn worker. Blocking until the slot is
    # free would let a single slow task pin the worker (or, with concurrent
    # request threads, the entire worker pool) for up to WEBHOOK_TASK_TIMEOUT
    # seconds — the very worker-exhaustion DoS this guard prevents.
    if not _webhook_task_slot.acquire(blocking=False):
        log_security_event(
            "webhook_task",
            "rejected",
            task=task,
            reason="pool_saturated",
            max_concurrent=WEBHOOK_TASK_MAX_CONCURRENT,
        )
        return {
            "task": task,
            "success": False,
            "error": (
                "webhook task pool saturated: at most "
                f"{WEBHOOK_TASK_MAX_CONCURRENT} task(s) may run concurrently per "
                "worker. Retry after the in-flight task completes."
            ),
        }, 503

    try:
        try:
            completed = subprocess.run(
                argv,
                cwd=str(_app().DATA_FOLDER),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log_security_event("webhook_task", "error", task=task, reason="timeout", timeout=timeout)
            return {"task": task, "success": False, "error": f"task timed out after {timeout}s"}, 504
        except OSError as exc:
            log_security_event("webhook_task", "error", task=task, reason="spawn_failed", error=str(exc))
            return {"task": task, "success": False, "error": f"failed to execute task: {exc}"}, 500

        success = completed.returncode == 0
        log_security_event("webhook_task", "success" if success else "error", task=task, exit_code=completed.returncode)
        return {
            "task": task,
            "success": success,
            "exitCode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }, 200
    finally:
        _webhook_task_slot.release()
