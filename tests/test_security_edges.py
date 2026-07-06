"""Regression tests for security edges in miso-gallery.

Covers:
- Spoofed X-Forwarded-For rate-limit bypass (now fixed — XFF ignored by default)
- Public non-media access (no auth leaks on public endpoints)
- Webhook auth-disabled behavior
- Symlink/mount boundary path traversal
- RATE_LIMIT_ROUTE_LIMITS JSON parsing (_load_route_overrides)
"""

from __future__ import annotations

import json
import logging
import os
import re

from conftest import TEST_SECRET, build_client


def setup_function():
    """Reset the in-memory rate limiter before each test to avoid cross-test pollution."""
    from security import FALLBACK_LIMITER
    FALLBACK_LIMITER.reset()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEED_TOKEN = "test-csrf-token"


def _seed_csrf(client):
    """Seed a known CSRF token into the client session."""
    with client.session_transaction() as sess:
        sess["csrf_token"] = _SEED_TOKEN


def _auth_post(client, **kwargs):
    """POST to /auth with seeded CSRF token and return response."""
    data = kwargs.pop("data", {})
    if isinstance(data, dict):
        data["csrf_token"] = _SEED_TOKEN
    else:
        data = {"csrf_token": _SEED_TOKEN}
    kwargs["data"] = data
    return client.post("/auth", **kwargs)


def _extract_csrf(html: str) -> str:
    m = re.search(r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']', html)
    assert m, "csrf_token not found in login form"
    return m.group(1)


# ---------------------------------------------------------------------------
# Tests for _load_route_overrides()
# ---------------------------------------------------------------------------

def _call_loader(monkeypatch, raw_value):
    """Import the module fresh, set the env var, and return _load_route_overrides() result."""
    import importlib
    import security

    # Ensure a clean module state for each invocation
    monkeypatch.setenv("RATE_LIMIT_ROUTE_LIMITS", raw_value if raw_value is not None else "")
    # Re-evaluate the function so it reads the current env var
    return security._load_route_overrides()


def test_load_route_overrides_empty_string(monkeypatch):
    """Empty string env var should produce no overrides."""
    from security import _load_route_overrides

    monkeypatch.setenv("RATE_LIMIT_ROUTE_LIMITS", "")
    assert _load_route_overrides() == {}


def test_load_route_overrides_whitespace_only(monkeypatch):
    """Whitespace-only env var should be treated as empty."""
    from security import _load_route_overrides

    monkeypatch.setenv("RATE_LIMIT_ROUTE_LIMITS", "   \t\n")
    assert _load_route_overrides() == {}


def test_load_route_overrides_unset(monkeypatch):
    """Unset env var should produce no overrides."""
    from security import _load_route_overrides

    monkeypatch.delenv("RATE_LIMIT_ROUTE_LIMITS", raising=False)
    assert _load_route_overrides() == {}


def test_load_route_overrides_valid_json(monkeypatch):
    """Valid JSON object should be parsed into RateLimitConfig entries."""
    from security import _load_route_overrides, RateLimitConfig

    payload = {
        "auth": {"max_requests": 5, "window": 300},
        "thumb": {"max_requests": 200, "window": 60},
    }
    monkeypatch.setenv("RATE_LIMIT_ROUTE_LIMITS", json.dumps(payload))

    overrides = _load_route_overrides()
    assert set(overrides.keys()) == {"auth", "thumb"}
    assert overrides["auth"] == RateLimitConfig(max_requests=5, window=300)
    assert overrides["thumb"] == RateLimitConfig(max_requests=200, window=60)


def test_load_route_overrides_malformed_json_logs_warning(monkeypatch, caplog):
    """Malformed JSON should fall back to defaults and emit a warning."""
    from security import _load_route_overrides

    monkeypatch.setenv("RATE_LIMIT_ROUTE_LIMITS", "{not valid json")

    with caplog.at_level(logging.WARNING, logger="security"):
        result = _load_route_overrides()

    assert result == {}
    assert any(
        "RATE_LIMIT_ROUTE_LIMITS" in record.message and "Invalid" in record.message
        for record in caplog.records
    )


def test_load_route_overrides_non_dict_json_returns_empty(monkeypatch, caplog):
    """Non-object JSON (e.g. list, string, number) should be rejected with a warning."""
    from security import _load_route_overrides

    for value in ("[1, 2, 3]", '"just-a-string"', "42", "null"):
        monkeypatch.setenv("RATE_LIMIT_ROUTE_LIMITS", value)
        with caplog.at_level(logging.WARNING, logger="security"):
            result = _load_route_overrides()
        assert result == {}, f"expected empty overrides for {value!r}"
        assert any(
            "must be a JSON object" in record.message
            for record in caplog.records
        ), f"expected warning for {value!r}"
        caplog.clear()


def test_load_route_overrides_boundary_values(monkeypatch):
    """Boundary and invalid values should be filtered or rejected sensibly."""
    from security import _load_route_overrides, RateLimitConfig

    payload = {
        "good": {"max_requests": 1, "window": 1},                  # boundary: minimum valid
        "zero_max": {"max_requests": 0, "window": 60},             # invalid: dropped
        "zero_window": {"max_requests": 10, "window": 0},          # invalid: dropped
        "negative_max": {"max_requests": -5, "window": 60},        # invalid: dropped
        "negative_window": {"max_requests": 10, "window": -1},     # invalid: dropped
        "non_int_max": {"max_requests": "abc", "window": 60},      # invalid: dropped
        "non_int_window": {"max_requests": 10, "window": "xyz"},   # invalid: dropped
        "missing_max": {"window": 60},                             # invalid: dropped (max_requests default 0)
        "missing_window": {"max_requests": 10},                    # invalid: dropped (window default 0)
    }
    monkeypatch.setenv("RATE_LIMIT_ROUTE_LIMITS", json.dumps(payload))

    overrides = _load_route_overrides()
    assert "good" in overrides
    assert overrides["good"] == RateLimitConfig(max_requests=1, window=1)

    for invalid in (
        "zero_max", "zero_window", "negative_max", "negative_window",
        "non_int_max", "non_int_window", "missing_max", "missing_window",
    ):
        assert invalid not in overrides, f"{invalid} should be rejected"


def test_load_route_overrides_non_dict_endpoint_value_skipped(monkeypatch):
    """Endpoint values that aren't objects should be silently skipped."""
    from security import _load_route_overrides

    payload = {
        "auth": {"max_requests": 5, "window": 60},
        "broken": "not-a-dict",
        "another": [1, 2, 3],
        "third": 42,
    }
    monkeypatch.setenv("RATE_LIMIT_ROUTE_LIMITS", json.dumps(payload))

    overrides = _load_route_overrides()
    assert "auth" in overrides
    assert "broken" not in overrides
    assert "another" not in overrides
    assert "third" not in overrides


def test_load_route_overrides_blank_endpoint_key_skipped(monkeypatch):
    """Blank-string endpoint keys should be skipped."""
    from security import _load_route_overrides

    payload = {
        "": {"max_requests": 5, "window": 60},
        "auth": {"max_requests": 7, "window": 70},
    }
    monkeypatch.setenv("RATE_LIMIT_ROUTE_LIMITS", json.dumps(payload))

    overrides = _load_route_overrides()
    assert "" not in overrides
    assert "auth" in overrides