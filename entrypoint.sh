#!/bin/sh
# Entrypoint for miso-gallery container.
#
# WEB_CONCURRENCY controls the number of gunicorn workers.
#
# Cross-worker rate limiting (security.py) requires a shared backend.
# If neither REDIS_URL nor RATE_LIMIT_REDIS_URL is configured, the app
# refuses to start with >1 worker unless ALLOW_INMEMORY_RATE_LIMIT=1
# is explicitly set -- the in-memory limiter is per-process and would
# silently multiply the effective request quota by WEB_CONCURRENCY,
# weakening brute-force and webhook protection.
#
# To keep the shipped configuration safe by default, this entrypoint:
#   * defaults WEB_CONCURRENCY to 2 when a Redis URL is configured
#     (the secure multi-worker setup the issue recommends), and
#   * defaults WEB_CONCURRENCY to 1 when no Redis URL is configured,
#     so the in-memory limiter cannot ship under multi-worker silently.
# Operators can always override WEB_CONCURRENCY explicitly.

set -e

# Pick a safe default worker count based on whether a shared rate-limit
# backend is configured.
if [ -n "${REDIS_URL:-}" ] || [ -n "${RATE_LIMIT_REDIS_URL:-}" ]; then
    : "${WEB_CONCURRENCY:=2}"
else
    : "${WEB_CONCURRENCY:=1}"
    if [ "${WEB_CONCURRENCY}" -gt 1 ] && [ -z "${ALLOW_INMEMORY_RATE_LIMIT:-}" ]; then
        echo "[entrypoint] ERROR: WEB_CONCURRENCY=${WEB_CONCURRENCY} but no REDIS_URL /" >&2
        echo "[entrypoint]        RATE_LIMIT_REDIS_URL is configured. The in-memory rate" >&2
        echo "[entrypoint]        limiter in security.py is per-process and would multiply" >&2
        echo "[entrypoint]        the effective request quota by the worker count." >&2
        echo "[entrypoint]        Either set REDIS_URL / RATE_LIMIT_REDIS_URL, or set" >&2
        echo "[entrypoint]        ALLOW_INMEMORY_RATE_LIMIT=1 to opt in to the unsafe" >&2
        echo "[entrypoint]        per-worker limiter (development only)." >&2
        exit 1
    fi
fi

export WEB_CONCURRENCY

# Ensure /data exists and is writable (handles bind-mounted volumes).
mkdir -p /data

# Start gunicorn with environment-driven worker count and timeout.
# --timeout must be >= max WEBHOOK_TASK_TIMEOUT (120s) so long-running webhook tasks
# are not killed by the worker process. We add a 10s buffer to avoid race conditions.
exec gunicorn \
    --bind "0.0.0.0:${PORT:-5000}" \
    --workers "$WEB_CONCURRENCY" \
    --timeout 130 \
    app:app
