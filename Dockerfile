FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py auth.py security.py tag_store.py trash.py health.py ./
COPY templates/ templates/
COPY entrypoint.sh .

# Create unprivileged user and ensure /data is writable.
# If the container needs to bind to a privileged port (<1024), a root-init
# wrapper is needed; the default PORT=5000 does not require this.
RUN groupadd -r appuser && \
    useradd -r -g appuser -u 10001 appuser && \
    mkdir -p /data && \
    chown -R appuser:appuser /data

EXPOSE 5000

# Worker count is controlled by WEB_CONCURRENCY env var.
# entrypoint.sh defaults to 2 workers when a Redis URL is configured
# (REDIS_URL / RATE_LIMIT_REDIS_URL) so cross-worker rate limiting is real.
# Without a Redis URL it defaults to 1 worker, because the in-memory rate
# limiter in security.py is NOT shared across workers and would otherwise
# silently multiply the effective request quota by WEB_CONCURRENCY,
# weakening brute-force and webhook protection (issue #419).
USER 10001
ENTRYPOINT ["/bin/sh", "/app/entrypoint.sh"]
