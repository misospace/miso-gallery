"""Regression tests for security edges in miso-gallery.

Covers:
- Spoofed X-Forwarded-For header bypassing rate limits (issue #205)
- Public non-media access (no auth leaks on public endpoints)
- Webhook auth-disabled behavior
- Symlink/mount boundary path traversal
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conftest import build_client, TEST_SECRET


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
# 1. Spoofed X-Forwarded-For rate-limit bypass
# ---------------------------------------------------------------------------

def test_xff_rotation_bypasses_rate_limit(monkeypatch, tmp_path):
    """Regression: rotating XFF headers should allow bypassing per-IP rate limits.

    The current _client_ip() implementation in security.py returns the first
    value from X-Forwarded-For without any trusted-proxy boundary.  This means
    a client can rotate XFF values to stay under the per-IP rate-limit cap.

    This test verifies the *current* (vulnerable) behaviour so that a future fix
    can invert this expectation.
    """
    client, _ = build_client(monkeypatch, tmp_path, auth_type="none")
    _seed_csrf(client)

    # The /auth endpoint is rate-limited at 5 requests / 300s.
    # Send 5 requests with different XFF headers — each should succeed because
    # the limiter sees a different IP per request.
    for i in range(5):
        resp = _auth_post(client, headers={"X-Forwarded-For": f"10.0.{i}.1"})
        assert resp.status_code == 302

    # A 6th request with a *new* XFF should also succeed — proving bypass.
    resp = _auth_post(client, headers={"X-Forwarded-For": "10.99.0.1"})
    assert resp.status_code == 302


def test_xff_single_ip_hits_rate_limit(monkeypatch, tmp_path):
    """Same XFF value must be rate-limited consistently."""
    client, _ = build_client(monkeypatch, tmp_path, auth_type="none")
    _seed_csrf(client)

    xff = "192.168.1.100"
    # Send 5 requests (the login rate limit is 5/300s)
    for i in range(5):
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
    for i in range(5):
        resp = _auth_post(client)

    # 6th request without XFF should be rate-limited (same remote_addr)
    resp = _auth_post(client)
    assert resp.status_code == 429


def test_xff_multiple_hops_returns_first(monkeypatch, tmp_path):
    """X-Forwarded-For with multiple comma-separated IPs should use the first."""
    client, _ = build_client(monkeypatch, tmp_path, auth_type="none")
    _seed_csrf(client)

    # Send 5 requests with multi-hop XFF — all share the same first IP
    for i in range(5):
        xff = "10.1.2.3, 10.9.9.9, 10.8.8.8"
        resp = _auth_post(client, headers={"X-Forwarded-For": xff})
        assert resp.status_code == 302

    # 6th request with same first-hop IP should be rate-limited
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
# 5. CF-Connecting-IP fallback
# ---------------------------------------------------------------------------

def test_cf_connecting_ip_used_when_no_xff(monkeypatch, tmp_path):
    """When XFF is absent but CF-Connecting-IP is present, use it as client IP."""
    client, _ = build_client(monkeypatch, tmp_path, auth_type="none")
    _seed_csrf(client)

    for i in range(5):
        resp = _auth_post(client, headers={"CF-Connecting-IP": f"172.16.{i}.1"})
        assert resp.status_code == 302

    # 6th request with a new CF-Connecting-IP should bypass rate limit
    resp = _auth_post(client, headers={"CF-Connecting-IP": "172.16.99.1"})
    assert resp.status_code == 302  # not 429
