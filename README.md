# Miso Gallery

<p align="center">
  <img src="https://img.shields.io/docker/v/ghcr.io/joryirving/miso-gallery?sort=semver&label=ghcr" alt="GHCR">
  <img src="https://github.com/joryirving/miso-gallery/actions/workflows/build.yaml/badge.svg" alt="Build">
  <img src="https://img.shields.io/github/v/release/joryirving/miso-gallery?sort=semver" alt="Release">
  <img src="https://img.shields.io/github/license/joryirving/miso-gallery" alt="License">
</p>

> A lightweight, self-hosted image gallery for AI-generated images.

## Features

- 🍲 **Simple**: Flask-based, minimal dependencies
- 📱 **Mobile-friendly**: PWA-ready responsive design
- 🔐 **Authentication**: OIDC (Authentik, Okta, Google) + local password
- 🖼️ **Thumbnails**: Auto-generated thumbnail caching
- 🗑️ **Bulk operations**: Multi-select delete
- 🔄 **Refresh**: Live refresh button
- 🐳 **Containerized**: Docker + Kubernetes deployment ready

## Quick Start

### Docker

```bash
docker run -d --name miso-gallery \
  -p 5000:5000 \
  -v /path/to/images:/data \
  ghcr.io/joryirving/miso-gallery:latest
```

### Docker Compose

```yaml
services:
  miso-gallery:
    image: ghcr.io/joryirving/miso-gallery:latest
    ports:
      - "5000:5000"
    volumes:
      - ./images:/data
    environment:
      - ADMIN_PASSWORD=your-password
```

## Configuration

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATA_FOLDER` | No | `/data` | Path to image directory |
| `IMAGE_BASE_URL` | No | - | Base URL for shareable links |
| `PORT` | No | `5000` | Server port |

### Authentication

Miso Gallery supports two authentication methods:

#### Local Password (Default)

```bash
docker run -d \
  -e ADMIN_PASSWORD=your-password \
  ghcr.io/joryirving/miso-gallery:latest
```

#### OIDC / Authentik

```bash
docker run -d \
  -e AUTH_TYPE=oidc \
  -e OIDC_ISSUER=https://authentik.yourdomain.com \
  -e OIDC_CLIENT_ID=miso-gallery \
  -e OIDC_CLIENT_SECRET=your-secret \
  -e OIDC_CALLBACK_URL=https://miso-gallery.yourdomain.com/auth/callback \
  -e SECRET_KEY=your-session-secret \
  ghcr.io/joryirving/miso-gallery:latest
```

#### Auth Configuration Options

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AUTH_TYPE` | No | `local` | Auth method: `local`, `oidc`, or `none` |
| `ADMIN_PASSWORD` | If local | - | Password for local auth (plaintext or Werkzeug hash: `pbkdf2:` / `scrypt:`) |
| `SECRET_KEY` | Yes | - | Flask secret for sessions. Generate with: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `LLM_API_KEYS` | No | - | Comma-separated Bearer tokens for the LLM API (`Authorization: Bearer <token>`) |
| `OIDC_ISSUER` | If OIDC | - | OIDC provider URL (e.g., https://authentik.example.com) |
| `OIDC_CLIENT_ID` | If OIDC | - | OIDC client ID |
| `OIDC_CLIENT_SECRET` | If OIDC | - | OIDC client secret |
| `OIDC_CALLBACK_URL` | If OIDC | - | Callback URL for OIDC |

#### Rate Limiting

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `RATE_LIMIT_REDIS_URL` | No | - | Redis/Dragonfly URL for shared rate-limit state (falls back to in-memory if unset/unreachable) |
| `RATE_LIMIT_PREFIX` | No | `miso-gallery:ratelimit` | Key prefix for rate-limit entries |
| `RATE_LIMIT_ROUTE_LIMITS` | No | - | JSON overrides per endpoint, e.g. `{"auth":{"max_requests":5,"window":300}}` |

See [docs/rate-limit-shared-backend.md](docs/rate-limit-shared-backend.md) for recommended production rollout and migration plan.

#### Authentik Setup

1. Create an Application in Authentik
2. Create a Provider (OpenID Connect) with these settings:
   - Client ID: `miso-gallery`
   - Client Secret: Generate a secure secret
   - Signing Key: Select default
   - Redirect URIs: `https://miso-gallery.yourdomain.com/auth/callback`
3. Copy the Provider URL (issuer) to `OIDC_ISSUER`

## Kubernetes Deployment

### HelmRelease Example

```yaml
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: miso-gallery
  namespace: apps
spec:
  chart:
    spec:
      chart: app-template
      version: 3.0
  values:
    controllers:
      miso-gallery:
        containers:
          app:
            image:
              repository: ghcr.io/joryirving/miso-gallery
              tag: latest
            env:
              - name: ADMIN_PASSWORD
                valueFrom:
                  secretKeyRef:
                    name: miso-gallery-secrets
                    key: password
            persistence:
              data:
                type: nfs
                server: nfs.yourdomain.com
                path: /path/to/images
    service:
      app:
        ports:
          http:
            port: 5000
    route:
      app:
        hostnames:
          - gallery.yourdomain.com
        parentRefs:
          - name: envoy-external
```

## Features

### Thumbnails

Thumbnails are automatically generated and cached in `.thumb_cache/` directory. This improves loading performance for large galleries.

- Max size: 400x400
- Format: Optimized JPEG
- Auto-refresh: Thumbnails regenerate when source image changes

### Multi-Select

- Click checkboxes on images to select
- Use "Select All" / "Deselect All" buttons
- Bulk delete selected images

### Direct Image Access

Even when authentication is enabled, direct URLs to images remain publicly accessible:

- `/view/folder/image.jpg` - Full resolution
- `/thumb/folder/image.jpg` - Thumbnail

This allows sharing images while protecting the gallery UI.

## LLM API

Miso Gallery includes a JSON API intended for LLM agents and other machine-to-machine clients. The primary purpose is to let an external LLM client inspect and manage gallery state: list/search media, read metadata, tag, delete, bulk-delete, and deduplicate images.

Enable the API by setting one or more comma-separated API keys:

```bash
docker run -d --name miso-gallery \
  -p 5000:5000 \
  -v /path/to/images:/data \
  -e SECRET_KEY=your-session-secret \
  -e ADMIN_PASSWORD=your-password \
  -e LLM_API_KEYS=agent-key-1,agent-key-2 \
  ghcr.io/joryirving/miso-gallery:latest
```

Authenticate each request with a Bearer token:

```bash
curl -H "Authorization: Bearer agent-key-1" \
  http://localhost:5000/api/llm/images
```

LLM API endpoints are token-authenticated and do not require CSRF tokens. Existing browser/session authentication continues to work for the UI.

### Read Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/llm/images?q=<query>` | Recursively list/search media. `q` matches filenames or relative paths. |
| `GET` | `/api/llm/image/<relpath>` | Return metadata for a single image/video. |
| `GET` | `/api/llm/recent?limit=N` | Return recent media sorted by modification time. Default `50`, max `500`. |
| `GET` | `/api/llm/folders` | Return folder listing with relative paths and parent folders. |

Example:

```bash
curl -H "Authorization: Bearer agent-key-1" \
  "http://localhost:5000/api/llm/images?q=cat"
```

Response shape:

```json
{
  "count": 1,
  "images": [
    {
      "name": "cat.jpg",
      "rel_path": "cats/cat.jpg",
      "media_type": "image",
      "size": 12345,
      "size_human": "12.1 KB",
      "modified": "2026-04-28T12:34:56Z",
      "mtime": 1777398896.0,
      "view_url": "/view/cats/cat.jpg",
      "thumb_url": "/thumb/cats/cat.jpg"
    }
  ]
}
```

### Write Endpoints

| Method | Path | Body | Description |
|--------|------|------|-------------|
| `POST` | `/api/llm/tags` | `{"rel_path":"cats/cat.jpg","tag":"favorite","action":"add"}` | Add/remove tags. Tag persistence is currently log-only. |
| `POST` | `/api/llm/delete` | `{"rel_path":"cats/cat.jpg"}` | Move one media file to trash and clear its thumbnail cache. |
| `POST` | `/api/llm/bulk-delete` | `{"rel_paths":["a.jpg","b.jpg"]}` | Move multiple media files to trash. |
| `POST` | `/api/llm/dedup` | `{}` or `{"remove":true}` | Find duplicate media by SHA-256. Defaults to dry-run; `remove:true` moves duplicates to trash. |

Delete example:

```bash
curl -X POST \
  -H "Authorization: Bearer agent-key-1" \
  -H "Content-Type: application/json" \
  -d '{"rel_path":"cats/cat.jpg"}' \
  http://localhost:5000/api/llm/delete
```

Dedup dry-run example:

```bash
curl -X POST \
  -H "Authorization: Bearer agent-key-1" \
  -H "Content-Type: application/json" \
  -d '{}' \
  http://localhost:5000/api/llm/dedup
```

### Optional: Server-Side Task Execution

Most LLM integrations do **not** need task execution. Use the gallery-management endpoints above unless you intentionally want Miso Gallery to expose a small set of preconfigured server-side automation commands.

Task execution is an optional advanced feature that reuses the existing webhook task infrastructure. It is disabled unless `WEBHOOK_ENABLED=true`, and only commands explicitly configured through `WEBHOOK_TASK_*` environment variables can be run. This can be useful for trusted maintenance or generation scripts that should run on the gallery host, but it is not required for normal LLM-to-gallery interaction.

```bash
docker run -d --name miso-gallery \
  -p 5000:5000 \
  -v /path/to/images:/data \
  -e SECRET_KEY=your-session-secret \
  -e LLM_API_KEYS=agent-key-1 \
  -e WEBHOOK_ENABLED=true \
  -e 'WEBHOOK_TASK_GENERATE=python3 /data/scripts/generate.py {params.prompt}' \
  ghcr.io/joryirving/miso-gallery:latest
```

Run a configured task:

```bash
curl -X POST \
  -H "Authorization: Bearer agent-key-1" \
  -H "Content-Type: application/json" \
  -d '{"task":"generate","params":{"prompt":"a cozy bowl of miso soup"}}' \
  http://localhost:5000/api/llm/task/run
```

Task commands run from `DATA_FOLDER`, scalar params are shell-quoted, and the timeout is controlled by `WEBHOOK_TASK_TIMEOUT` with a default of 30 seconds.

## Development

### Local Development

```bash
# Clone and setup
git clone https://github.com/joryirving/miso-gallery.git
cd miso-gallery

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run development server
python app.py
```

### Building

```bash
# Build Docker image
docker build -t miso-gallery:latest .

# Run locally
docker run -p 5000:5000 -v ./images:/data miso-gallery:latest
```

### Releases

Use the **Manual Release** GitHub Actions workflow and enter a version like `0.4.6`. It normalizes `v0.4.6` to `0.4.6`, updates the in-app version string in `app.py`, pushes that bump to `main` through the configured bot identity, creates the plain-semver tag, and creates the GitHub release with generated notes.

## License

MIT
