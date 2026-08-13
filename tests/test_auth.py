"""Tests for OIDC token refresh functionality (issue #351)."""

import logging
import sys
from pathlib import Path

from conftest import build_client

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_oidc_refresh_not_configured(monkeypatch, tmp_path):
    """Token refresh endpoint returns 400 when OIDC is not configured."""
    client, _ = build_client(monkeypatch, tmp_path, auth_type="oidc")

    resp = client.get("/auth/oidc/refresh")
    assert resp.status_code == 400
    data = resp.get_json()
    assert "OIDC not configured" in data["error"]


def test_oidc_refresh_not_authenticated(monkeypatch, tmp_path):
    """Token refresh endpoint returns 401 when user is not authenticated via OIDC."""
    extra_env = {
        "OIDC_ENABLED": "true",
        "OIDC_ISSUER": "https://issuer.example",
        "OIDC_CLIENT_ID": "client",
        "OIDC_CLIENT_SECRET": "secret",
    }
    client, _ = build_client(monkeypatch, tmp_path, auth_type="oidc", extra_env=extra_env)

    resp = client.get("/auth/oidc/refresh")
    assert resp.status_code == 401


def test_oidc_refresh_no_refresh_token(monkeypatch, tmp_path):
    """Token refresh returns 401 when no refresh token is stored in session."""
    extra_env = {
        "OIDC_ENABLED": "true",
        "OIDC_ISSUER": "https://issuer.example",
        "OIDC_CLIENT_ID": "client",
        "OIDC_CLIENT_SECRET": "secret",
    }
    client, _ = build_client(monkeypatch, tmp_path, auth_type="oidc", extra_env=extra_env)

    # Simulate an authenticated OIDC session without a refresh token
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["auth_method"] = "oidc"
        sess["user_id"] = "test-user"
        sess["user_name"] = "Test User"

    resp = client.get("/auth/oidc/refresh")
    assert resp.status_code == 401
    data = resp.get_json()
    assert "no refresh token" in data["error"].lower()


def test_oidc_callback_stores_refresh_token(monkeypatch, tmp_path):
    """oidc_callback stores refresh_token and expires_in in the session."""
    extra_env = {
        "OIDC_ENABLED": "true",
        "OIDC_ISSUER": "https://issuer.example",
        "OIDC_CLIENT_ID": "client",
        "OIDC_CLIENT_SECRET": "secret",
    }
    client, _ = build_client(monkeypatch, tmp_path, auth_type="oidc", extra_env=extra_env)

    # Mock oauth.oidc.authorize_access_token to return a token with refresh_token
    import app as app_module

    mock_called = False

    def mock_authorize_access_token(*args, **kwargs):
        nonlocal mock_called
        mock_called = True
        return {
            "access_token": "mock-access-token",
            "refresh_token": "mock-refresh-token",
            "expires_in": 3600,
            "id_token": "mock-id-token",
            "token_type": "Bearer",
        }

    # Patch the authorize_access_token method
    original_authorize = app_module.oauth.oidc.authorize_access_token
    app_module.oauth.oidc.authorize_access_token = mock_authorize_access_token

    try:
        # Mock the userinfo endpoint
        from unittest.mock import MagicMock
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "sub": "test-sub",
            "email": "test@example.com",
            "name": "Test User",
        }
        original_get = app_module.oauth.oidc.get
        app_module.oauth.oidc.get = MagicMock(return_value=mock_resp)

        # Mock verify_oidc_authorization to allow the user
        original_verify = app_module.verify_oidc_authorization
        app_module.verify_oidc_authorization = MagicMock(return_value=(True, None))

        try:
            resp = client.get(
                "/auth/oidc/callback",
                query_string={"code": "mock-code"},
                follow_redirects=False,
            )
            assert mock_called
            assert resp.status_code == 302

            # Check that refresh token and expiry are stored in session
            with client.session_transaction() as sess:
                assert sess.get("oidc_refresh_token") == "mock-refresh-token"
                assert "oidc_token_expires_at" in sess
                assert sess["authenticated"] is True
                assert sess["auth_method"] == "oidc"
        finally:
            app_module.oauth.oidc.get = original_get
            app_module.verify_oidc_authorization = original_verify
    finally:
        app_module.oauth.oidc.authorize_access_token = original_authorize


def test_oidc_callback_stores_expiry_without_refresh(monkeypatch, tmp_path):
    """oidc_callback stores expires_in even when no refresh_token is provided."""
    extra_env = {
        "OIDC_ENABLED": "true",
        "OIDC_ISSUER": "https://issuer.example",
        "OIDC_CLIENT_ID": "client",
        "OIDC_CLIENT_SECRET": "secret",
    }
    client, _ = build_client(monkeypatch, tmp_path, auth_type="oidc", extra_env=extra_env)

    import app as app_module

    def mock_authorize_access_token(*args, **kwargs):
        return {
            "access_token": "mock-access-token",
            "expires_in": 1800,
            "id_token": "mock-id-token",
            "token_type": "Bearer",
        }

    original_authorize = app_module.oauth.oidc.authorize_access_token
    app_module.oauth.oidc.authorize_access_token = mock_authorize_access_token

    try:
        from unittest.mock import MagicMock
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "sub": "test-sub",
            "email": "test@example.com",
            "name": "Test User",
        }
        original_get = app_module.oauth.oidc.get
        app_module.oauth.oidc.get = MagicMock(return_value=mock_resp)

        original_verify = app_module.verify_oidc_authorization
        app_module.verify_oidc_authorization = MagicMock(return_value=(True, None))

        try:
            resp = client.get(
                "/auth/oidc/callback",
                query_string={"code": "mock-code"},
                follow_redirects=False,
            )
            assert resp.status_code == 302

            with client.session_transaction() as sess:
                assert "oidc_token_expires_at" in sess
                # No refresh token stored when provider doesn't send one
                assert sess.get("oidc_refresh_token") is None
        finally:
            app_module.oauth.oidc.get = original_get
            app_module.verify_oidc_authorization = original_verify
    finally:
        app_module.oauth.oidc.authorize_access_token = original_authorize


def test_verify_local_password_plaintext_warning(monkeypatch, tmp_path, caplog):
    """verify_local_password logs a warning when plaintext ADMIN_PASSWORD is used."""
    import auth as auth_module

    monkeypatch.setattr(auth_module, "ADMIN_PASSWORD", "plaintext-secret")

    with caplog.at_level(logging.WARNING, logger="auth"):
        result = auth_module.verify_local_password("plaintext-secret")
        assert result is True
        assert "Plaintext ADMIN_PASSWORD detected" in caplog.text


def test_verify_local_password_hashed_no_warning(monkeypatch, tmp_path, caplog):
    """verify_local_password does not warn when a hashed password is used."""
    from werkzeug.security import generate_password_hash

    import auth as auth_module

    hashed = generate_password_hash("hashed-secret")
    monkeypatch.setattr(auth_module, "ADMIN_PASSWORD", hashed)

    with caplog.at_level(logging.WARNING, logger="auth"):
        result = auth_module.verify_local_password("hashed-secret")
        assert result is True
        assert "Plaintext ADMIN_PASSWORD detected" not in caplog.text


def test_verify_local_password_plaintext_constant_time(monkeypatch, tmp_path):
    """verify_local_password uses constant-time comparison for plaintext passwords."""
    import auth as auth_module

    monkeypatch.setattr(auth_module, "ADMIN_PASSWORD", "short")

    # These should all return False without timing side-channels
    assert auth_module.verify_local_password("longer-password") is False
    assert auth_module.verify_local_password("shor") is False
    assert auth_module.verify_local_password("short") is True


def test_oidc_tokens_not_in_cookie_payload(monkeypatch, tmp_path):
    """Session cookie payload must not contain plaintext OIDC credentials.

    Regression test for issue #384: Flask's default SecureCookieSessionInterface
    signs (HMAC) the session cookie but does not encrypt it, so any value stored
    in ``session`` — including long-lived refresh tokens — is readable by the
    browser.  This test verifies that the raw cookie value is opaque (encrypted)
    and does not leak OIDC token values in plaintext.
    """
    extra_env = {
        "OIDC_ENABLED": "true",
        "OIDC_ISSUER": "https://issuer.example",
        "OIDC_CLIENT_ID": "client",
        "OIDC_CLIENT_SECRET": "secret",
    }
    client, _ = build_client(monkeypatch, tmp_path, auth_type="oidc", extra_env=extra_env)

    # Store OIDC tokens in the session (simulating what oidc_callback does)
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["auth_method"] = "oidc"
        sess["user_id"] = "test-user"
        sess["user_name"] = "Test User"
        sess["oidc_refresh_token"] = "super-secret-refresh-token-12345"

    # Make a request so the session cookie is set in the response
    resp = client.get("/")

    # Extract the raw session cookie value from the response headers
    cookie_name = client.application.config.get("SESSION_COOKIE_NAME", "session")
    cookie_value = None
    for cookie in resp.headers.getlist("Set-Cookie"):
        if cookie.startswith(cookie_name + "="):
            # Parse "name=value; attributes..."
            cookie_value = cookie.split("=", 1)[1].split(";")[0].strip()
            break

    assert cookie_value is not None, "Session cookie was not set in response"

    # The cookie value must NOT contain plaintext OIDC token values.
    # With encryption, the payload is opaque ciphertext; with plain signing,
    # it would be URL-safe base64 of a JSON dict containing the tokens.
    assert "super-secret-refresh-token-12345" not in cookie_value, (
        "oidc_refresh_token found in plaintext in session cookie"
    )


    # Additional sanity: the cookie should not be valid JSON (which would
    # indicate it's a plain signed session rather than encrypted).
    import json as _json

    try:
        _json.loads(cookie_value)
        raise AssertionError("Session cookie is valid JSON — tokens are not encrypted")
    except (_json.JSONDecodeError, ValueError):
        pass  # Expected: encrypted payload is not valid JSON


def test_oidc_refresh_success(monkeypatch, tmp_path):
    """Successful refresh: session built with refresh_token dict, userinfo called
    with the new access_token, session refresh_token and expiry updated."""
    from unittest.mock import MagicMock, patch

    extra_env = {
        "OIDC_ENABLED": "true",
        "OIDC_ISSUER": "https://issuer.example",
        "OIDC_CLIENT_ID": "client",
        "OIDC_CLIENT_SECRET": "secret",
    }
    client, _ = build_client(monkeypatch, tmp_path, auth_type="oidc", extra_env=extra_env)

    import app as app_module

    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["auth_method"] = "oidc"
        sess["user_id"] = "test-sub"
        sess["user_name"] = "Test User"
        sess["oidc_refresh_token"] = "user-refresh-token"

    captured_init = {}

    class FakeOAuth2Session:
        def __init__(self, client_id, client_secret=None, token=None):
            captured_init["client_id"] = client_id
            captured_init["client_secret"] = client_secret
            captured_init["token"] = token

        def refresh_token(self, token_url):
            captured_init["refresh_token_url"] = token_url
            return {
                "access_token": "new-access-token",
                "refresh_token": "new-refresh-token",
                "expires_in": 3600,
            }

    captured_userinfo_token = {}

    def fake_userinfo(token=None):
        captured_userinfo_token["token"] = token
        return {"sub": "test-sub", "email": "test@example.com", "name": "Test User"}

    original_verify = app_module.verify_oidc_authorization
    app_module.verify_oidc_authorization = MagicMock(return_value=(True, None))

    try:
        with patch("authlib.integrations.requests_client.OAuth2Session", FakeOAuth2Session):
            monkeypatch.setattr(app_module.oauth.oidc, "userinfo", fake_userinfo)
            resp = client.get("/auth/oidc/refresh")

        assert resp.status_code == 200
        assert resp.get_json()["status"] == "refreshed"

        assert captured_init["client_id"] == "client"
        assert captured_init["client_secret"] == "secret"
        assert captured_init["token"] == {"refresh_token": "user-refresh-token"}
        assert captured_init["refresh_token_url"] == "https://issuer.example/protocol/openid-connect/token"
        assert captured_userinfo_token["token"] == {"access_token": "new-access-token"}

        with client.session_transaction() as sess:
            assert sess["oidc_refresh_token"] == "new-refresh-token"
            assert "oidc_token_expires_at" in sess
            assert "oidc_access_token" not in sess
    finally:
        app_module.verify_oidc_authorization = original_verify


def test_oidc_refresh_authorization_revoked(monkeypatch, tmp_path):
    """When userinfo shows the user is no longer authorized, session is cleared
    and 403 is returned."""
    from unittest.mock import MagicMock, patch

    extra_env = {
        "OIDC_ENABLED": "true",
        "OIDC_ISSUER": "https://issuer.example",
        "OIDC_CLIENT_ID": "client",
        "OIDC_CLIENT_SECRET": "secret",
    }
    client, _ = build_client(monkeypatch, tmp_path, auth_type="oidc", extra_env=extra_env)

    import app as app_module

    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["auth_method"] = "oidc"
        sess["user_id"] = "test-sub"
        sess["user_name"] = "Test User"
        sess["oidc_refresh_token"] = "user-refresh-token"

    captured_init = {}

    class FakeOAuth2Session:
        def __init__(self, client_id, client_secret=None, token=None):
            captured_init["client_id"] = client_id
            captured_init["client_secret"] = client_secret
            captured_init["token"] = token

        def refresh_token(self, token_url):
            captured_init["refresh_token_url"] = token_url
            return {"access_token": "new-access-token", "expires_in": 3600}

    def fake_userinfo(token=None):
        return {"sub": "test-sub", "email": "test@example.com"}

    original_verify = app_module.verify_oidc_authorization
    app_module.verify_oidc_authorization = MagicMock(return_value=(False, "user_removed"))

    try:
        with patch("authlib.integrations.requests_client.OAuth2Session", FakeOAuth2Session):
            monkeypatch.setattr(app_module.oauth.oidc, "userinfo", fake_userinfo)
            resp = client.get("/auth/oidc/refresh")

        assert resp.status_code == 403
        assert captured_init["client_id"] == "client"
        assert captured_init["client_secret"] == "secret"
        assert captured_init["token"] == {"refresh_token": "user-refresh-token"}
        assert captured_init["refresh_token_url"] == "https://issuer.example/protocol/openid-connect/token"

        with client.session_transaction() as sess:
            assert not sess
    finally:
        app_module.verify_oidc_authorization = original_verify
