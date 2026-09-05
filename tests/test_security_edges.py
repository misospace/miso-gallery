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

from conftest import TEST_SECRET, auth_header, build_client


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

def test_webhook_disabled_returns_403(monkeypatch, tmp_path):
    """When WEBHOOK_ENABLED=false, webhook endpoint should return 403."""
    client, _ = build_client(
        monkeypatch, tmp_path,
        auth_type="none",
        extra_env={"WEBHOOK_ENABLED": "false"},
    )

    resp = client.post("/api/webhook/run", json={"task": "generate"})
    assert resp.status_code == 403


def test_webhook_enabled_accepts_requests(monkeypatch, tmp_path):
    """When WEBHOOK_ENABLED=true with WEBHOOK_SECRET, endpoint accepts valid requests."""
    client, _ = build_client(
        monkeypatch, tmp_path,
        auth_type="none",
        extra_env={
            "WEBHOOK_ENABLED": "true",
            "WEBHOOK_SECRET": "test-secret-123",
            "WEBHOOK_TASK_GENERATE": "echo {params.name}",
        },
    )

    resp = client.post(
        "/api/webhook/run",
        json={"task": "generate", "params": {"name": "test"}},
        headers={"Authorization": "Bearer test-secret-123"},
    )
    assert resp.status_code == 200


def test_webhook_no_task_rejected(monkeypatch, tmp_path):
    """Webhook POST without a 'task' field should be rejected."""
    client, _ = build_client(
        monkeypatch, tmp_path,
        auth_type="none",
        extra_env={
            "WEBHOOK_ENABLED": "true",
            "WEBHOOK_SECRET": "test-secret-123",
        },
    )

    resp = client.post(
        "/api/webhook/run",
        json={},
        headers={"Authorization": "Bearer test-secret-123"},
    )
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


def test_thumb_route_rejects_symlink_outside_data_folder(monkeypatch, tmp_path):
    """#442: /thumb/ must not follow symlinks that point outside DATA_FOLDER."""
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")

    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "hostname"
    target.write_bytes(b"test-host")
    (data_dir / "bar.jpg").symlink_to(target)

    resp = client.get("/thumb/bar.jpg")
    assert resp.status_code == 404
    assert b"test-host" not in resp.data


def test_llm_image_rejects_symlink_outside_data_folder(monkeypatch, tmp_path):
    """#447: /api/llm/image/ must not follow symlinks that point outside DATA_FOLDER.

    A read-scoped API key holder who can drop a file under DATA_FOLDER must not
    be able to obtain media_metadata() for an arbitrary filesystem target.
    """
    client, data_dir = build_client(monkeypatch, tmp_path)

    (data_dir / "foo.png").symlink_to("/etc/passwd")

    resp = client.get("/api/llm/image/foo.png", headers=auth_header())
    assert resp.status_code == 404
    body = resp.get_json()
    assert body is None or "size" not in body


def test_llm_image_serves_symlink_inside_data_folder(monkeypatch, tmp_path):
    """#447: a symlink that resolves back inside DATA_FOLDER is still served."""
    client, data_dir = build_client(monkeypatch, tmp_path)

    (data_dir / "foo.png").symlink_to(data_dir / "cats" / "cat.jpg")

    resp = client.get("/api/llm/image/foo.png", headers=auth_header())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["name"] == "foo.png"
    assert "size" in body


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

    # The single-worker dev opt-in lets us exercise the fallback path without
    # standing up a Redis instance. The fail-fast guard is covered by
    # tests/test_security_redis_required.py.
    monkeypatch.setenv("ALLOW_INMEMORY_RATE_LIMIT", "1")
    monkeypatch.setenv("WEB_CONCURRENCY", "1")

    limiter = security.get_primary_limiter()
    assert limiter is not None
    assert hasattr(limiter, "allow")


def test_refresh_primary_limiter_rebuilds(monkeypatch):
    """refresh_primary_limiter rebuilds from the current environment."""
    import security

    # Force a fresh build by resetting the module-level cache
    security._primary_limiter = None

    monkeypatch.setenv("ALLOW_INMEMORY_RATE_LIMIT", "1")
    monkeypatch.setenv("WEB_CONCURRENCY", "1")

    limiter1 = security.get_primary_limiter()
    assert limiter1 is not None

    limiter2 = security.refresh_primary_limiter()
    assert limiter2 is not None


def test_audit_log_ignores_forged_xff_from_untrusted_source(monkeypatch):
    """The audit-log ``_client_ip`` must ignore forged ``X-Forwarded-For``.

    Issue #404: app.py used to define its own ``_client_ip`` that trusted
    ``X-Forwarded-For`` unconditionally, which made every
    ``log_security_event()`` ``remote_addr`` field attacker-controlled. The
    audit-log path must resolve the client IP with the same trust rules
    as ``security._client_ip`` (gated by ``TRUSTED_PROXIES`` /
    ``TRUST_PROXY``).
    """
    import app as app_module
    import security

    # No TRUSTED_PROXIES / TRUST_PROXY is configured: forged XFF must be ignored.
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    monkeypatch.delenv("TRUST_PROXY", raising=False)

    app = app_module.app
    forged = "198.51.100.42"

    # The audit-log path resolves through the same hardened helper.
    assert app_module._client_ip is security._client_ip, (
        "app.py must reuse security._client_ip so audit logs honour the "
        "trusted-proxy trust model (see issue #404)."
    )

    with app.test_request_context(
        "/delete/foo.png",
        method="POST",
        headers={"X-Forwarded-For": forged},
        environ_overrides={"REMOTE_ADDR": "203.0.113.7"},
    ):
        resolved = app_module._client_ip()
        assert resolved == "203.0.113.7"
        assert resolved != forged


def test_audit_log_honours_xff_when_proxy_trusted(monkeypatch):
    """With TRUSTED_PROXIES configured, XFF is honoured from that source."""
    import app as app_module

    # Mark the immediate peer (203.0.113.7) as a trusted reverse proxy so
    # the hardened helper will follow the X-Forwarded-For chain.
    monkeypatch.setenv("TRUSTED_PROXIES", "203.0.113.7")
    monkeypatch.delenv("TRUST_PROXY", raising=False)

    app = app_module.app
    forged = "198.51.100.42"

    with app.test_request_context(
        "/delete/foo.png",
        method="POST",
        headers={"X-Forwarded-For": forged},
        environ_overrides={"REMOTE_ADDR": "203.0.113.7"},
    ):
        resolved = app_module._client_ip()
        assert resolved == forged


# ---------------------------------------------------------------------------
# is_safe_redirect_url — backslash / dot-prefixed open-redirect bypasses
# (issue #452)
# ---------------------------------------------------------------------------




class TestIsSafeRedirectUrl:
    """Unit tests for security.is_safe_redirect_url covering the
    backslash parser-differential and dot-prefixed bypass vectors (issue #452)."""

    def _is_safe(self, url):
        from security import is_safe_redirect_url
        return is_safe_redirect_url(url)

    # --- backslash-prefixed payloads (the core of issue #452) ---

    def test_single_backslash_external(self):
        assert self._is_safe(r"\evil.com/phish") is False

    def test_double_backslash_external(self):
        assert self._is_safe("\\\\evil.com") is False

    def test_backslash_with_path(self):
        assert self._is_safe("\\\\evil.com/phish") is False

    def test_backslash_with_port(self):
        assert self._is_safe(r"\evil.com:8080/x") is False

    def test_backslash_mid_url(self):
        # Mid-URL backslashes normalize to "/" giving a safe relative path.
        assert self._is_safe(r"/path\to\evil.com") is True

    # --- dot-prefixed payloads ---

    def test_dot_external(self):
        assert self._is_safe(".evil.com") is False

    def test_dot_slash_relative_allowed(self):
        # "./evil.com" is a legitimate relative path — must be accepted.
        assert self._is_safe("./evil.com") is True

    def test_dotdot_slash_allowed(self):
        assert self._is_safe("../foo") is True

    # --- combination payloads ---

    def test_backslash_dot_external(self):
        assert self._is_safe(r"\.evil.com") is False

    def test_double_backslash_dot_external(self):
        assert self._is_safe("\\\\.evil.com") is False

    # --- already-blocked vectors must stay blocked ---

    def test_protocol_relative(self):
        assert self._is_safe("//evil.com") is False

    def test_http_scheme(self):
        assert self._is_safe("http://evil.com") is False

    def test_https_scheme(self):
        assert self._is_safe("https://evil.com") is False

    def test_javascript_scheme(self):
        assert self._is_safe("javascript:alert(1)") is False

    def test_data_scheme(self):
        assert self._is_safe("data:text/html,<script>alert(1)</script>") is False

    # --- legitimate relative paths must stay accepted ---

    def test_relative_path(self):
        assert self._is_safe("/path") is True

    def test_relative_dashboard(self):
        assert self._is_safe("/dashboard") is True

    def test_relative_with_query(self):
        assert self._is_safe("/search?q=cat") is True

    def test_empty_string(self):
        assert self._is_safe("") is True

    def test_none(self):
        assert self._is_safe(None) is True


class TestAuthNextRedirect:
    """Integration tests: POST /auth with malicious next= must not 302 to
    an attacker-controlled Location (issue #452)."""

    def _post_auth(self, monkeypatch, tmp_path, next_value):
        client, _ = build_client(monkeypatch, tmp_path, auth_type="local")
        _seed_csrf(client)
        return _auth_post(
            client,
            data={"username": "admin", "password": "pass123", "next": next_value},
        )

    def test_auth_next_single_backslash_falls_back(self, monkeypatch, tmp_path):
        resp = self._post_auth(monkeypatch, tmp_path, r"\evil.com/phish")
        assert resp.status_code in (200, 302)
        if resp.status_code == 302:
            location = resp.headers.get("Location", "")
            assert "evil.com" not in location, (
                f"302 Location leaked attacker domain: {location!r}"
            )

    def test_auth_next_double_backslash_falls_back(self, monkeypatch, tmp_path):
        resp = self._post_auth(monkeypatch, tmp_path, "\\\\evil.com")
        assert resp.status_code in (200, 302)
        if resp.status_code == 302:
            location = resp.headers.get("Location", "")
            assert "evil.com" not in location, (
                f"302 Location leaked attacker domain: {location!r}"
            )

    def test_auth_next_dot_external_falls_back(self, monkeypatch, tmp_path):
        resp = self._post_auth(monkeypatch, tmp_path, ".evil.com")
        assert resp.status_code in (200, 302)
        if resp.status_code == 302:
            location = resp.headers.get("Location", "")
            assert "evil.com" not in location, (
                f"302 Location leaked attacker domain: {location!r}"
            )

    def test_auth_next_legit_relative_still_works(self, monkeypatch, tmp_path):
        resp = self._post_auth(monkeypatch, tmp_path, "/dashboard")
        assert resp.status_code == 302
        assert resp.headers.get("Location", "").endswith("/dashboard")


# ---------------------------------------------------------------------------
# Issue #444: index() folder view must skip hidden segments (dotfiles, dot-
# dirs) and symlinks -- the same exclusions iter_gallery_items() applies.
# Defense in depth: the gallery UI never surfaces operator-shell-created
# artifacts (`.git/HEAD`, `.DS_Store`) nor symlinks that point outside
# DATA_FOLDER.
# ---------------------------------------------------------------------------


def test_index_hides_dotdir_git(tmp_path, monkeypatch):
    """`DATA/.git/HEAD` (and the `.git` directory itself) must not be rendered."""
    client, _data_dir = build_client(monkeypatch, tmp_path, auth_type="none")
    # build_client creates `.thumb_cache/`, `sample.png`, `copy.png`, `cats/`.
    # Add a hidden `.git/` containing HEAD.
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert ".git" not in body, "index() must not render the .git/ folder"
    assert "HEAD" not in body, "index() must not render files inside .git/"


def test_index_hides_dotfile_ds_store(tmp_path, monkeypatch):
    """`DATA/.DS_Store` must not be rendered as a gallery item."""
    client, _data_dir = build_client(monkeypatch, tmp_path, auth_type="none")
    (tmp_path / ".DS_Store").write_bytes(b"\x00\x01\x02bogus")
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert ".DS_Store" not in body


def test_index_hides_file_symlink(tmp_path, monkeypatch):
    """A symlinked file under DATA_FOLDER must not be rendered.

    `DATA/foo.png -> /tmp/whatever.png` -- iterdir follows the link and
    reports it as a regular file with a matching extension, but index() must
    skip symlinks to avoid surfacing operator-shell artifacts.
    """
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")
    # Target needs a valid PNG suffix so the *unfixed* index() would render it.
    target = tmp_path.parent / "sibling_target.png"
    # 1x1 transparent PNG so suffix-valid file exists outside DATA_FOLDER.
    target.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4"
        b"\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18"
        b"\xd8\xa2\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    link = data_dir / "foo.png"
    link.symlink_to(target)
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Neither the basename nor the relpath should appear in the listing.
    assert "foo.png" not in body, (
        "index() must not render symlinked files; got a card with name 'foo.png'"
    )
    # And the thumb/view URL for the symlink must not exist (404), even if
    # the underlying file does.
    thumb = client.get("/thumb/foo.png")
    assert thumb.status_code == 404
    target.unlink(missing_ok=True)


def test_index_hides_directory_symlink(tmp_path, monkeypatch):
    """A symlinked directory under DATA_FOLDER must not be rendered as a
    subfolder, and clicking through to it must not enumerate the target.

    `DATA/dir -> /tmp/whatever` would otherwise be navigated as `dir/` and
    iterdir()'d in the target.
    """
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")
    target = tmp_path.parent / "sibling_target_dir"
    target.mkdir()
    (target / "secret.png").write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4"
        b"\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18"
        b"\xd8\xa2\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    link = data_dir / "dir"
    link.symlink_to(target)
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # The symlinked folder must not appear as a folder card. We check the
    # checkbox relpath so a literal "dir" substring elsewhere in the page
    # (e.g. <html dir="ltr">) does not produce a false positive.
    assert 'value="dir/"' not in body, (
        "directory symlink must not appear as a checkbox-able subfolder entry"
    )
    assert 'class="folder-name">dir<' not in body, (
        "index() must not render directory symlinks as subfolders"
    )
