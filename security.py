"""Security helpers for Miso Gallery."""

from __future__ import annotations

import secrets
import time
from functools import wraps

from flask import jsonify, request, session

rate_limit_storage: dict[str, list[float]] = {}


def rate_limit(max_requests: int = 30, window: int = 60):
    """Simple in-memory rate limit decorator."""

    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            client_ip = request.headers.get(
                "X-Forwarded-For", request.headers.get("CF-Connecting-IP", request.remote_addr)
            )
            key = f"{client_ip}:{request.endpoint or 'unknown'}"
            now = time.time()

            history = [t for t in rate_limit_storage.get(key, []) if now - t < window]
            if len(history) >= max_requests:
                return jsonify({"error": "Rate limit exceeded"}), 429
            history.append(now)
            rate_limit_storage[key] = history
            return f(*args, **kwargs)

        return decorated

    return decorator


def sanitize_path(path: str) -> bool:
    path = str(path or "").replace("\x00", "")
    if ".." in path or path.startswith("/"):
        return False
    return True


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'",
}


def add_security_headers(response):
    for header, value in SECURITY_HEADERS.items():
        response.headers[header] = value
    return response


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(24)
        session["csrf_token"] = token
    return token


def validate_csrf(submitted: str | None) -> bool:
    token = session.get("csrf_token")
    return bool(token and submitted and submitted == token)
