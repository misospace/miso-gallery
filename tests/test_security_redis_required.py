"""Tests for the cross-worker rate-limit safety guard (issue #419).

The ``_build_primary_limiter`` factory must refuse to silently degrade to the
in-memory ``InMemoryRateLimiter`` whenever the deployment looks production-shaped,
because that limiter is per-process and would multiply the effective rate-limit
quota by the number of gunicorn workers. The shipped configuration must not be
the weak one by default.

These tests exercise ``_build_primary_limiter`` directly so they do not depend
on environment state leaking from the rest of the suite.
"""

from __future__ import annotations

import importlib
import sys

import pytest


def _reload_security(monkeypatch, **env):
    """Reload ``security`` with the given environment vars set."""
    for key in (
        "REDIS_URL",
        "RATE_LIMIT_REDIS_URL",
        "ALLOW_INMEMORY_RATE_LIMIT",
        "WEB_CONCURRENCY",
        "RATE_LIMIT_PREFIX",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    sys.modules.pop("security", None)
    return importlib.import_module("security")


def test_build_primary_limiter_refuses_without_redis(monkeypatch, caplog):
    """No Redis URL and no opt-in flag must raise, not warn-and-fall-back."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("RATE_LIMIT_REDIS_URL", raising=False)
    monkeypatch.delenv("ALLOW_INMEMORY_RATE_LIMIT", raising=False)

    from security import _build_primary_limiter

    with caplog.at_level("WARNING"), pytest.raises(RuntimeError) as excinfo:
        _build_primary_limiter()

    message = str(excinfo.value)
    assert "REDIS_URL" in message or "RATE_LIMIT_REDIS_URL" in message
    # The error must explain the security consequence, not just be a stack trace.
    assert "per-process" in message or "in-memory" in message


def test_build_primary_limiter_refuses_with_multi_worker_and_opt_in(monkeypatch):
    """ALLOW_INMEMORY_RATE_LIMIT=1 with WEB_CONCURRENCY>1 must still refuse."""
    security = _reload_security(
        monkeypatch,
        ALLOW_INMEMORY_RATE_LIMIT="1",
        WEB_CONCURRENCY="4",
    )

    with pytest.raises(RuntimeError) as excinfo:
        security._build_primary_limiter()

    message = str(excinfo.value)
    assert "WEB_CONCURRENCY" in message
    assert "per-worker" in message or "per-process" in message


def test_build_primary_limiter_allows_single_worker_with_opt_in(monkeypatch):
    """ALLOW_INMEMORY_RATE_LIMIT=1 with WEB_CONCURRENCY=1 returns the fallback."""
    security = _reload_security(
        monkeypatch,
        ALLOW_INMEMORY_RATE_LIMIT="1",
        WEB_CONCURRENCY="1",
    )

    limiter = security._build_primary_limiter()

    assert isinstance(limiter, security.InMemoryRateLimiter)
    assert limiter is security.FALLBACK_LIMITER


def test_build_primary_limiter_handles_garbage_worker_count(monkeypatch):
    """Non-integer WEB_CONCURRENCY is treated as 1 and the error still mentions it."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("RATE_LIMIT_REDIS_URL", raising=False)
    monkeypatch.delenv("ALLOW_INMEMORY_RATE_LIMIT", raising=False)
    monkeypatch.setenv("WEB_CONCURRENCY", "not-a-number")

    from security import _build_primary_limiter

    with pytest.raises(RuntimeError):
        _build_primary_limiter()


def test_build_primary_limiter_treats_truthy_opt_in_case_insensitively(monkeypatch):
    """ALLOW_INMEMORY_RATE_LIMIT='True' and 'YES' must both opt in for single worker."""
    for truthy in ("true", "True", "YES", "yes"):
        security = _reload_security(
            monkeypatch,
            ALLOW_INMEMORY_RATE_LIMIT=truthy,
            WEB_CONCURRENCY="1",
        )
        limiter = security._build_primary_limiter()
        assert isinstance(limiter, security.InMemoryRateLimiter), (
            f"opt-in value {truthy!r} should enable the fallback"
        )
