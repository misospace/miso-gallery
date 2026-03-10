# Post-Deploy Smoke Check

This script verifies critical functionality after deployment.

## Usage

```bash
# Default (localhost)
./scripts/smoke-check.sh

# Custom URL
GALLERY_URL="https://gallery.example.com" ./scripts/smoke-check.sh
```

## What It Checks

1. **Root endpoint** (`/`) - Basic availability
2. **OIDC redirect** (`/auth/oidc`) - Authentication flow
3. **Authenticated routes** (`/recent`, `/trash`) - Verify auth is required
4. **Public view** (`/view/*`) - Public access still works

## Exit Codes

- `0` - All critical checks passed
- `1` - Some checks failed (review deployment)
