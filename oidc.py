"""OIDC login / callback / refresh flow (extracted from app.py, issue #520).

The three view functions keep their original endpoint names (``oidc_login``,
``oidc_callback``, ``oidc_refresh``) and rate limits. They are registered onto
the Flask app by :func:`register_oidc_routes`, which ``app.py`` calls with its
``log_security_event`` audit logger so this module stays free of app wiring
and importing ``app`` here never becomes circular.
"""

from __future__ import annotations

import os
import time

from flask import jsonify, redirect, request, session, url_for

from auth import is_oidc_configured, oauth, verify_oidc_authorization
from security import is_safe_redirect_url, rate_limit, validate_csrf


def resolve_oidc_token_url() -> str:
    """Resolve the OIDC token endpoint used by /auth/oidc/refresh.

    Resolution order (Issue #450):
    1. ``token_endpoint`` from the OIDC discovery document (Authlib caches it
       in ``oauth.oidc.server_metadata`` once fetched) — the standard path,
       which works for any conformant provider (Keycloak, Auth0, Okta, ...).
    2. ``OIDC_TOKEN_URL`` env var — explicit operator override for providers
       whose discovery document is unreachable or incomplete.
    3. Authentik-style ``<OIDC_ISSUER>/protocol/openid-connect/token`` —
       last-resort fallback when both are missing.
    """
    try:
        metadata = oauth.oidc.server_metadata
    except Exception:
        metadata = None
    if isinstance(metadata, dict):
        token_endpoint = metadata.get("token_endpoint")
        if token_endpoint:
            return token_endpoint
    override = os.environ.get("OIDC_TOKEN_URL", "").strip()
    if override:
        return override
    return os.environ.get("OIDC_ISSUER", "").rstrip("/") + "/protocol/openid-connect/token"


def register_oidc_routes(app, log_security_event) -> None:
    """Register the OIDC auth routes on ``app``.

    ``log_security_event`` is the app's structured audit logger; it is passed
    in (rather than imported from ``app``) to keep this module importable
    without the Flask app module.
    """

    @app.route("/auth/oidc")
    @rate_limit(max_requests=20, window=60)
    def oidc_login():
        """Initiate OIDC authentication flow."""
        if not is_oidc_configured():
            return redirect(url_for("login"))

        # Store the return URL in session
        raw_next = request.args.get("next") or url_for("index")
        next_url = raw_next if is_safe_redirect_url(raw_next) else url_for("index")
        session["oidc_next_url"] = next_url

        # Get the callback URL
        callback_url = os.environ.get("OIDC_CALLBACK_URL") or url_for("oidc_callback", _external=True)

        # Redirect to OIDC provider
        return oauth.oidc.authorize_redirect(callback_url)

    @app.route("/auth/oidc/callback")
    @rate_limit(max_requests=20, window=60)
    def oidc_callback():
        """Handle OIDC callback."""
        if not is_oidc_configured():
            return redirect(url_for("login"))

        try:
            # Get the access token
            token = oauth.oidc.authorize_access_token()

            # Get user info from the token or userinfo endpoint
            user_info = token.get("userinfo")
            if not user_info:
                # Fetch user info from userinfo endpoint
                resp = oauth.oidc.get("userinfo")
                user_info = resp.json()

            # Extract stable subject ID (always log this for audit trails)
            oidc_sub = user_info.get("sub")

            # Extract user identifier (prefer email, fallback to sub)
            user_id = user_info.get("email") or user_info.get("sub")
            user_name = user_info.get("name") or user_info.get("preferred_username") or user_id

            if not user_id:
                return "Could not identify user from OIDC response", 400

            # Check OIDC authorization (domain/group/claim allowlists)
            allowed, reason = verify_oidc_authorization(user_info)
            if not allowed:
                log_security_event(
                    "login", "denied", auth_method="oidc", reason=reason,
                    oidc_sub=oidc_sub,
                )
                next_url = session.pop("oidc_next_url", None) or "/"
                return redirect(url_for("login", error="oidc_denied", next=next_url))

            # Set session as permanent and authenticated
            session.permanent = True
            session["authenticated"] = True
            session["user_id"] = user_id
            session["user_name"] = user_name
            session["auth_method"] = "oidc"

            # Store refresh token and expiry for future token refresh
            if token.get("refresh_token"):
                session["oidc_refresh_token"] = token["refresh_token"]
            if token.get("expires_in"):
                session["oidc_token_expires_at"] = time.time() + int(token["expires_in"])

            log_security_event("login", "success", auth_method="oidc", oidc_sub=oidc_sub)

            # Redirect to the stored next URL or index
            next_url = session.pop("oidc_next_url", None) or url_for("index")
            return redirect(next_url)

        except Exception as e:
            log_security_event("login", "error", auth_method="oidc", error=type(e).__name__)
            next_url = session.pop("oidc_next_url", None) or "/"
            return redirect(url_for("login", error="oidc_failed", next=next_url))

    @app.route("/auth/oidc/refresh", methods=["POST"])
    @rate_limit(max_requests=5, window=300)
    def oidc_refresh():
        """Refresh OIDC access token using stored refresh token."""
        # This endpoint mutates session state (rotates the OIDC refresh token and
        # expiry), so it is POST-only and requires a CSRF token: a cross-site GET
        # (e.g. an <img src> or <a href> on a hostile page) must not be able to
        # trigger a token round-trip or rotate the victim's refresh token
        # (Issue #454).
        if not validate_csrf(request.form.get("csrf_token")):
            log_security_event(
                "token_refresh", "denied",
                auth_method="oidc",
                reason="invalid_csrf",
                user_id=session.get("user_id"),
            )
            return jsonify({"error": "Invalid CSRF token"}), 403

        if not is_oidc_configured():
            return jsonify({"error": "OIDC not configured"}), 400

        if not session.get("authenticated") or session.get("auth_method") != "oidc":
            return jsonify({"error": "not authenticated via OIDC"}), 401

        refresh_token = session.get("oidc_refresh_token")
        if not refresh_token:
            log_security_event(
                "token_refresh", "denied",
                auth_method="oidc",
                reason="no_refresh_token",
                user_id=session.get("user_id"),
            )
            return jsonify({"error": "no refresh token available"}), 401

        try:
            from authlib.integrations.requests_client import OAuth2Session

            client_id = os.environ.get("OIDC_CLIENT_ID", "")
            client_secret = os.environ.get("OIDC_CLIENT_SECRET", "")
            token_url = resolve_oidc_token_url()

            oauth_session = OAuth2Session(client_id, client_secret, token={"refresh_token": refresh_token})
            new_token = oauth_session.refresh_token(token_url)

            if new_token.get("refresh_token"):
                session["oidc_refresh_token"] = new_token["refresh_token"]
            if new_token.get("expires_in"):
                session["oidc_token_expires_at"] = time.time() + int(new_token["expires_in"])

            user_info = oauth.oidc.userinfo(token={"access_token": new_token["access_token"]})
            allowed, reason = verify_oidc_authorization(user_info)
            if not allowed:
                log_security_event(
                    "token_refresh", "denied",
                    auth_method="oidc",
                    reason=reason,
                    user_id=session.get("user_id"),
                )
                # Invalidate session on authorization failure
                session.clear()
                return jsonify({"error": "authorization revoked after refresh"}), 403

            log_security_event(
                "token_refresh", "success",
                auth_method="oidc",
                user_id=session.get("user_id"),
            )
            return jsonify({"status": "refreshed"})

        except Exception as e:
            log_security_event(
                "token_refresh", "error",
                auth_method="oidc",
                error=type(e).__name__,
                user_id=session.get("user_id"),
            )
            return jsonify({"error": "token refresh failed"}), 500
