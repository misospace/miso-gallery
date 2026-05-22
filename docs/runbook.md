# Runbook: Miso Gallery Operations

## Table of Contents

- [Image Backup and Restore](#image-backup-and-restore)
- [Health Checks](#health-checks)
- [NFS Failure Handling](#nfs-failure-handling)
- [Authentication Recovery](#authentication-recovery)
  - [Local Password Recovery](#local-password-recovery)
  - [OIDC Recovery](#oidc-recovery)
  - [API Key Rotation](#api-key-rotation)
- [Webhook Task Execution](#webhook-task-execution)
- [Release Rollback](#release-rollback)
- [GHCR Image Verification](#ghcr-image-verification)

---

## Image Backup and Restore

### Overview

This runbook documents the procedure for backing up and restoring images stored in the Miso Gallery data directory.

### Backup Procedure

1. Ensure the gallery container is stopped or the data directory is not being written to.
2. Use `rsync` or a similar tool to copy the `/data` directory to a backup location:
   ```bash
   rsync -avh /data/ /backup/miso-gallery/
   ```
3. Verify the backup completed successfully.

### Restore Procedure

1. Stop the gallery container if it is running.
2. Copy the backup back to the data directory:
   ```bash
   rsync -avh /backup/miso-gallery/ /data/
   ```
3. Restart the container.

### Automation

Consider scheduling a cron job to perform regular backups.

---

## Health Checks

Miso Gallery exposes several health endpoints for probing service status and storage health.

### Root Health Endpoint

```
GET /health
```

**Expected response (healthy):**
```json
{
  "status": "healthy",
  "version": "0.1.16",
  "timestamp": "2026-05-18T09:00:00+00:00",
  "storage": {
    "status": "healthy",
    "data_folder": {
      "read": {"ok": true, "message": "Read access OK"},
      "write": {"ok": true, "message": "Write access OK"}
    },
    "thumbnail_cache": {
      "read": {"ok": true, "message": "Read access OK"},
      "write": {"ok": true, "message": "Write access OK"}
    }
  }
}
```

**Status codes:** `200` when healthy, `503` when any storage check fails.

### Storage Health Endpoints

| Endpoint | Purpose | Status Code (healthy) |
|---|---|---|
| `GET /health/storage` | Full storage read+write probe | 200 |
| `GET /health/storage/read` | Read-only storage probe (safe for probes) | 200 |
| `GET /health/storage/write` | Write-capable storage probe | 200 |

**Note:** The `/health/storage/write` endpoint performs a safe write test by creating and immediately deleting a temporary file. This is safe but should not be called at high frequency on NFS-backed storage.

### Unhealthy Signal File

When storage health becomes unhealthy, the app writes a signal file to:
```
/tmp/miso-gallery-storage-unhealthy.signal
```

Contents include:
```
status=unhealthy
timestamp=2026-05-18T09:00:00+00:00
data_folder=/data
thumbnail_cache=/data/.thumb_cache
```

To clear the signal after resolving the underlying issue, restart the container or fix the storage path and call `/health/storage` (the signal is cleared automatically on next healthy check).

---

## NFS Failure Handling

NFS-mounted storage is the most common cause of storage health failures.

### Symptoms

- `/health/storage` returns `503` with `"status": "unhealthy"`
- Gallery image loading is slow or fails
- Thumbnail generation hangs or errors
- The unhealthy signal file appears at `/tmp/miso-gallery-storage-unhealthy.signal`

### Recovery Steps

1. **Check NFS mount status:**
   ```bash
   mount | grep /data
   df -h /data
   ```

2. **Test connectivity to NFS server:**
   ```bash
   ping <nfs-server>
   nfsstat -s
   ```

3. **If the mount is stale (server unreachable):**
   - Try a soft remount: `mount -o remount,soft /data`
   - If that fails, restart the container (Docker may handle NFS reconnect)
   - For persistent issues, consider switching to hard mount with intr:
     ```bash
     mount -o remount,hard,intr <nfs-server>:/export /data
     ```

4. **After recovery:** Verify health endpoint returns `200`:
   ```bash
   curl -s http://localhost:5000/health/storage | python3 -m json.tool
   ```

### Prevention

- Use NFS hard mounts with `timeo=600,retrans=2` for production deployments.
- Monitor the unhealthy signal file with an external watchdog.
- Keep `STORAGE_HEALTH_SIGNAL_FILE` path writable by the gallery process.

---

## Authentication Recovery

Miso Gallery supports three authentication modes: `none`, `local` (password), and `oidc`.

### Local Password Recovery

If you lose access to the admin password:

1. **Generate a new password hash:**
   ```bash
   python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('new-password'))"
   ```
   This outputs a scrypt hash like `scrypt:32768:8:1$...`.

2. **Set the new password via environment variable:**
   ```bash
   export ADMIN_PASSWORD=scrypt:32768:8:1$...
   docker compose restart gallery  # or however you deploy
   ```

3. **Or set plaintext (not recommended for production):**
   ```bash
   export ADMIN_PASSWORD=new-password
   ```

**Supported formats:** The app accepts both plaintext and hashed (`pbkdf2:`, `scrypt:`) values for backward compatibility.

### OIDC Recovery

If OIDC login is broken:

1. **Verify OIDC configuration:**
   ```bash
   # Check if OIDC is enabled
   curl -s http://localhost:5000/about | grep -i oidc
   ```

2. **Check OIDC environment variables:**
   Ensure these are set correctly:
   - `OIDC_ENABLED=true`
   - `OIDC_ISSUER=https://your-oidc-provider/`
   - `OIDC_CLIENT_ID=<client-id>`
   - `OIDC_CLIENT_SECRET=<client-secret>`
   - `AUTH_TYPE=oidc`

3. **Disable OIDC temporarily to regain access:**
   ```bash
   export AUTH_TYPE=local
   export ADMIN_PASSWORD=<recovery-password>
   docker compose restart gallery
   ```

4. **Verify OIDC issuer metadata is reachable:**
   ```bash
   curl -s https://your-oidc-provider/.well-known/openid-configuration | python3 -m json.tool
   ```

5. **Common issues:**
   - **Redirect URI mismatch:** Ensure `OIDC_CALLBACK_URL` matches the callback registered in your OIDC provider. If not set, it defaults to `<base-url>/auth/oidc/callback`.
   - **Client secret rotation:** Regenerate the client secret in your OIDC provider and update `OIDC_CLIENT_SECRET`.

### API Key Rotation

Miso Gallery supports separate read and write API keys. The desired model:

| Variable | Scope | Endpoints |
|---|---|---|
| `LLM_READ_API_KEYS` | Read only | List, view, thumbnails, search |
| `LLM_WRITE_API_KEYS` | Write (implies read) | Delete, dedup, bulk operations, task execution + all read endpoints |
| `LLM_API_KEYS` | Both (legacy) | All endpoints; deprecated in favour of explicit keys |

**Key rules:**
- A write key is accepted on both read and write endpoints.
- A read-only key is rejected from write endpoints.
- When explicit `LLM_READ_API_KEYS` or `LLM_WRITE_API_KEYS` are set, the legacy `LLM_API_KEYS` value is ignored.

**To rotate keys:**

1. Set new keys via environment variables:
   ```bash
   export LLM_READ_API_KEYS="key1,key2"
   export LLM_WRITE_API_KEYS="write-key-1"
   docker compose restart gallery
   ```

2. Old keys are immediately invalidated — no grace period.

3. **To remove all API key auth:** Set both vars to empty:
   ```bash
   export LLM_READ_API_KEYS=""
   export LLM_WRITE_API_KEYS=""
   ```

**Note:** The `require_api_key` decorator (used on some endpoints) still accepts browser sessions for backward compatibility. For strict machine-only access, use `require_api_key_with_scope`.

---

## Webhook Task Execution

### Threat Model

Webhook tasks allow authenticated write-scoped API callers to execute arbitrary shell commands on the gallery host. This is a **high-privilege operation** with the following risks:

- A compromised API key can execute any command as the gallery process user.
- Command injection via untrusted payload parameters (e.g., image paths in task templates).
- Long-running tasks block the HTTP request and consume resources.

### Recommended Configuration

| Variable | Recommended Default | Purpose |
|---|---|---|
| `WEBHOOK_ENABLED` | `false` | Disable by default; enable only if needed |
| `WEBHOOK_TASK_TIMEOUT` | `30` | Max seconds per task (range: 1–600) |
| `LLM_WRITE_API_KEYS` | unique high-entropy tokens | Required for `/api/llm/task/run`; read-scoped keys are rejected |

### Enabling Tasks

Only enable webhook tasks when you have a specific automation need (e.g., triggering external processing pipelines):

```bash
export WEBHOOK_ENABLED=true
export WEBHOOK_TASK_RENAME="mv {path} /archive/{basename}"
# ... additional task definitions ...
```

Each task is defined as `WEBHOOK_TASK_<TASK_NAME>` environment variable where `<TASK_NAME>` is the task identifier. The value is a shell command template with `{path}`, `{basename}`, and other parameters injected from the request payload.

### Disabling Tasks (Emergency)

To immediately disable all webhook tasks:
```bash
export WEBHOOK_ENABLED=false
docker compose restart gallery
```

Tasks can also be disabled per-task by unsetting the `WEBHOOK_TASK_*` variable.

---

## Release Rollback

### Rolling Back a Bad Release

1. **Identify the previous known-good tag:**
   ```bash
   git tag --sort=-version:refname | head -5
   ```

2. **Pull the previous image from GHCR:**
   ```bash
   docker pull ghcr.io/misospace/miso-gallery:<previous-tag>
   ```

3. **Restart with the previous image:**
   ```bash
   # Update your compose file or deployment to use the previous tag
   docker compose up -d gallery
   ```

4. **Verify the rollback:**
   ```bash
   curl -s http://localhost:5000/health | python3 -m json.tool
   ```

### Publishing a New Release

1. Bump `APP_VERSION` in `app.py` (line ~110):
   ```python
   APP_VERSION = (os.environ.get("APP_VERSION") or "0.1.x").strip() or "0.1.x"
   ```

2. Run the Manual Release workflow from the GitHub Actions tab, or tag and push:
   ```bash
   npm version 0.1.x --no-git-tag-version
   git add app.py
   git commit -m "chore: bump APP_VERSION to 0.1.x"
   git tag 0.1.x
   git push origin main --tags
   ```

3. The release workflow builds the Docker image, runs tests, and publishes to GHCR.

4. **Version invariant check:** The release workflow now validates that `APP_VERSION` matches the release tag. If they do not match, the build fails with a clear error directing you to run:
   ```bash
   npm version <TAG> --no-git-tag-version
   ```

---

## GHCR Image Verification

### Verifying Image Integrity

After pulling a new image, verify it was published by the correct workflow:

```bash
# Check image metadata
docker inspect ghcr.io/misospace/miso-gallery:0.1.x | python3 -m json.tool

# Verify the image was built in CI (check created/updated timestamps)
docker history ghcr.io/misospace/miso-gallery:0.1.x
```

### Image Tags

| Tag | Description |
|---|---|
| `:latest` | Latest release |
| `:0.1.x` | Specific release version |
| `:main` | Latest main branch build (unstable) |

### Troubleshooting

- **Image pull fails:** Verify GHCR authentication and that the image exists:
  ```bash
  docker login ghcr.io
  skopeo inspect docker://ghcr.io/misospace/miso-gallery:0.1.x
  ```

- **Container starts but health checks fail:** Check the container logs:
  ```bash
  docker logs gallery
  ```
  Common causes: missing `SECRET_KEY`, invalid `DATA_FOLDER` permissions, or NFS mount issues.

---

*Last updated: 2026-05-18. Expanded from audit #139 recommendations.*
