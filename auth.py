"""
Authentication module for Miso Gallery

Supports multiple auth methods:
1. No auth (default)
2. Local password auth
3. OIDC via Authentik or other providers

Environment Variables:
---------------------
# Auth method selection
AUTH_TYPE=local|oidc|none  (default: none)

# Local auth
ADMIN_PASSWORD=your-password

# OIDC auth (if AUTH_TYPE=oidc)
OIDC_ISSUER=https://authentik.yourdomain.com
OIDC_CLIENT_ID=miso-gallery
OIDC_CLIENT_SECRET=your-client-secret
OIDC_CALLBACK_URL=https://miso-gallery.yourdomain.com/auth/callback

# Flask secret key (required for sessions)
SECRET_KEY=random-string
"""

import os
from functools import wraps
from flask import session, redirect, url_for, request, flash

# Config
AUTH_TYPE = os.environ.get('AUTH_TYPE', 'none').lower()
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')
SECRET_KEY = os.environ.get('SECRET_KEY', os.urandom(32))

# OIDC Config
OIDC_ISSUER = os.environ.get('OIDC_ISSUER', '')
OIDC_CLIENT_ID = os.environ.get('OIDC_CLIENT_ID', '')
OIDC_CLIENT_SECRET = os.environ.get('OIDC_CLIENT_SECRET', '')
OIDC_CALLBACK_URL = os.environ.get('OIDC_CALLBACK_URL', '')

def is_auth_enabled():
    """Check if any auth is enabled"""
    return AUTH_TYPE in ('local', 'oidc') or bool(ADMIN_PASSWORD)

def require_auth(f):
    """Decorator to require authentication"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_auth_enabled():
            return f(*args, **kwargs)
        if session.get('authenticated'):
            return f(*args, **kwargs)
        return redirect(url_for('login'))
    return decorated
