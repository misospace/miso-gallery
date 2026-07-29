"""Tests for OIDC token refresh functionality (issue #351)."""

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
    """Token refresh returns 400 when no refresh token is stored in session."""
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
    assert resp.status_code == 400
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
