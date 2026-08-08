#!/bin/sh
# Entrypoint for miso-gallery container.
#
# WEB_CONCURRENCY controls the number of gunicorn workers.
# Default is 2; set to 1 if you rely on the in-memory rate limiter
# (security.py) which is not shared across multiple workers.
# For production with Redis-backed rate limiting, increase as needed.

set -e

: "${WEB_CONCURRENCY:=2}"

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
