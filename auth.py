"""Authentication helpers for Miso Gallery."""

from __future__ import annotations

import json
import os
import secrets
from functools import wraps
from typing import Literal

from authlib.integrations.flask_client import OAuth
from flask import jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash

AuthMode = Literal["none", "local", "oidc"]

AUTH_TYPE = os.environ.get("AUTH_TYPE", "local").strip().lower()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
LLM_READ_API_KEYS = [key.strip() for key in os.environ.get("LLM_READ_API_KEYS", "").split(",") if key.strip()]
LLM_WRITE_API_KEYS = [key.strip() for key in os.environ.get("LLM_WRITE_API_KEYS", "").split(",") if key.strip()]

# Legacy single var — use LLM_READ_API_KEYS / LLM_WRITE_API_KEYS instead
_LLM_LEGACY_KEYS = [key.strip() for key in os.environ.get("LLM_API_KEYS", "").split(",") if key.strip()]

# OIDC Authorization Configuration
OIDC_ALLOWED_DOMAINS = [
    d.strip().lower() for d in os.environ.get("OIDC_ALLOWED_DOMAINS", "").split(",") if d.strip()
]
OIDC_ALLOWED_GROUPS = [
    g.strip().lower() for g in os.environ.get("OIDC_ALLOWED_GROUPS", "").split(",") if g.strip()
]
OIDC_REQUIRED_CLAIMS: dict[str, str] = {}
_oidc_required_claims_raw = os.environ.get("OIDC_REQUIRED_CLAIMS", "").strip()
if _oidc_required_claims_raw:
    try:
        OIDC_REQUIRED_CLAIMS = json.loads(_oidc_required_claims_raw)
    except (json.JSONDecodeError, ValueError):
        OIDC_REQUIRED_CLAIMS = {}

# OIDC Configuration
OIDC_ENABLED = os.environ.get("OIDC_ENABLED", "").strip().lower() == "true"
OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "").strip()
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "").strip()
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "").strip()
OIDC_CALLBACK_URL = os.environ.get("OIDC_CALLBACK_URL", "").strip()

oauth = OAuth()


def configure_oauth(app):
    """Configure OAuth with OIDC provider if enabled."""
    if not is_oidc_configured():
        return

    # Authentik-compatible defaults
    issuer_url = OIDC_ISSUER.rstrip("/")
    if "/.well-known/openid-configuration" in issuer_url:
        issuer_url = issuer_url.replace("/.well-known/openid-configuration", "")

    oauth.init_app(app)
    oauth.register(
        name="oidc",
        client_id=OIDC_CLIENT_ID,
        client_secret=OIDC_CLIENT_SECRET,
        server_metadata_url=f"{issuer_url}/.well-known/openid-configuration",
        client_kwargs={
            "scope": "openid profile email",
        },
    )


def is_oidc_configured() -> bool:
    """Check if OIDC is properly configured."""
    return bool(OIDC_ENABLED and OIDC_ISSUER and OIDC_CLIENT_ID and OIDC_CLIENT_SECRET)


def resolved_auth_mode() -> AuthMode:
    """Resolve effective auth mode based on configured env vars."""
    if AUTH_TYPE == "none":
        return "none"

    if AUTH_TYPE == "oidc" or is_oidc_configured():
        return "oidc"

    # default/local
    if ADMIN_PASSWORD:
        return "local"
    return "none"


def is_auth_enabled() -> bool:
    return resolved_auth_mode() != "none"


def is_authenticated() -> bool:
    return bool(session.get("authenticated"))


def _any_api_keys() -> bool:
    """True if any API key is configured (read, write, or legacy)."""
    return bool(LLM_READ_API_KEYS or LLM_WRITE_API_KEYS or _LLM_LEGACY_KEYS)


def is_api_key_auth_enabled() -> bool:
    return _any_api_keys()


def _bearer_token() -> str:
    auth_header = request.headers.get("Authorization", "")
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return ""
    return token.strip()


def _keys_for_scope(scope: Literal["read", "write"]) -> list[str]:
    if scope == "write":
        # Prefer explicit write keys; fall back to legacy for backward compat
        if LLM_WRITE_API_KEYS:
            return LLM_WRITE_API_KEYS
        return _LLM_LEGACY_KEYS
    # Read scope: explicit read keys, write keys (write implies read), then legacy
    keys = list(LLM_READ_API_KEYS)
    keys.extend(LLM_WRITE_API_KEYS)
    if not keys:
        keys = list(_LLM_LEGACY_KEYS)
    return keys


def verify_api_key_scope(token: str, scope: Literal["read", "write"]) -> bool:
    """Verify a bearer token has the required scope."""
    keys = _keys_for_scope(scope)
    if not token or not keys:
        return False
    return any(secrets.compare_digest(token, configured) for configured in keys)


# API key identification helpers for audit logging.

_API_KEY_HINT_CHARS = 4  # show first/last N chars as a stable hint


def _api_key_hint(key: str) -> str:
    """Return a short stable hint derived from an API key for audit logging.

    Shows first 4 and last 4 characters (or less if the key is shorter).
    Does NOT reveal the full key.
    """
    if not key or len(key) < 8:
        return key[:2] + "..." if key else ""
    return f"{key[:_API_KEY_HINT_CHARS]}...{key[-_API_KEY_HINT_CHARS:]}"


def _find_matching_key(
    token: str, scope: Literal["read", "write"]
) -> tuple[bool, str | None]:
    """Verify a bearer token and return (success, key_hint_or_none).

    Uses constant-time comparison to prevent timing attacks.
    Returns the hint for the first matching key found.
    """
    keys = _keys_for_scope(scope)
    if not token or not keys:
        return False, None
    for configured in keys:
        if secrets.compare_digest(token, configured):
            return True, _api_key_hint(configured)
    return False, None


# Backward-compat: verify_api_key checks read scope
def verify_api_key(token: str) -> bool:
    return verify_api_key_scope(token, "read")


def verify_local_password(password: str) -> bool:
    """Verify local password.

    Supports plaintext and hashed formats for migration:
    - plaintext: ADMIN_PASSWORD=***
    - hashed: ADMIN_PASSWORD=*** or scrypt:...
    """
    if not ADMIN_PASSWORD:
        return False

    stored = ADMIN_PASSWORD
    if stored.startswith(("pbkdf2:", "scrypt:")):
        return check_password_hash(stored, password)
    return password == stored


def require_auth(view_fn):
    @wraps(view_fn)
    def wrapper(*args, **kwargs):
        if not is_auth_enabled():
            return view_fn(*args, **kwargs)
        if is_authenticated():
            return view_fn(*args, **kwargs)
        return redirect(url_for("login", next=request.path))

    return wrapper


def require_api_key(view_fn):
    """Backward-compatible decorator — checks read scope, still allows browser sessions."""
    @wraps(view_fn)
    def wrapper(*args, **kwargs):
        if verify_api_key_scope(_bearer_token(), "read"):
            return view_fn(*args, **kwargs)
        if is_authenticated():
            return view_fn(*args, **kwargs)
        if not _any_api_keys():
            return jsonify({"error": "LLM API keys are not configured"}), 403
        return jsonify({"error": "Bearer token required"}), 401

    return wrapper


def require_api_key_with_scope(scope: Literal["read", "write"]):
    """Decorator for /api/llm/* endpoints. Rejects browser sessions — machine-only."""
    def decorator(view_fn):
        @wraps(view_fn)
        def wrapper(*args, **kwargs):
            # Verify API key and store hint in session for audit logging
            matched, key_hint = _find_matching_key(_bearer_token(), scope)
            if matched:
                session["api_key_hint"] = key_hint
                return view_fn(*args, **kwargs)
            if is_authenticated():
                return jsonify({"error": "Browser sessions are not accepted on LLM API endpoints"}), 403
            if not _any_api_keys():
                return jsonify({"error": "LLM API keys are not configured"}), 403
            if scope == "write" and not LLM_WRITE_API_KEYS and not _LLM_LEGACY_KEYS:
                return jsonify({"error": "Write API keys are not configured"}), 403
            return jsonify({"error": "Bearer token required"}), 401

        return wrapper
    return decorator


def verify_oidc_authorization(user_info: dict) -> tuple[bool, str | None]:
    """Check OIDC user info against authorization allowlists/claims.

    Returns (allowed, reason). If allowed is False, reason explains why.
    All checks are optional — if no authorization config is set, all users pass.

    Checks performed (all must pass):
      1. Domain allowlist: if OIDC_ALLOWED_DOMAINS is set, email domain must match
      2. Group allowlist: if OIDC_ALLOWED_GROUPS is set, user groups must intersect
      3. Required claims: if OIDC_REQUIRED_CLAIMS is set, all key=value pairs must match
    """
    # Check domain allowlist
    if OIDC_ALLOWED_DOMAINS:
        email = (user_info.get("email") or "").lower()
        if not email:
            return False, "OIDC authorization failed: no email in user info"
        user_domain = email.rsplit("@", 1)[-1] if "@" in email else ""
        if user_domain not in OIDC_ALLOWED_DOMAINS:
            return (
                False,
                f"OIDC authorization failed: domain '{user_domain}' not in allowed domains",
            )

    # Check group allowlist
    if OIDC_ALLOWED_GROUPS:
        groups = [g.lower() for g in user_info.get("groups", [])]
        if not any(g in OIDC_ALLOWED_GROUPS for g in groups):
            return (
                False,
                f"OIDC authorization failed: no matching group in {OIDC_ALLOWED_GROUPS}",
            )

    # Check required claims
    if OIDC_REQUIRED_CLAIMS:
        for claim_key, expected_value in OIDC_REQUIRED_CLAIMS.items():
            actual = str(user_info.get(claim_key, ""))
            if actual != expected_value:
                return (
                    False,
                    f"OIDC authorization failed: claim '{claim_key}' mismatch "
                    f"(expected '{expected_value}', got '{actual}')",
                )

    return True, None


def get_oidc_label() -> str:
    """Get a display name for the OIDC provider."""
    label = os.environ.get("OIDC_ISSUER_LABEL", "").strip()
    if label:
        return label
    provider_name = os.environ.get("OIDC_PROVIDER_NAME", "").strip()
    if provider_name:
        return provider_name
    if OIDC_ISSUER:
        # Extract domain from issuer URL
        return OIDC_ISSUER.replace("https://", "").replace("http://", "").split("/")[0]
    return "OIDC"
