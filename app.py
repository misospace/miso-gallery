from __future__ import annotations

import hashlib
import html
import json
import logging
import mimetypes
import os
import re
import secrets
import shlex
import subprocess
import threading
import time
import warnings
from base64 import urlsafe_b64encode
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from flask.sessions import SecureCookieSessionInterface
from PIL import Image, UnidentifiedImageError
from werkzeug.utils import secure_filename

from auth import (
    configure_oauth,
    get_oidc_label,
    is_auth_enabled,
    is_oidc_configured,
    require_auth,
    resolved_auth_mode,
    verify_local_password,
)
from gallery_scan import (
    GALLERY_PAGE_DEFAULT,
    GALLERY_PAGE_MAX,
    GALLERY_SCAN_CACHE_MAX_ENTRIES,
    GALLERY_SCAN_CACHE_TTL,
    GALLERY_SCAN_LIMIT,
    RECENT_ENUMERATION_LIMIT,
    _apply_scan_limit,
    _gallery_item_rel_path,
    _gallery_scan_cache,
    _invalidate_gallery_scan_cache,
    _paginate,
    _parse_pagination,
    _tag_store,
    file_sha256,
    find_duplicate_media,
    format_size,
    is_excluded_gallery_path,
    is_media_file,
    iter_gallery_items,
    media_metadata,
    recent_gallery_entries,
    sanitize_rel_path,
    source_file_path,
)
from health import health, storage_health, storage_health_read, storage_health_write
from llm_api import load_env_config as load_llm_env_config
from llm_api import register_llm_routes
from metadata import read_image_metadata
from oidc import register_oidc_routes
from security import (
    _client_ip,
    add_security_headers,
    csrf_token,
    is_safe_redirect_url,
    rate_limit,
    sanitize_path,
    validate_csrf,
)
from tag_store import TagStore
from thumbnails import (
    batch_remove_thumbnails,
    ensure_thumbnail_cache_dir,
    folder_cover_rel_path,
    generate_thumbnail,
    load_env_config,
    remove_thumbnail_cache_for,
    run_thumbnail_integrity_check,
    thumbnail_filename,
)
from trash import (
    dir_size,
    empty_trash,
    list_trash,
    move_to_trash,
    purge_old_trash,
    restore_from_trash,
)

# The thumbnail/folder-cover module reads its GALLERY_* settings at import
# time; refresh them on every app import so reloading app (as the test
# bootstrap does) observes per-run environment overrides.
load_env_config()

# The LLM API module reads its destructive-operation guardrail limits from the
# environment at import time; refresh them on every app import for the same
# reason as load_env_config() above.
load_llm_env_config()

# Load service worker from external file (extracted from app.py)
SERVICE_WORKER_PATH = os.path.join(os.path.dirname(__file__), "templates", "service-worker.js")
with open(SERVICE_WORKER_PATH, "r") as _f:
    SERVICE_WORKER_TEMPLATE = _f.read()

log = logging.getLogger("app")


DATA_FOLDER = Path(os.environ.get("DATA_FOLDER", "/data"))
THUMBNAIL_CACHE_DIR = DATA_FOLDER / ".thumb_cache"
TAG_DATABASE = Path(os.environ.get("TAG_DATABASE", str(DATA_FOLDER / ".miso-gallery-tags.sqlite3")))

# In-browser upload (issue #485): browser uploads land in UPLOAD_DIR so that
# ad-hoc images can be shared without write access to the gallery's image
# share. The upload target must remain in the gallery root.
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", str(DATA_FOLDER / "input"))).resolve()
try:
    UPLOAD_DIR.relative_to(DATA_FOLDER.resolve())
except ValueError as exc:
    raise ValueError("UPLOAD_DIR must resolve within DATA_FOLDER") from exc


def resolve_secret_key() -> str:
    configured = os.environ.get("SECRET_KEY", "").strip()
    if not configured:
        raise ValueError(
            "SECRET_KEY environment variable is required. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    return configured


app = Flask(__name__)


@app.before_request
def set_csp_nonce():
    g.csp_nonce = secrets.token_urlsafe(16)

app.secret_key = resolve_secret_key()

# Session persistence configuration for mobile compatibility
# PERMANENT_SESSION_LIFETIME: 30 days for persistent sessions
# SESSION_COOKIE_SECURE: Only send over HTTPS (set via env for flexibility)
# SESSION_COOKIE_SAMESITE: Lax to allow cross-site navigation while maintaining security
# SESSION_COOKIE_HTTPONLY: Prevent JavaScript access to session cookie
app.config["PERMANENT_SESSION_LIFETIME"] = int(os.environ.get("SESSION_LIFETIME_DAYS", 30)) * 24 * 60 * 60
# Cap browser uploads (issue #485); 2 GiB default keeps shared media files
# within the "ad-hoc images" use case.
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024)))
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "true").strip().lower() == "true"
app.config["SESSION_COOKIE_SAMESITE"] = os.environ.get("SESSION_COOKIE_SAMESITE", "Lax")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_REFRESH_EACH_REQUEST"] = False


class EncryptedSessionInterface(SecureCookieSessionInterface):
    """Encrypt session data with Fernet so OIDC tokens are not client-readable.

    Flask's default SecureCookieSessionInterface signs (HMAC) the cookie but does
    not encrypt it, meaning any value stored in ``session`` is visible to the
    browser.  This subclass wraps the serialised JSON payload in a Fernet
    ciphertext before signing, so the cookie payload is opaque even if an
    attacker obtains the SECRET_KEY (they would still need the derived Fernet key).
    """

    def _get_fernet(self, app):
        # Derive a 32-byte URL-safe key from the app's SECRET_KEY so we don't
        # require a separate configuration value.
        key = hashlib.sha256(app.secret_key.encode("utf-8")).digest()
        return Fernet(urlsafe_b64encode(key))

    def get_signing_serializer(self, app):
        if not app.secret_key:
            return None
        fernet = self._get_fernet(app)
        serializer = super().get_signing_serializer(app)
        if serializer is None:
            return None

        # Wrap the parent's sign/dump so encryption happens around the JSON.
        original_dump = serializer.dump
        original_load = serializer.load

        def encrypted_dump(data):
            json_bytes = original_dump(data)
            return fernet.encrypt(json_bytes)

        def encrypted_load(payload):
            try:
                decrypted = fernet.decrypt(payload)
            except InvalidToken:
                return None
            return original_load(decrypted)

        serializer.dump = encrypted_dump
        serializer.load = encrypted_load
        return serializer


app.session_interface = EncryptedSessionInterface()

app.after_request(add_security_headers)

# ---------------------------------------------------------------------------
# Startup-time security checks
# ---------------------------------------------------------------------------

if (os.environ.get("WEBHOOK_ENABLED", "").strip().lower() == "true"
        and not os.environ.get("WEBHOOK_SECRET")):
    log.warning(
        "SECURITY: WEBHOOK_ENABLED=true but WEBHOOK_SECRET is not set. "
        "The /api/webhook/run endpoint will be unavailable (503) to prevent "
        "unauthenticated command execution."
    )


def _request_id() -> str:
    """Generate a short unique request ID for audit logging."""
    return secrets.token_hex(8)


def log_security_event(event: str, outcome: str, *, request_id: str = "", **fields: object) -> None:
    """Emit a structured log line for security/access events.

    Intentionally avoids logging secrets (passwords, OIDC tokens, raw userinfo).
    """

    try:
        if not request_id:
            request_id = _request_id()
        payload: dict[str, object] = {
            "event": event,
            "outcome": outcome,
            "path": request.path,
            "method": request.method,
            "remote_addr": _client_ip(),
            "user_id": session.get("user_id"),
            "user_name": session.get("user_name"),
            "auth_method": session.get("auth_method") or ("local" if session.get("authenticated") else None),
            "api_key_hint": session.get("api_key_hint"),
            "api_key_class": session.get("api_key_class"),  # key class identifier for audit logs
            "request_id": request_id,
        }
        payload.update(fields)
        payload = {k: v for k, v in payload.items() if v is not None}
        app.logger.info(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    except Exception:
        # Never break request handling due to logging
        return


# Configure OAuth for OIDC if enabled
configure_oauth(app)
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
VIDEO_EXTENSIONS = (".gif", ".mp4", ".webm", ".mov")
FAVICON_URL = os.environ.get("FAVICON_URL", "").strip()

PWA_THEME_COLOR = "#0d0d0d"
PWA_APP_NAME = "Miso Gallery"
# The bundled default version. release-please bumps the block below in its
# release PR; scripts/read_app_version.py resolves the indirection.
# x-release-please-start-version
DEFAULT_APP_VERSION = "0.3.0"
# x-release-please-end
APP_VERSION = (os.environ.get("APP_VERSION") or DEFAULT_APP_VERSION).strip() or DEFAULT_APP_VERSION
WEBHOOK_TASK_PREFIX = "WEBHOOK_TASK_"
_VALID_TASK_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# Per-process concurrency cap for webhook task subprocesses (issue #448).
#
# `run_configured_task()` invokes a configured command via `subprocess.run(...)`
# synchronously in the request thread. With `WEBHOOK_TASK_TIMEOUT` capped at 120s
# and gunicorn `--timeout` set to 130s, a single webhook call can occupy a worker
# for up to two minutes. The shipped default worker count is 2
# (entrypoint.sh, Dockerfile); a webhook-secret holder (or anyone with a
# write-scoped LLM API key) can submit enough concurrent long-running task
# requests to saturate the worker pool and stall every other route.
#
# Capping concurrent in-flight task invocations at 1 per gunicorn worker
# ensures any concurrent request that arrives while a task is already running
# is rejected with 503 immediately rather than blocking the worker (or the
# gunicorn pool) indefinitely. Each gunicorn worker process gets its own
# semaphore instance because module-level state is per-process.
WEBHOOK_TASK_MAX_CONCURRENT = 1
_webhook_task_slot = threading.BoundedSemaphore(WEBHOOK_TASK_MAX_CONCURRENT)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()

# Destructive-operation guardrails (issue #199)
BULK_DELETE_MAX_ITEMS = int(os.environ.get("BULK_DELETE_MAX_ITEMS", "200"))
BULK_DELETE_MAX_FOLDERS = int(os.environ.get("BULK_DELETE_MAX_FOLDERS", "50"))
BULK_DELETE_FOLDER_SIZE_CAP = int(os.environ.get("BULK_DELETE_FOLDER_SIZE_CAP", str(10 * 1024 * 1024 * 1024)))  # 10 GB default


def _webhook_enabled() -> bool:
    return os.environ.get("WEBHOOK_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}

def _verify_webhook_secret(token: str) -> bool:
    """Verify Bearer token matches WEBHOOK_SECRET if configured."""
    if not WEBHOOK_SECRET:
        return False
    return secrets.compare_digest(token, WEBHOOK_SECRET)


def _task_env_key(task_name: str) -> str:
    normalized = "".join(ch if ch.isalnum() else "_" for ch in task_name).strip("_").upper()
    return f"{WEBHOOK_TASK_PREFIX}{normalized}" if normalized else ""


def _validate_task_name(task_name: str) -> str | None:
    """Validate that a task name only contains safe characters.

    Returns an error message string if validation fails, or None if valid.
    Only [a-zA-Z0-9_-] are permitted to prevent env var key collision and injection.
    """
    if not _VALID_TASK_RE.match(task_name):
        return (
            f"Invalid task name: '{task_name}'. "
            "Task names may only contain letters, digits, hyphens, and underscores."
        )
    return None


def _render_task_command(template: str, params: dict[str, object]) -> str:
    rendered = template
    for key, value in params.items():
        if isinstance(value, (dict, list)):
            raise ValueError(f"params.{key} must be a scalar value")
        rendered = rendered.replace(f"{{params.{key}}}", shlex.quote(str(value)))

    if "{params." in rendered:
        raise ValueError("missing required params for command template")

    return rendered


def run_configured_task(payload: dict[str, object]) -> tuple[dict[str, object], int]:
    if not _webhook_enabled():
        return {"error": "Webhook tasks are disabled"}, 404

    task = str(payload.get("task", "")).strip()
    params = payload.get("params") or {}

    if not task:
        return {"error": "task is required"}, 400
    if not isinstance(params, dict):
        return {"error": "params must be an object"}, 400

    validation_error = _validate_task_name(task)
    if validation_error is not None:
        return {"error": validation_error}, 400

    env_key = _task_env_key(task)
    if not env_key:
        return {"error": "invalid task name"}, 400

    template = os.environ.get(env_key, "").strip()
    if not template:
        return {"error": f"task '{task}' is not configured"}, 404

    try:
        command = _render_task_command(template, params)
        argv = shlex.split(command)
    except ValueError as exc:
        return {"error": str(exc)}, 400

    if not argv:
        return {"error": "configured task produced an empty command"}, 500

    try:
        timeout = max(1, min(120, int(os.environ.get("WEBHOOK_TASK_TIMEOUT", "30"))))
    except ValueError:
        timeout = 30

    # Per-worker concurrency cap (issue #448): refuse with 503 if a task is
    # already in flight in this gunicorn worker. Blocking until the slot is
    # free would let a single slow task pin the worker (or, with concurrent
    # request threads, the entire worker pool) for up to WEBHOOK_TASK_TIMEOUT
    # seconds — the very worker-exhaustion DoS this guard prevents.
    if not _webhook_task_slot.acquire(blocking=False):
        log_security_event(
            "webhook_task",
            "rejected",
            task=task,
            reason="pool_saturated",
            max_concurrent=WEBHOOK_TASK_MAX_CONCURRENT,
        )
        return {
            "task": task,
            "success": False,
            "error": (
                "webhook task pool saturated: at most "
                f"{WEBHOOK_TASK_MAX_CONCURRENT} task(s) may run concurrently per "
                "worker. Retry after the in-flight task completes."
            ),
        }, 503

    try:
        try:
            completed = subprocess.run(
                argv,
                cwd=str(DATA_FOLDER),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log_security_event("webhook_task", "error", task=task, reason="timeout", timeout=timeout)
            return {"task": task, "success": False, "error": f"task timed out after {timeout}s"}, 504
        except OSError as exc:
            log_security_event("webhook_task", "error", task=task, reason="spawn_failed", error=str(exc))
            return {"task": task, "success": False, "error": f"failed to execute task: {exc}"}, 500

        success = completed.returncode == 0
        log_security_event("webhook_task", "success" if success else "error", task=task, exit_code=completed.returncode)
        return {
            "task": task,
            "success": success,
            "exitCode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }, 200
    finally:
        _webhook_task_slot.release()


@app.before_request
def check_auth():
    if not is_auth_enabled():
        return None

    if request.path.startswith("/api/llm/"):
        return None

    # Webhook run endpoint authenticates via its own bearer-secret check;
    # it is a machine API and must not be redirected to /login when no
    # browser session is present. The route's own _verify_webhook_secret
    # gate handles missing/incorrect secrets.
    if request.path == "/api/webhook/run":
        return None

    # Health probes are consumed by k8s liveness/readiness, load balancers,
    # and uptime monitors — none of which carry a session cookie. They must
    # always return their JSON 200/503 payload, never a redirect to /login.
    if request.path == "/health" or request.path.startswith("/health/"):
        return None

    if request.path.startswith("/images/") or request.path.startswith("/view/"):
        return None

    if (
        request.path.startswith("/thumb/")
        or request.path.startswith("/assets/")
        or request.path in {"/favicon.ico", "/manifest.webmanifest", "/service-worker.js"}
    ):
        return None

    if request.path in ["/login", "/auth", "/logout", "/auth/oidc", "/auth/oidc/callback", "/auth/oidc/refresh"]:
        return None

    if session.get("authenticated"):
        return None

    return redirect(url_for("login", next=request.path))


@app.route("/favicon.ico")
def favicon():
    if FAVICON_URL:
        return redirect(FAVICON_URL)

    logo_path = Path(app.root_path) / "assets" / "miso-gallery-logo.png"
    if logo_path.exists():
        return send_from_directory(str(logo_path.parent), logo_path.name)

    return ("", 204)

@app.route("/assets/<path:filename>")
def assets(filename: str):
    safe_name = sanitize_rel_path(filename)
    asset_dir = Path(app.root_path) / "assets"
    return send_from_directory(str(asset_dir), safe_name)


@app.route("/manifest.webmanifest")
def manifest():
    payload = {
        "name": PWA_APP_NAME,
        "short_name": "Miso",
        "description": "Mobile-first gallery for Miso images",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": PWA_THEME_COLOR,
        "theme_color": PWA_THEME_COLOR,
        "icons": [
            {
                "src": "/assets/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable",
            },
            {
                "src": "/assets/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable",
            },
        ],
    }
    response = make_response(json.dumps(payload))
    response.headers["Content-Type"] = "application/manifest+json"
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@app.route("/service-worker.js")
def service_worker():
    response = make_response(SERVICE_WORKER_TEMPLATE)
    response.headers["Content-Type"] = "application/javascript; charset=utf-8"
    response.headers["Cache-Control"] = "no-cache"
    return response



@app.route("/images/<path:filename>")
def images(filename: str):
    rel_path = sanitize_rel_path(filename)
    if not is_media_file(DATA_FOLDER / rel_path):
        abort(404)
    return send_from_directory(str(DATA_FOLDER), rel_path)


@app.route("/")
@app.route("/<path:subpath>")
@require_auth
def index(subpath: str = ""):
    # The catch-all must not shadow the /auth/* routes: without this guard a
    # GET to a POST-only auth endpoint (e.g. /auth/oidc/refresh) would match
    # this route and 404 instead of letting Flask return 405 (Issue #454).
    if subpath.startswith("auth/"):
        return "Not found", 404

    # Search query for filtering items
    search_query = request.args.get('q', '').strip().lower()
    bulk_state = request.args.get('bulk_state', '').strip().lower()
    bulk_deleted = request.args.get('bulk_deleted', '0').strip()
    bulk_folders = request.args.get('bulk_folders', '0').strip()
    safe_subpath = sanitize_rel_path(subpath) if subpath else ""
    folder_path = DATA_FOLDER / safe_subpath
    if not folder_path.exists() or not folder_path.is_dir():
        return "Folder not found", 404

    items = []
    stats = {"folders": 0, "images": 0}

    has_more = False
    for item in sorted(folder_path.iterdir(), key=lambda p: p.name.lower()):
        # Skip symlinks to prevent filesystem traversal outside the data root,
        # and use the same hidden-segment exclusion as iter_gallery_items() so
        # dotfiles / dot-folders (e.g. `.git/HEAD`, `.DS_Store`) cannot surface
        # in the gallery UI. See issue #444.
        if item.is_symlink() or is_excluded_gallery_path(item):
            continue

        rel_path = f"{safe_subpath}/{item.name}".lstrip("/") if safe_subpath else item.name
        rel_path = rel_path.replace("\\", "/")

        if item.is_dir():
            stats["folders"] += 1
            cover_rel_path = folder_cover_rel_path(rel_path)
            items.append(
                {
                    "name": item.name,
                    "rel_path": rel_path,
                    "url": url_for("index", subpath=rel_path),
                    "cover_thumb_url": url_for("thumb", filename=cover_rel_path) if cover_rel_path else None,
                    "is_dir": True,
                }
            )
        elif item.suffix.lower() in IMAGE_EXTENSIONS or item.suffix.lower() in VIDEO_EXTENSIONS:
            stats["images"] += 1
            item_stat = item.stat()
            is_video = item.suffix.lower() in VIDEO_EXTENSIONS
            items.append(
                {
                    "name": item.name,
                    "rel_path": rel_path,
                    "thumb_url": url_for("thumb", filename=rel_path),
                    "view_url": url_for("view", filename=rel_path),
                    "delete_url": url_for("delete", filename=rel_path),
                    "size": format_size(item_stat.st_size),
                    "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(item_stat.st_mtime)),
                    "is_dir": False,
                    "media_type": "video" if is_video else "image",
                }
            )

        # Apply scan limit to subfolder browsing (Issue #386).
        if len(items) >= GALLERY_SCAN_LIMIT:
            has_more = True
            break

    # Location uploads land in, when it is inside the gallery root so the
    # upload button can deep-link there (issue #485).
    try:
        upload_dir_rel = UPLOAD_DIR.resolve().relative_to(DATA_FOLDER.resolve()).as_posix()
    except (ValueError, OSError):
        upload_dir_rel = ""

    # Apply category search filter (root only).
    # Issue #51 expects folder/category name substring matching.
    if search_query and not safe_subpath:
        items = [i for i in items if i.get("is_dir") and search_query in i["name"].lower()]
        stats["folders"] = sum(1 for i in items if i.get("is_dir"))
        stats["images"] = sum(1 for i in items if not i.get("is_dir"))

    # Detect scan truncation (Issue #349).
    scanned_count = stats["folders"] + stats["images"]
    scan_truncated = _apply_scan_limit(has_more=has_more, scanned_count=scanned_count)
    scan_limit = GALLERY_SCAN_LIMIT

    parent_url = None
    nav_crumbs: list[dict[str, object]] = []
    if safe_subpath:
        parts = safe_subpath.split("/")
        crumbs = ['<a href="/">Home</a>']
        for i, part in enumerate(parts[:-1]):
            path = "/".join(parts[: i + 1])
            crumbs.append(f'<a href="/{html.escape(path)}">{html.escape(part)}</a>')
        crumbs.append(html.escape(parts[-1]))
        breadcrumb = " / ".join(crumbs)

        accum: list[str] = []
        for part in parts:
            accum.append(part)
            path = "/".join(accum)
            nav_crumbs.append({"name": part, "url": url_for("index", subpath=path), "is_current": path == safe_subpath})

        parent_subpath = "/".join(parts[:-1])
        parent_url = url_for("index", subpath=parent_subpath) if parent_subpath else url_for("index")
    else:
        breadcrumb = "All Images"

    bulk_feedback = None
    if bulk_state == "success":
        moved_files = int(bulk_deleted) if bulk_deleted.isdigit() else 0
        moved_folders = int(bulk_folders) if bulk_folders.isdigit() else 0
        parts = []
        if moved_files:
            parts.append(f"{moved_files} image{'s' if moved_files != 1 else ''}")
        if moved_folders:
            parts.append(f"{moved_folders} folder{'s' if moved_folders != 1 else ''}")
        summary = " and ".join(parts) if parts else "selected items"
        bulk_feedback = {
            "kind": "success",
            "message": f"Moved {summary} to trash. Selection cleared.",
        }
    elif bulk_state == "noop":
        bulk_feedback = {
            "kind": "info",
            "message": "No selected items were moved to trash.",
        }

    return render_template("index.html",
        items=items,
        breadcrumb=breadcrumb,
        parent_url=parent_url,
        stats=stats,
        current_subpath=safe_subpath,
        upload_dir_rel=upload_dir_rel,
        nav_crumbs=nav_crumbs,
        search_query=search_query,
        category_filter_active=bool(search_query and not safe_subpath),
        bulk_feedback=bulk_feedback,
        scan_truncated=scan_truncated,
        scan_limit=scan_limit,
        app_version=APP_VERSION,
        csrf=csrf_token(),
        theme_color=PWA_THEME_COLOR,
    )


def _serve_video_with_range(source_path: Path):
    """Serve a video file with HTTP Range-request support for seeking/scrubbing.

    Returns 206 Partial Content when a Range header is present, otherwise 200 OK
    with the full file.
    """
    file_size = source_path.stat().st_size
    range_header = request.headers.get("Range")

    if not range_header:
        # No Range header — serve the entire file
        return send_file(str(source_path))

    # Parse Range header (expect format: "bytes=start-end" or "bytes=start-")
    match = re.match(r"bytes=(\d+)-(\d*)", range_header)
    if not match:
        # Invalid Range header — serve full file without Range processing
        return send_file(str(source_path), conditional=False)

    start = int(match.group(1))
    end_str = match.group(2)
    end = int(end_str) if end_str else file_size - 1

    # Clamp to valid range
    start = max(0, min(start, file_size - 1))
    end = max(start, min(end, file_size - 1))

    length = end - start + 1

    def generate():
        with open(source_path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk_size = min(8192, remaining)
                data = f.read(chunk_size)
                if not data:
                    break
                yield data
                remaining -= len(data)

    mime_type = mimetypes.guess_type(str(source_path))[0] or "application/octet-stream"

    response = make_response(generate())
    response.status_code = 206
    response.headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    response.headers["Content-Length"] = str(length)
    response.headers["Content-Type"] = mime_type
    response.headers["Accept-Ranges"] = "bytes"
    return response


@app.route("/thumb/<path:filename>")
@rate_limit(max_requests=120, window=60)
def thumb(filename: str):
    rel_path = sanitize_rel_path(filename)
    source_path = source_file_path(rel_path)
    if not source_path.exists() or not is_media_file(source_path):
        return "Not found", 404

    ensure_thumbnail_cache_dir()
    cached_name = thumbnail_filename(rel_path, source_path)
    cached_path = THUMBNAIL_CACHE_DIR / cached_name

    # Videos are served directly with Range-request support for seeking/scrubbing.
    if source_path.suffix.lower() in VIDEO_EXTENSIONS:
        return _serve_video_with_range(source_path)

    if not cached_path.exists():
        try:
            generate_thumbnail(source_path, cached_path)
        except (UnidentifiedImageError, OSError):
            return "Not found", 404

    return send_from_directory(str(THUMBNAIL_CACHE_DIR), cached_name)


@app.route("/view/<path:filename>")
def view(filename: str):
    rel_path = sanitize_rel_path(filename)
    if not is_media_file(DATA_FOLDER / rel_path):
        abort(404)
    return send_from_directory(str(DATA_FOLDER), rel_path)


@app.route("/delete/<path:filename>", methods=["POST"])
@require_auth
@rate_limit(max_requests=30, window=60)
def delete(filename: str):
    if not sanitize_path(filename):
        log_security_event("delete", "denied", reason="invalid_filename")
        return {"error": "Invalid filename"}, 400
    if not validate_csrf(request.form.get("csrf_token")):
        log_security_event("delete", "denied", reason="invalid_csrf")
        return {"error": "Invalid CSRF token"}, 403

    rel_path = sanitize_rel_path(filename)
    file_path = source_file_path(rel_path)

    outcome = "not_found"
    if file_path.exists() and file_path.is_file():
        moved = move_to_trash(file_path, DATA_FOLDER)
        if moved:
            remove_thumbnail_cache_for(rel_path)
            outcome = "success"
        else:
            outcome = "error"

    log_security_event("delete", outcome, target=rel_path)
    _invalidate_gallery_scan_cache()

    folder = os.path.dirname(rel_path)
    return redirect(url_for("index", subpath=folder if folder else ""))


@app.route("/bulk-delete", methods=["POST"])
@require_auth
@rate_limit(max_requests=20, window=60)
def bulk_delete():
    if not validate_csrf(request.form.get("csrf_token")):
        log_security_event("bulk_delete", "denied", reason="invalid_csrf")
        return {"error": "Invalid CSRF token"}, 403

    current_subpath = sanitize_rel_path(request.form.get("current_subpath", "")) if request.form.get("current_subpath") else ""
    selected_files = request.form.getlist("filenames")
    selected_folders = request.form.getlist("folders")

    # Guard: cap total item count (issue #199)
    total_items = len(selected_files) + len(selected_folders)
    if total_items > BULK_DELETE_MAX_ITEMS:
        log_security_event("bulk_delete", "rejected", reason="exceeds_max_items", selected_files=len(selected_files), selected_folders=len(selected_folders), max_items=BULK_DELETE_MAX_ITEMS)
        return {"error": f"Too many items. Maximum is {BULK_DELETE_MAX_ITEMS}."}, 422

    # Guard: cap folder count (issue #199)
    if len(selected_folders) > BULK_DELETE_MAX_FOLDERS:
        log_security_event("bulk_delete", "rejected", reason="exceeds_max_folders", selected_folders=len(selected_folders), max_folders=BULK_DELETE_MAX_FOLDERS)
        return {"error": f"Too many folders. Maximum is {BULK_DELETE_MAX_FOLDERS}."}, 422

    # Guard: preflight folder size estimation (issue #199)
    for rel_path in selected_folders:
        if not sanitize_path(rel_path):
            log_security_event("bulk_delete", "denied", reason="invalid_path", stage="preflight", rel_path=rel_path)
            continue
        safe_rel_path = sanitize_rel_path(rel_path)
        folder_path = DATA_FOLDER / safe_rel_path
        if folder_path.exists() and folder_path.is_dir():
            size = dir_size(folder_path)
            if size > BULK_DELETE_FOLDER_SIZE_CAP:
                log_security_event("bulk_delete", "rejected", reason="folder_too_large", rel_path=safe_rel_path, size=size, cap=BULK_DELETE_FOLDER_SIZE_CAP)
                return {"error": f"Folder is too large to delete ({size} bytes exceeds {BULK_DELETE_FOLDER_SIZE_CAP} byte limit)."}, 422

    moved_files = 0
    moved_folders = 0
    purged_thumbnails: list[str] = []

    # Delete selected files
    for rel_path in selected_files:
        if not sanitize_path(rel_path):
            log_security_event("bulk_delete", "denied", reason="invalid_path", stage="delete_files", rel_path=rel_path)
            continue
        safe_rel_path = sanitize_rel_path(rel_path)
        file_path = source_file_path(safe_rel_path)
        if file_path.exists() and file_path.is_file() and move_to_trash(file_path, DATA_FOLDER):
            moved_files += 1
            purged_thumbnails.append(safe_rel_path)

    # Delete selected folders
    for rel_path in selected_folders:
        if not sanitize_path(rel_path):
            log_security_event("bulk_delete", "denied", reason="invalid_path", stage="delete_folders", rel_path=rel_path)
            continue
        safe_rel_path = sanitize_rel_path(rel_path)
        folder_path = DATA_FOLDER / safe_rel_path
        if folder_path.exists() and folder_path.is_dir() and move_to_trash(folder_path, DATA_FOLDER):
            moved_folders += 1
            purged_thumbnails.append(safe_rel_path)

    if purged_thumbnails:
        batch_remove_thumbnails(purged_thumbnails)

    outcome = "success" if (moved_files or moved_folders) else "noop"
    log_security_event(
        "bulk_delete",
        outcome,
        selected_files=len(selected_files),
        selected_folders=len(selected_folders),
        moved_files=moved_files,
        moved_folders=moved_folders,
        current_subpath=current_subpath,
    )
    _invalidate_gallery_scan_cache()

    redirect_kwargs = {"subpath": current_subpath}
    if moved_files or moved_folders:
        redirect_kwargs.update(
            bulk_state="success",
            bulk_deleted=str(moved_files),
            bulk_folders=str(moved_folders),
        )
    else:
        redirect_kwargs.update(bulk_state="noop")

    return redirect(url_for("index", **redirect_kwargs))


def _is_valid_upload_content(file, suffix: str) -> bool:
    """Validate image contents or supported video container signatures."""
    stream = file.stream
    try:
        if suffix in IMAGE_EXTENSIONS + (".gif",):
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(stream) as image:
                    image.verify()
            return True
        header = stream.read(4096)
        if suffix in (".mp4", ".mov"):
            return len(header) >= 16 and header[4:8] == b"ftyp" and b"moov" in header[8:]
        return len(header) >= 8 and header.startswith(b"\x1a\x45\xdf\xa3") and b"webm" in header.lower()
    except (OSError, SyntaxError, UnidentifiedImageError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return False
    finally:
        stream.seek(0)


def _new_upload_destination(safe_name: str) -> tuple[Path, int] | None:
    """Atomically open a new regular destination under UPLOAD_DIR."""
    stem, suffix = os.path.splitext(safe_name)
    for _ in range(10):
        candidate = UPLOAD_DIR / safe_name
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            safe_name = f"{stem}-{secrets.token_hex(4)}{suffix}"
            continue
        except OSError:
            return None
        return candidate, fd
    return None


@app.route("/upload", methods=["POST"])
@require_auth
@rate_limit(max_requests=10, window=60)
def upload():
    """Upload validated media for sharing (issue #485)."""
    if not validate_csrf(request.form.get("csrf_token")):
        log_security_event("upload", "denied", reason="invalid_csrf")
        return {"error": "Invalid CSRF token"}, 403

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stored = []
    skipped = []
    for file in request.files.getlist("files"):
        raw_name = file.filename
        safe_name = secure_filename(raw_name or "")
        suffix = Path(safe_name).suffix.lower()
        if not safe_name or suffix not in IMAGE_EXTENSIONS + VIDEO_EXTENSIONS:
            log_security_event("upload", "denied", reason="unsupported_type", filename=raw_name)
            skipped.append(raw_name)
            continue
        if not _is_valid_upload_content(file, suffix):
            log_security_event("upload", "denied", reason="invalid_content", filename=raw_name)
            skipped.append(raw_name)
            continue
        destination = _new_upload_destination(safe_name)
        if destination is None:
            log_security_event("upload", "denied", reason="unsafe_destination", filename=raw_name)
            skipped.append(raw_name)
            continue
        dest, fd = destination
        try:
            with os.fdopen(fd, "wb") as output:
                while chunk := file.stream.read(64 * 1024):
                    output.write(chunk)
        except OSError:
            dest.unlink(missing_ok=True)
            skipped.append(raw_name)
            continue
        stored.append(dest.name)

    outcome = "success" if stored else ("skipped" if skipped else "noop")
    log_security_event("upload", outcome, stored=len(stored), skipped=len(skipped))
    if not stored:
        return {"error": "No files were uploaded. Send valid media files in the 'files' field."}, 422

    _invalidate_gallery_scan_cache()
    dest_subpath = UPLOAD_DIR.relative_to(DATA_FOLDER.resolve()).as_posix()
    return redirect(url_for("index", subpath=dest_subpath))


@app.route("/tag", methods=["POST"])
@require_auth
@rate_limit(max_requests=20, window=60)
def add_tag():
    if not validate_csrf(request.form.get("csrf_token")):
        log_security_event("add_tag", "denied", reason="invalid_csrf")
        return {"error": "Invalid CSRF token"}, 403

    rel_path = sanitize_rel_path(request.form.get("rel_path", ""))

    # Accept multiple tags via `tags[]` form field for batch inserts (Fixes #330).
    # Falls back to single `tag` field for backward compatibility.
    tags = [t.strip() for t in request.form.getlist("tags[]") if t.strip()]
    if not tags:
        tag = request.form.get("tag", "").strip()
        if tag:
            tags = [tag]

    if not rel_path or not tags:
        return {"error": "Missing rel_path or tag"}, 400

    _tag_store().add_tags(rel_path, tags)
    log_security_event(
        "add_tag",
        "success",
        target=rel_path,
        tag=",".join(tags),
    )
    _invalidate_gallery_scan_cache()

    return {"status": "ok"}

@app.route("/recent")
@require_auth
@rate_limit(max_requests=30, window=60)
def recent_view():
    """Show recently added images sorted by modification time (newest first)."""
    max_items = 50
    entries, scan_truncated = recent_gallery_entries(max_items)

    images = []
    for rel_path_str, mtime, size in entries:
        date_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))

        # Get folder path for navigation
        folder_path = os.path.dirname(rel_path_str)
        folder_url = url_for("index", subpath=folder_path) if folder_path else url_for("index")

        images.append({
            "name": os.path.basename(rel_path_str),
            "rel_path": rel_path_str,
            "url": url_for("view", filename=rel_path_str),
            "thumb": url_for("thumb", filename=rel_path_str),
            "added": date_str,
            "size": format_size(size),
            "folder_url": folder_url,
        })

    # Detect scan truncation (Issue #349). recent_gallery_entries reports True
    # only when the walk stopped at RECENT_ENUMERATION_LIMIT (issue #436) with
    # media left unseen, so libraries larger than GALLERY_SCAN_LIMIT no longer
    # raise a false warning.
    return render_template(
        "recent.html",
        items=images,
        scan_truncated=scan_truncated,
        scan_limit=RECENT_ENUMERATION_LIMIT,
        theme_color=PWA_THEME_COLOR,
    )


@app.route("/trash")
@require_auth
@rate_limit(max_requests=30, window=60)
def trash_view():
    items = list_trash(DATA_FOLDER)
    return render_template("trash.html", items=items, csrf=csrf_token(), theme_color=PWA_THEME_COLOR)


@app.route("/trash/restore/<path:item_name>", methods=["POST"])
@require_auth
@rate_limit(max_requests=20, window=60)
def trash_restore(item_name: str):
    if not validate_csrf(request.form.get("csrf_token")):
        log_security_event("trash_restore", "denied", reason="invalid_csrf")
        return {"error": "Invalid CSRF token"}, 403

    try:
        restored = restore_from_trash(item_name, DATA_FOLDER)
    except ValueError as exc:
        log_security_event("trash_restore", "denied", reason=str(exc), item=item_name)
        flash(str(exc), "error")
        return redirect(url_for("trash_view"))

    log_security_event("trash_restore", "success" if restored else "not_found", item=item_name)
    _invalidate_gallery_scan_cache()
    return redirect(url_for("trash_view"))


@app.route("/trash/empty", methods=["POST"])
@require_auth
@rate_limit(max_requests=5, window=60)
def trash_empty():
    if not validate_csrf(request.form.get("csrf_token")):
        log_security_event("trash_empty", "denied", reason="invalid_csrf")
        return {"error": "Invalid CSRF token"}, 403

    deleted = empty_trash(DATA_FOLDER)
    log_security_event("trash_empty", "success", deleted=deleted)
    _invalidate_gallery_scan_cache()
    return redirect(url_for("trash_view"))


@app.route("/trash/purge", methods=["POST"])
@require_auth
@rate_limit(max_requests=5, window=60)
def trash_purge():
    if not validate_csrf(request.form.get("csrf_token")):
        return {"error": "Invalid CSRF token"}, 403
    days = request.form.get("days", "30")
    try:
        retention_days = max(1, min(3650, int(days)))
    except ValueError:
        log.warning(
            "Invalid 'days' value '%s' submitted to trash purge; using default of 30 days",
            days,
        )
        flash("Invalid retention period provided; using default of 30 days.", "warning")
        retention_days = 30
    purge_old_trash(DATA_FOLDER, retention_days)
    _invalidate_gallery_scan_cache()
    return redirect(url_for("trash_view"))


@app.route("/login")
def login():
    raw_next = request.args.get("next") or "/"
    next_url = raw_next if is_safe_redirect_url(raw_next) else "/"

    error_code = (request.args.get("error") or "").strip().lower()
    error_map = {
        "invalid": "Invalid password. Please try again.",
        "oidc_failed": "OIDC login failed. Please try again.",
        "oidc_denied": "Your account does not meet the OIDC authorization requirements.",
        "oidc_disabled": "OIDC is not configured.",
        "local_disabled": "Password login is disabled.",
    }
    error = error_map.get(error_code)

    return render_template("login.html",
        csrf=csrf_token(),
        oidc_enabled=is_oidc_configured(),
        local_enabled=bool(os.environ.get("ADMIN_PASSWORD")),
        oidc_label=get_oidc_label(),
        next_url=next_url,
        error=error,
        theme_color=PWA_THEME_COLOR,
    )


@app.route("/auth", methods=["POST"])
@rate_limit(max_requests=5, window=300)
def auth():
    if not validate_csrf(request.form.get("csrf_token")):
        return {"error": "Invalid CSRF token"}, 403

    raw_next = request.form.get("next") or request.args.get("next") or "/"
    next_url = raw_next if is_safe_redirect_url(raw_next) else url_for("index")

    if resolved_auth_mode() != "local":
        return redirect(url_for("login", error="local_disabled", next=next_url))

    password = request.form.get("password", "")
    if verify_local_password(password):
        session.permanent = True
        session["authenticated"] = True
        session["auth_method"] = "local"
        log_security_event("login", "success", auth_method="local")
        # The landing page posts { type: "auth_changed" } to the active
        # service worker, which purges all caches (Issue #453).
        return redirect(next_url)

    log_security_event("login", "failure", auth_method="local", reason="invalid_password")
    return redirect(url_for("login", error="invalid", next=next_url))


@app.route("/logout")
def logout():
    log_security_event("logout", "success")
    session.clear()
    # The /login page posts { type: "auth_changed" } to the active
    # service worker, which purges all caches so a subsequent user
    # cannot receive this user's cached content (Issue #453).
    return redirect(url_for("login", next="/"))


@app.route("/maintenance/thumbnails/regenerate", methods=["POST"])
@require_auth
@rate_limit(max_requests=5, window=60)
def maintenance_thumbnails_regenerate():
    if not validate_csrf(request.form.get("csrf_token")):
        log_security_event("thumb_maintenance", "denied", reason="invalid_csrf")
        return {"error": "Invalid CSRF token"}, 403

    try:
        limit = max(1, min(5000, int(request.args.get("limit", "0") or "0"))) if request.args.get("limit") else None
    except (ValueError, TypeError):
        limit = None
    stats = run_thumbnail_integrity_check(limit=limit)
    log_security_event(
        "thumb_maintenance",
        "success",
        checked=stats["checked"],
        regenerated=stats["regenerated"],
        failed=stats["failed"],
    )

    return redirect(
        url_for(
            "settings_view",
            thumb_checked=stats["checked"],
            thumb_regenerated=stats["regenerated"],
            thumb_failed=stats["failed"],
        )
    )


@app.route("/api/webhook/run", methods=["POST"])
@rate_limit(max_requests=20, window=60)
def webhook_run_task():
    # Fail closed: require WEBHOOK_SECRET when webhooks are enabled to prevent
    # unauthenticated command execution (especially when AUTH_TYPE=none).
    if not _webhook_enabled():
        return jsonify({"error": "Webhook execution is disabled"}), 403

    if not WEBHOOK_SECRET:
        log.warning(
            "Blocked webhook task execution: WEBHOOK_ENABLED=true but "
            "WEBHOOK_SECRET is not configured."
        )
        return jsonify(
            {
                "error": (
                    "Webhook execution unavailable: WEBHOOK_SECRET is not "
                    "configured. Set WEBHOOK_SECRET to enable webhook tasks."
                )
            }
        ), 503

    # Validate bearer token. The webhook is a documented bearer-secret-authenticated
    # machine API; the bearer secret is the request's own authentication. CSRF is
    # only meaningful for browser-session flows (where the session cookie is
    # implicit) and would block legitimate secret-holding machine clients.
    auth_header = request.headers.get("Authorization", "")
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not _verify_webhook_secret(token):
        return jsonify({"error": "Webhook secret required"}), 401

    body, status = run_configured_task(request.get_json(silent=True) or {})
    _invalidate_gallery_scan_cache()
    return body, status


# The /api/llm/* REST endpoints live in llm_api.py (issue #519); register them
# here so their view functions share this module's live globals via llm_api._app().
register_llm_routes(app, log_security_event)


@app.route("/api/image-metadata/<path:relpath>")
@require_auth
@rate_limit(max_requests=120, window=60)
def image_metadata(relpath: str):
    """Return embedded PNG generation metadata for a single gallery image.

    Read lazily per image (never during enumeration) so the details panel can
    fill itself in when opened. Guard sequence mirrors llm_image().
    """
    if not sanitize_path(relpath):
        return {"error": "Invalid path"}, 400
    media_path = source_file_path(relpath)
    # Reject symlinks under DATA_FOLDER that resolve outside it, for the same
    # reason as llm_image(): a read-scoped caller must not be able to read the
    # text chunks of an arbitrary filesystem target.
    if media_path.is_symlink():
        try:
            media_path.resolve().relative_to(DATA_FOLDER.resolve())
        except ValueError:
            return {"error": "Image not found"}, 404
    if not media_path.exists() or not is_media_file(media_path) or is_excluded_gallery_path(media_path):
        return {"error": "Image not found"}, 404
    return {"metadata": read_image_metadata(media_path)}


@app.route("/settings")
@require_auth
def settings_view():
    maintenance_result = None
    checked = request.args.get("thumb_checked")
    regenerated = request.args.get("thumb_regenerated")
    failed = request.args.get("thumb_failed")
    if checked is not None and regenerated is not None and failed is not None:
        try:
            maintenance_result = {
                "checked": int(checked),
                "regenerated": int(regenerated),
                "failed": int(failed),
            }
        except ValueError:
            maintenance_result = None

    return render_template("settings.html",
        theme_color=PWA_THEME_COLOR,
        data_folder=str(DATA_FOLDER),
        thumb_cache=str(THUMBNAIL_CACHE_DIR),
        maintenance_result=maintenance_result,
        csrf=csrf_token(),
    )


@app.route("/about")
@require_auth
def about_view():
    return render_template("about.html",
        theme_color=PWA_THEME_COLOR,
        app_version=APP_VERSION,
        auth_enabled=is_auth_enabled(),
        auth_mode=resolved_auth_mode(),
        oidc_configured=is_oidc_configured(),
    )


# OIDC login/callback/refresh routes live in oidc.py (issue #520); the app's
# audit logger is injected so oidc.py never imports this module.
register_oidc_routes(app, log_security_event)


app.add_url_rule("/health", "health", health, methods=["GET"])
app.add_url_rule("/health/storage", "storage_health", storage_health, methods=["GET"])
app.add_url_rule("/health/storage/read", "storage_health_read", storage_health_read, methods=["GET"])
app.add_url_rule("/health/storage/write", "storage_health_write", storage_health_write, methods=["GET"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
