"""Authentication helpers for Miso Gallery."""

from __future__ import annotations

import os
from functools import wraps
from typing import Literal

from flask import redirect, request, session, url_for
from werkzeug.security import check_password_hash

AuthMode = Literal["none", "local", "oidc"]

AUTH_TYPE = os.environ.get("AUTH_TYPE", "local").strip().lower()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()

OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "").strip()
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "").strip()
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "").strip()
OIDC_CALLBACK_URL = os.environ.get("OIDC_CALLBACK_URL", "").strip()


def resolved_auth_mode() -> AuthMode:
    """Resolve effective auth mode based on configured env vars."""
    if AUTH_TYPE == "none":
        return "none"

    if AUTH_TYPE == "oidc":
        if OIDC_ISSUER and OIDC_CLIENT_ID and OIDC_CLIENT_SECRET and OIDC_CALLBACK_URL:
            return "oidc"
        # Invalid OIDC config falls back to none to avoid lockouts
        return "none"

    # default/local
    if ADMIN_PASSWORD:
        return "local"
    return "none"


def is_auth_enabled() -> bool:
    return resolved_auth_mode() != "none"


def is_authenticated() -> bool:
    return bool(session.get("authenticated"))


def verify_local_password(password: str) -> bool:
    """Verify local password.

    Supports plaintext and hashed formats for migration:
    - plaintext: ADMIN_PASSWORD=mysecret
    - hashed: ADMIN_PASSWORD=pbkdf2:sha256:... or scrypt:...
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
