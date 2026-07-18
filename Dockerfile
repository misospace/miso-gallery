FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py auth.py security.py tag_store.py trash.py health.py .//
COPY templates/ templates/
COPY entrypoint.sh .

RUN mkdir -p /data

EXPOSE 5000

# Worker count is controlled by WEB_CONCURRENCY env var (default: 2).
# NOTE: The in-memory rate limiter in security.py is NOT shared across workers.
# With WEB_CONCURRENCY > 1, rate limiting is per-worker and less effective.
# For accurate cross-worker rate limiting, configure REDIS_URL in security.py.
ENTRYPOINT ["/bin/sh", "/app/entrypoint.sh"]
