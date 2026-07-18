#!/bin/sh
# Entrypoint for miso-gallery container.
#
# WEB_CONCURRENCY controls the number of gunicorn workers.
# Default is 2; set to 1 if you rely on the in-memory rate limiter
# (security.py) which is not shared across multiple workers.
# For production with Redis-backed rate limiting, increase as needed.

set -e

: "${WEB_CONCURRENCY:=2}"

exec gunicorn \
    --bind "0.0.0.0:${PORT:-5000}" \
    --workers "$WEB_CONCURRENCY" \
    app:app
