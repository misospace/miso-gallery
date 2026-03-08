"""Security helpers for Miso Gallery."""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from functools import wraps
from threading import Lock
from typing import Any

from flask import jsonify, request, session

try:
    import redis
except ImportError:  # pragma: no cover - optional dependency in some environments
    redis = None

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitConfig:
    max_requests: int
    window: int


def _to_positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _load_route_overrides() -> dict[str, RateLimitConfig]:
    """Load endpoint-specific rate limit overrides from RATE_LIMIT_ROUTE_LIMITS.

    Expected format:
      {"auth": {"max_requests": 5, "window": 300}, "thumb": {"max_requests": 200, "window": 60}}
    """

    raw = os.environ.get("RATE_LIMIT_ROUTE_LIMITS", "").strip()
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Invalid RATE_LIMIT_ROUTE_LIMITS JSON; using decorator defaults")
        return {}

    if not isinstance(parsed, dict):
        logger.warning("RATE_LIMIT_ROUTE_LIMITS must be a JSON object; using decorator defaults")
        return {}

    overrides: dict[str, RateLimitConfig] = {}
    for endpoint, values in parsed.items():
        if not isinstance(endpoint, str) or not endpoint:
            continue
        if not isinstance(values, dict):
            continue

        max_requests = _to_positive_int(values.get("max_requests"), 0)
        window = _to_positive_int(values.get("window"), 0)
        if max_requests > 0 and window > 0:
            overrides[endpoint] = RateLimitConfig(max_requests=max_requests, window=window)

    return overrides


class InMemoryRateLimiter:
    """Fallback in-memory limiter (single-process only)."""

    def __init__(self) -> None:
        self._storage: dict[str, list[float]] = {}
        self._lock = Lock()

    def allow(self, key: str, max_requests: int, window: int) -> bool:
        now = time.time()
        with self._lock:
            history = [ts for ts in self._storage.get(key, []) if now - ts < window]
            if len(history) >= max_requests:
                self._storage[key] = history
                return False
            history.append(now)
            self._storage[key] = history
            return True


class RedisRateLimiter:
    """Redis/Dragonfly-backed limiter using a sorted-set sliding window."""

    LUA_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local max_requests = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)
if count >= max_requests then
  return 0
end

redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, math.ceil(window))
return 1
"""

    def __init__(self, redis_url: str, prefix: str) -> None:
        if redis is None:  # pragma: no cover
            raise RuntimeError("redis package not installed")

        self._client = redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
            health_check_interval=30,
        )
        self._prefix = prefix
        self._script = self._client.register_script(self.LUA_SCRIPT)
        self._client.ping()

    def allow(self, key: str, max_requests: int, window: int) -> bool:
        now = time.time()
        redis_key = f"{self._prefix}:{key}"
        member = f"{now}:{secrets.token_hex(8)}"
        allowed = self._script(keys=[redis_key], args=[now, window, max_requests, member])
        return bool(int(allowed))


FALLBACK_LIMITER = InMemoryRateLimiter()
ROUTE_LIMIT_OVERRIDES = _load_route_overrides()


def _build_primary_limiter() -> RedisRateLimiter | InMemoryRateLimiter:
    redis_url = os.environ.get("RATE_LIMIT_REDIS_URL") or os.environ.get("REDIS_URL")
    redis_prefix = os.environ.get("RATE_LIMIT_PREFIX", "miso-gallery:ratelimit")

    if not redis_url:
        return FALLBACK_LIMITER

    try:
        limiter = RedisRateLimiter(redis_url=redis_url, prefix=redis_prefix)
        logger.info("Rate limiter backend: redis")
        return limiter
    except Exception as exc:  # pragma: no cover - runtime fallback
        logger.warning("Failed to initialize Redis rate limiter (%s); using in-memory fallback", exc)
        return FALLBACK_LIMITER


PRIMARY_LIMITER = _build_primary_limiter()


def _client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        first = forwarded.split(",", 1)[0].strip()
        if first:
            return first

    cf_ip = request.headers.get("CF-Connecting-IP", "").strip()
    if cf_ip:
        return cf_ip

    return request.remote_addr or "unknown"


def _effective_config(endpoint: str, default_max_requests: int, default_window: int) -> RateLimitConfig:
    override = ROUTE_LIMIT_OVERRIDES.get(endpoint)
    if override:
        return override
    return RateLimitConfig(max_requests=default_max_requests, window=default_window)


def rate_limit(max_requests: int = 30, window: int = 60):
    """Rate limit decorator with Redis/Dragonfly backend and in-memory fallback."""

    default_max = _to_positive_int(max_requests, 30)
    default_window = _to_positive_int(window, 60)

    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            endpoint = request.endpoint or "unknown"
            config = _effective_config(endpoint, default_max, default_window)
            key = f"{_client_ip()}:{endpoint}"

            try:
                allowed = PRIMARY_LIMITER.allow(key, config.max_requests, config.window)
            except Exception as exc:  # pragma: no cover - runtime resilience
                logger.warning("Primary limiter failed (%s); using in-memory fallback", exc)
                allowed = FALLBACK_LIMITER.allow(key, config.max_requests, config.window)

            if not allowed:
                return (
                    jsonify(
                        {
                            "error": "Rate limit exceeded",
                            "max_requests": config.max_requests,
                            "window": config.window,
                        }
                    ),
                    429,
                )
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
