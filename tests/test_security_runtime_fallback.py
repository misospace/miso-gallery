"""Runtime behavior of the rate_limit decorator when the primary limiter fails (issue #508).

The decorator used to degrade silently to the per-process in-memory
``FALLBACK_LIMITER`` whenever the primary (Redis) limiter raised at runtime.
That contradicts the module's fail-closed startup stance: without an explicit
``ALLOW_INMEMORY_RATE_LIMIT`` opt-in, a runtime limiter failure must deny the
request (503) rather than silently multiply the effective quota across workers.
"""

from __future__ import annotations

import logging

import pytest
from flask import Flask

import security
from security import FALLBACK_LIMITER


def setup_function():
    """Reset shared limiter state so tests do not leak into each other."""
    security._primary_limiter = None
    FALLBACK_LIMITER.reset()


class _FailingLimiter:
    """Fake primary limiter whose ``allow`` always raises (e.g. Redis is down)."""

    def __init__(self) -> None:
        self.calls = 0

    def allow(self, key: str, max_requests: int, window: int) -> bool:
        self.calls += 1
        raise RuntimeError("redis connection refused")


def _build_app(max_requests: int = 2, window: int = 60) -> tuple[Flask, list[bool]]:
    """Minimal Flask app with a single rate-limited route (repo decorator order).

    Returns ``(app, handler_ran)`` where ``handler_ran`` is a one-element list
    used as a mutable flag: the route body sets ``handler_ran[0] = True`` when it
    executes. A denied request (503/429) short-circuits the decorator before the
    body runs, so the flag stays ``False``.
    """
    app = Flask(__name__)
    app.config["TESTING"] = True
    handler_ran = [False]

    @app.route("/limited")
    @security.rate_limit(max_requests=max_requests, window=window)
    def limited() -> str:
        handler_ran[0] = True
        return "ok"

    return app, handler_ran


def test_runtime_failure_fails_closed_without_opt_in(monkeypatch, caplog):
    """Primary limiter failure without ALLOW_INMEMORY_RATE_LIMIT -> 503, no fallback use."""
    monkeypatch.delenv("ALLOW_INMEMORY_RATE_LIMIT", raising=False)
    failing = _FailingLimiter()
    monkeypatch.setattr(security, "_primary_limiter", failing)

    # Behavior spy: count every call to the shared in-memory fallback. The
    # fail-closed path must never consume the fallback, even though the primary
    # limiter raised. The spy delegates to the real limiter and counts invocations
    # rather than poking at the private ``_storage`` state.
    original_allow = FALLBACK_LIMITER.allow
    fallback_calls = 0

    def _spy_allow(key, max_requests, window):
        nonlocal fallback_calls
        fallback_calls += 1
        return original_allow(key, max_requests, window)

    monkeypatch.setattr(FALLBACK_LIMITER, "allow", _spy_allow)

    app, handler_ran = _build_app()
    client = app.test_client()

    with caplog.at_level(logging.ERROR, logger="security"):
        resp = client.get("/limited")

    # The request is denied and the route handler never ran.
    assert resp.status_code == 503
    body = resp.get_json()
    assert isinstance(body.get("error"), str) and body["error"]
    assert not handler_ran[0]

    # No in-memory fallback consumption: the shared fallback was never called.
    assert fallback_calls == 0
    assert failing.calls == 1

    # A loud error record explains the fail-closed denial and how to opt in.
    # Relax the count to "at least one" so a single request still yields the
    # operator-facing guidance even if the module logs more than one record.
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records, "expected at least one ERROR record for the fail-closed denial"
    messages = [r.getMessage() for r in error_records]
    assert any(
        "redis connection refused" in m
        and "ALLOW_INMEMORY_RATE_LIMIT=1" in m
        and "WEB_CONCURRENCY=1" in m
        for m in messages
    ), f"no ERROR record with the fail-closed guidance; got: {messages!r}"


@pytest.mark.parametrize("opt_in_value", ("1", "true", "YES"))
def test_runtime_failure_uses_fallback_with_opt_in(monkeypatch, opt_in_value):
    """With ALLOW_INMEMORY_RATE_LIMIT set, a runtime failure degrades to the fallback."""
    monkeypatch.setenv("ALLOW_INMEMORY_RATE_LIMIT", opt_in_value)
    monkeypatch.setattr(security, "_primary_limiter", _FailingLimiter())

    app, handler_ran = _build_app(max_requests=2)
    client = app.test_client()

    # Two requests pass through the in-memory fallback (limit is 2 / 60s)...
    assert client.get("/limited").status_code == 200
    assert client.get("/limited").status_code == 200
    # ...and the third is denied by the fallback limiter itself.
    resp = client.get("/limited")
    assert resp.status_code == 429
    assert resp.get_json()["error"] == "Rate limit exceeded"

    # The route handler did run for the requests the fallback let through.
    assert handler_ran[0]


def test_first_build_failure_fails_closed_without_opt_in(monkeypatch):
    """First-build failure: no Redis and no opt-in -> get_primary_limiter() raises
    RuntimeError on the first build, so the request is denied (503) instead of
    silently falling back to the per-process in-memory limiter.

    This pins the lazy first-build path (security._primary_limiter is None) as
    distinct from a runtime failure after a previously-healthy build.
    """
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("RATE_LIMIT_REDIS_URL", raising=False)
    monkeypatch.delenv("ALLOW_INMEMORY_RATE_LIMIT", raising=False)
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.setattr(security, "_primary_limiter", None)

    app, handler_ran = _build_app()
    client = app.test_client()

    resp = client.get("/limited")

    # The primary limiter could not be built on first use -> fail closed.
    assert resp.status_code == 503
    assert not handler_ran[0]


def test_first_build_failure_uses_fallback_with_opt_in(monkeypatch):
    """Same first-build setup but WITH ALLOW_INMEMORY_RATE_LIMIT=1 and
    WEB_CONCURRENCY=1 -> the in-memory limiter is built and the route is served.
    """
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("RATE_LIMIT_REDIS_URL", raising=False)
    monkeypatch.setenv("ALLOW_INMEMORY_RATE_LIMIT", "1")
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    monkeypatch.setattr(security, "_primary_limiter", None)

    app, handler_ran = _build_app()
    client = app.test_client()

    resp = client.get("/limited")

    assert resp.status_code == 200
    assert handler_ran[0]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("Yes", True),
        ("yes", True),
        (" 1 ", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("", False),
    ],
)
def test_allow_inmemory_opt_in_values(monkeypatch, value, expected):
    """The shared helper accepts exactly the truthy values the module uses elsewhere."""
    monkeypatch.setenv("ALLOW_INMEMORY_RATE_LIMIT", value)
    assert security._allow_inmemory_opt_in() is expected


def test_allow_inmemory_opt_in_unset(monkeypatch):
    """Unset env var means no opt-in."""
    monkeypatch.delenv("ALLOW_INMEMORY_RATE_LIMIT", raising=False)
    assert security._allow_inmemory_opt_in() is False
