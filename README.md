# Miso Gallery

<p align="center">
  <img src="https://img.shields.io/docker/v/joryirving/miso-gallery?sort=semver&label=docker" alt="Docker">
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
| `SECRET_KEY` | If OIDC | random | Flask secret for sessions |
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
