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
# 1. Spoofed X-Forwarded-For rate-limit bypass (now fixed)
# ---------------------------------------------------------------------------

def test_xff_no_longer_bypasses_rate_limit(monkeypatch, tmp_path):
    """Regression: XFF rotation should NO LONGER bypass rate limits.

    After the trusted-proxy fix, _client_ip() returns request.remote_addr
    by default — X-Forwarded-For is ignored unless the source is a trusted
    proxy.  All requests from the test client share remote_addr=127.0.0.1,
    so they all hit the same rate-limit key regardless of XFF values.
    """
    client, _ = build_client(monkeypatch, tmp_path, auth_type="none")
    _seed_csrf(client)

    # The /auth endpoint is rate-limited at 5 requests / 300s.
    # Even with different XFF headers, all requests share the same remote_addr
    # so the 6th request should be rate-limited.
    for i in range(5):
        resp = _auth_post(client, headers={"X-Forwarded-For": f"10.0.{i}.1"})
        assert resp.status_code == 302

    # A 6th request with a *different* XFF should now be rate-limited —
    # proving that XFF rotation no longer bypasses the limit.
    resp = _auth_post(client, headers={"X-Forwarded-For": "10.99.0.1"})
    assert resp.status_code == 429


def test_xff_single_ip_hits_rate_limit(monkeypatch, tmp_path):
    """Same XFF value must be rate-limited consistently."""
    client, _ = build_client(monkeypatch, tmp_path, auth_type="none")
    _seed_csrf(client)

    xff = "192.168.1.100"
    # Send 5 requests (the login rate limit is 5/300s)
    for _i in range(5):
        resp = _auth_post(client, headers={"X-Forwarded-For": xff})
        assert resp.status_code == 302

    # 6th request with same IP should be rate-limited
    resp = _auth_post(client, headers={"X-Forwarded-For": xff})
    assert resp.status_code == 429
    payload = resp.get_json()
    assert payload["error"] == "Rate limit exceeded"


def test_xff_empty_uses_remote_addr(monkeypatch, tmp_path):
    """When XFF is absent, _client_ip falls back to remote_addr."""
    client, _ = build_client(monkeypatch, tmp_path, auth_type="none")
    _seed_csrf(client)

    # No XFF header — should use the test client's remote_addr (127.0.0.1)
    for _i in range(5):
        resp = _auth_post(client)

    # 6th request without XFF should be rate-limited (same remote_addr)
    resp = _auth_post(client)
    assert resp.status_code == 429


def test_xff_multiple_hops_uses_first(monkeypatch, tmp_path):
    """X-Forwarded-For with multiple comma-separated IPs uses the first — but only from trusted proxies.

    From untrusted sources, _client_ip() ignores XFF entirely and falls back
    to remote_addr.  All requests share remote_addr=127.0.0.1, so they hit
    the same rate-limit key regardless of XFF content.
    """
    client, _ = build_client(monkeypatch, tmp_path, auth_type="none")
    _seed_csrf(client)

    # Send 5 requests with multi-hop XFF — all share the same remote_addr
    for _i in range(5):
        xff = "10.1.2.3, 10.9.9.9, 10.8.8.8"
        resp = _auth_post(client, headers={"X-Forwarded-For": xff})
        assert resp.status_code == 302

    # 6th request with different XFF content should still be rate-limited
    # because XFF is ignored from untrusted sources.
    resp = _auth_post(
        client,
        headers={"X-Forwarded-For": "10.1.2.3, 10.9.9.8, 10.8.8.7"},
    )
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# 2. Public non-media access
# ---------------------------------------------------------------------------

def test_public_endpoints_no_auth_header_required(monkeypatch, tmp_path):
    """Public endpoints (/recent) should not require authentication when auth is disabled."""
    client, _ = build_client(monkeypatch, tmp_path, auth_type="none")

    # These should work without any auth header
    resp = client.get("/recent")
    assert resp.status_code == 200


def test_public_endpoints_no_sensitive_data_in_response(monkeypatch, tmp_path):
    """Public HTML responses should not leak secrets, session tokens, or config."""
    client, _ = build_client(monkeypatch, tmp_path, auth_type="none")

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # Should not leak SECRET_KEY or admin password
    assert TEST_SECRET not in html
    assert "pass123" not in html.lower()

    # Should not expose internal env vars or debug info
    assert "os.environ" not in html


def test_media_endpoint_no_auth_required(monkeypatch, tmp_path):
    """Public image serving (/images/*) should work without auth."""
    client, _ = build_client(monkeypatch, tmp_path, auth_type="none")

    resp = client.get("/images/sample.png")
    assert resp.status_code == 200
    # Should return image content
    assert resp.content_type.startswith("image/")


# ---------------------------------------------------------------------------
# 3. Webhook auth-disabled behavior
# ---------------------------------------------------------------------------

def test_webhook_disabled_returns_404(monkeypatch, tmp_path):
    """When WEBHOOK_ENABLED=false, webhook endpoints should return 404."""
    client, _ = build_client(
        monkeypatch, tmp_path,
        auth_type="none",
        extra_env={"WEBHOOK_ENABLED": "false"},
    )

    resp = client.post("/api/webhook/run", json={"task": "generate"})
    assert resp.status_code == 404


def test_webhook_enabled_accepts_requests(monkeypatch, tmp_path):
    """When WEBHOOK_ENABLED=true, webhook endpoint should accept requests."""
    client, _ = build_client(
        monkeypatch, tmp_path,
        auth_type="none",
        extra_env={
            "WEBHOOK_ENABLED": "true",
            "WEBHOOK_TASK_GENERATE": "echo {params.name}",
        },
    )

    resp = client.post("/api/webhook/run", json={
        "task": "generate",
        "params": {"name": "test"},
    })
    assert resp.status_code == 200


def test_webhook_no_task_rejected(monkeypatch, tmp_path):
    """Webhook POST without a 'task' field should be rejected."""
    client, _ = build_client(
        monkeypatch, tmp_path,
        auth_type="none",
        extra_env={"WEBHOOK_ENABLED": "true"},
    )

    resp = client.post("/api/webhook/run", json={})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 4. Symlink / mount boundary path traversal
# ---------------------------------------------------------------------------

def test_sanitize_path_rejects_dotdot(monkeypatch, tmp_path):
    """sanitize_path should reject paths containing '..'."""
    from security import sanitize_path

    assert sanitize_path("normal.png") is True
    assert sanitize_path("../etc/passwd") is False
    assert sanitize_path("images/../../secret") is False
    assert sanitize_path("") is True  # empty is safe (handled elsewhere)


def test_sanitize_path_rejects_leading_slash(monkeypatch, tmp_path):
    """sanitize_path should reject absolute paths."""
    from security import sanitize_path

    assert sanitize_path("/etc/passwd") is False
    assert sanitize_path("//etc/passwd") is False


def test_sanitize_path_rejects_null_bytes(monkeypatch, tmp_path):
    """sanitize_path should strip null bytes (but not make path safe on its own)."""
    from security import sanitize_path

    # Null byte injection — sanitize_path strips nulls but '..' still present
    assert sanitize_path("normal\x00.png") is True  # null stripped, becomes "normal.png"
    assert sanitize_path("../secret\x00.png") is False  # ".." still present


def test_images_route_rejects_path_traversal(monkeypatch, tmp_path):
    """The /images/ endpoint should not serve files outside DATA_FOLDER via '..'."""
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")

    # Try to escape the data directory
    resp = client.get("/images/../app.py")
    assert resp.status_code in (404, 403)

    resp = client.get("/images/../../etc/passwd")
    assert resp.status_code in (404, 403)


def test_images_route_serves_valid_image(monkeypatch, tmp_path):
    """The /images/ endpoint should serve valid images from DATA_FOLDER."""
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")

    resp = client.get("/images/sample.png")
    assert resp.status_code == 200
    assert resp.content_type.startswith("image/")


def test_thumb_route_rejects_path_traversal(monkeypatch, tmp_path):
    """The /thumb/ endpoint should not serve files outside DATA_FOLDER."""
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")

    # Create a valid image for thumbnail testing
    from PIL import Image
    img = Image.new("RGB", (64, 64), color="blue")
    img.save(data_dir / "safe.jpg")

    resp = client.get("/thumb/../app.py")
    assert resp.status_code in (404, 403)

    # Valid thumbnail request should work
    resp = client.get("/thumb/safe.jpg")
    assert resp.status_code == 200
    assert resp.content_type.startswith("image/")


# ---------------------------------------------------------------------------
# 5. CF-Connecting-IP fallback (now also ignores untrusted headers)
# ---------------------------------------------------------------------------

def test_cf_connecting_ip_ignored_from_untrusted_source(monkeypatch, tmp_path):
    """CF-Connecting-IP is ignored when the source is not a trusted proxy.

    After the trusted-proxy fix, _client_ip() only honours CF-Connecting-IP
    when the request comes from a configured trusted proxy.  From untrusted
    sources (like our test client), it falls back to remote_addr.
    """
    client, _ = build_client(monkeypatch, tmp_path, auth_type="none")
    _seed_csrf(client)

    # Send 5 requests with different CF-Connecting-IP values — they should
    # all share the same rate-limit key (remote_addr=127.0.0.1).
    for i in range(5):
        resp = _auth_post(client, headers={"CF-Connecting-IP": f"172.16.{i}.1"})
        assert resp.status_code == 302

    # 6th request with a new CF-Connecting-IP should be rate-limited —
    # proving the header is ignored from untrusted sources.
    resp = _auth_post(client, headers={"CF-Connecting-IP": "172.16.99.1"})
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# 6. RATE_LIMIT_ROUTE_LIMITS JSON parsing (_load_route_overrides)
# ---------------------------------------------------------------------------

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
    from security import RateLimitConfig, _load_route_overrides

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
    from security import RateLimitConfig, _load_route_overrides

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


def test_get_route_limit_overrides_lazy(monkeypatch):
    """get_route_limit_overrides loads lazily on first call."""
    import security

    monkeypatch.setenv("RATE_LIMIT_ROUTE_LIMITS", json.dumps({"test": {"max_requests": 1, "window": 10}}))

    # Force a fresh load by resetting the module-level cache
    security._route_limit_overrides = None

    overrides = security.get_route_limit_overrides()
    assert "test" in overrides
    assert overrides["test"].max_requests == 1


def test_refresh_route_overrides_picks_up_env_change(monkeypatch):
    """refresh_route_overrides reloads from the current environment."""
    import security

    monkeypatch.setenv("RATE_LIMIT_ROUTE_LIMITS", json.dumps({"initial": {"max_requests": 5, "window": 60}}))
    security._route_limit_overrides = None

    overrides1 = security.get_route_limit_overrides()
    assert "initial" in overrides1

    # Change the environment variable and refresh
    monkeypatch.setenv("RATE_LIMIT_ROUTE_LIMITS", json.dumps({"updated": {"max_requests": 10, "window": 120}}))
    overrides2 = security.refresh_route_overrides()
    assert "updated" in overrides2
    assert "initial" not in overrides2


def test_get_primary_limiter_lazy(monkeypatch):
    """get_primary_limiter builds lazily on first call."""
    import security

    # Force a fresh build by resetting the module-level cache
    security._primary_limiter = None

    limiter = security.get_primary_limiter()
    assert limiter is not None
    assert hasattr(limiter, "allow")


def test_refresh_primary_limiter_rebuilds(monkeypatch):
    """refresh_primary_limiter rebuilds from the current environment."""
    import security

    # Force a fresh build by resetting the module-level cache
    security._primary_limiter = None

    limiter1 = security.get_primary_limiter()
    assert limiter1 is not None

    limiter2 = security.refresh_primary_limiter()
    assert limiter2 is not None
