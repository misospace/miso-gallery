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

# Command-template placeholders are matched template-side with a restricted
# key pattern (issue #634). Per-key literal replacement let a caller-supplied
# key containing `} ... {params.` match a span across two adjacent operator
# placeholders and delete operator template text.
_PARAM_PLACEHOLDER_RE = re.compile(r"\{params\.([A-Za-z0-9_-]+)\}")

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
    """Substitute ``{params.<key>}`` placeholders with shell-quoted values.

    Placeholders are matched template-side with a restricted key pattern
    (``[A-Za-z0-9_-]+``, issue #634), never by literal string replacement of
    caller-supplied keys: a key containing ``} ... {params.`` could otherwise
    span two adjacent operator placeholders and swallow template text.
    Behavior change for exotic param keys: keys outside ``[A-Za-z0-9_-]`` no
    longer match a placeholder, so a template placeholder written with such a
    key now fails the missing-params check instead of rendering.

    Rendering runs in two passes so a param value can never be re-substituted
    (issue #630). Pass 1 replaces each matched placeholder with a NUL-delimited
    sentinel token; NUL cannot appear in a template read from the environment
    (env values are NUL-terminated C strings), so placeholders left in the
    template afterwards are genuinely missing params. Pass 2 splices the
    ``shlex.quote``d values in with a single ``re.sub`` pass: substituted text
    is never rescanned, so a value containing the literal text
    ``{params.<other-key>}`` (or sentinel text) stays one opaque argv element
    instead of being expanded inside its own quotes and split by
    ``shlex.split`` into extra arguments.
    """
    quoted_by_sentinel: dict[str, str] = {}
    for key, value in params.items():
        if isinstance(value, (dict, list)):
            raise ValueError(f"params.{key} must be a scalar value")

    def _to_sentinel(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in params:
            return match.group(0)
        sentinel = f"\x00params{len(quoted_by_sentinel)}\x00"
        quoted_by_sentinel[sentinel] = shlex.quote(str(params[key]))
        return sentinel

    rendered = _PARAM_PLACEHOLDER_RE.sub(_to_sentinel, template)

    if "{params." in rendered:
        raise ValueError("missing required params for command template")

    if not quoted_by_sentinel:
        return rendered

    sentinel_re = re.compile("|".join(map(re.escape, quoted_by_sentinel)))
    return sentinel_re.sub(lambda match: quoted_by_sentinel[match.group(0)], rendered)


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

    # Spawn-boundary guard (issue #635): execve forbids NUL bytes in argv
    # strings, so subprocess.run would raise an uncaught
    # ValueError("embedded null byte") that escapes the only-
    # TimeoutExpired/OSError handling below and surfaces as an HTTP 500.
    # Template text cannot contain NUL (env values are NUL-terminated C
    # strings), so any NUL in argv arrived in a param value: reject it as
    # client input before the concurrency slot is consumed or any spawn is
    # attempted. The renderer keeps NUL-bearing values as opaque single argv
    # entries (issue #630); the execve invariant is enforced here, where the
    # spawn happens.
    if any("\x00" in arg for arg in argv):
        return {"error": "param values must not contain NUL bytes"}, 400

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
