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

# Worker count is controlled by WEB_CONCURRENCY env var (default: 2).
# NOTE: The in-memory rate limiter in security.py is NOT shared across workers.
# With WEB_CONCURRENCY > 1, rate limiting is per-worker and less effective.
# For accurate cross-worker rate limiting, configure REDIS_URL in security.py.
USER 10001
ENTRYPOINT ["/bin/sh", "/app/entrypoint.sh"]
