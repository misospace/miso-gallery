from __future__ import annotations

import contextlib
import hashlib
import html
import json
import os
import secrets
import shlex
import subprocess
import time
from pathlib import Path

from flask import (
    Flask,
    abort,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from PIL import Image, UnidentifiedImageError

from auth import (
    configure_oauth,
    get_oidc_label,
    is_auth_enabled,
    is_oidc_configured,
    oauth,
    require_api_key_with_scope,
    require_auth,
    resolved_auth_mode,
    verify_local_password,
    verify_oidc_authorization,
)
from health import health, storage_health, storage_health_read, storage_health_write
from security import (
    add_security_headers,
    csrf_token,
    is_safe_redirect_url,
    rate_limit,
    sanitize_path,
    validate_csrf,
)
from trash import (
    dir_size,
    empty_trash,
    list_trash,
    move_to_trash,
    purge_old_trash,
    restore_from_trash,
)

# Load service worker from external file (extracted from app.py)
SERVICE_WORKER_PATH = os.path.join(os.path.dirname(__file__), "templates", "service-worker.js")
with open(SERVICE_WORKER_PATH, "r") as _f:
    SERVICE_WORKER_TEMPLATE = _f.read()


DATA_FOLDER = Path(os.environ.get("DATA_FOLDER", "/data"))
THUMBNAIL_CACHE_DIR = DATA_FOLDER / ".thumb_cache"


def resolve_secret_key() -> str:
    configured = os.environ.get("SECRET_KEY", "").strip()
    if not configured:
        raise ValueError(
            "SECRET_KEY environment variable is required. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    return configured


app = Flask(__name__)
app.secret_key = resolve_secret_key()

# Session persistence configuration for mobile compatibility
# PERMANENT_SESSION_LIFETIME: 30 days for persistent sessions
# SESSION_COOKIE_SECURE: Only send over HTTPS (set via env for flexibility)
# SESSION_COOKIE_SAMESITE: Lax to allow cross-site navigation while maintaining security
# SESSION_COOKIE_HTTPONLY: Prevent JavaScript access to session cookie
app.config["PERMANENT_SESSION_LIFETIME"] = int(os.environ.get("SESSION_LIFETIME_DAYS", 30)) * 24 * 60 * 60
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "true").strip().lower() == "true"
app.config["SESSION_COOKIE_SAMESITE"] = os.environ.get("SESSION_COOKIE_SAMESITE", "Lax")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_REFRESH_EACH_REQUEST"] = False

app.after_request(add_security_headers)


def _client_ip() -> str | None:
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return forwarded or request.remote_addr


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
THUMBNAIL_MAX_SIZE = 400
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
VIDEO_EXTENSIONS = (".gif", ".mp4", ".webm", ".mov")
FAVICON_URL = os.environ.get("FAVICON_URL", "").strip()

PWA_THEME_COLOR = "#0d0d0d"
PWA_APP_NAME = "Miso Gallery"
APP_VERSION = (os.environ.get("APP_VERSION") or "0.1.17").strip() or "0.1.17"
WEBHOOK_TASK_PREFIX = "WEBHOOK_TASK_"
AUTO_FOLDER_COVERS_ENABLED = os.environ.get("GALLERY_AUTO_FOLDER_COVERS", "false").strip().lower() in {"1", "true", "yes", "on"}
FOLDER_COVER_CACHE_TTL = max(int(os.environ.get("GALLERY_COVER_CACHE_TTL", "3600") or 3600), 0)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
_FOLDER_COVER_CACHE: dict[str, tuple[float, str | None]] = {}

# Bounded pagination defaults for gallery endpoints
GALLERY_PAGE_DEFAULT = 50
GALLERY_PAGE_MAX = 500
GALLERY_SCAN_LIMIT = int(os.environ.get("GALLERY_SCAN_LIMIT", "5000"))

# Destructive-operation guardrails (issue #199)
BULK_DELETE_MAX_ITEMS = int(os.environ.get("BULK_DELETE_MAX_ITEMS", "200"))
BULK_DELETE_MAX_FOLDERS = int(os.environ.get("BULK_DELETE_MAX_FOLDERS", "50"))
BULK_DELETE_FOLDER_SIZE_CAP = int(os.environ.get("BULK_DELETE_FOLDER_SIZE_CAP", str(10 * 1024 * 1024 * 1024)))  # 10 GB default
LLM_BULK_DELETE_MAX_ITEMS = int(os.environ.get("LLM_BULK_DELETE_MAX_ITEMS", "500"))
LLM_DEDUP_MAX_REMOVALS = int(os.environ.get("LLM_DEDUP_MAX_REMOVALS", "200"))


def _paginate(items, page=1, per_page=GALLERY_PAGE_DEFAULT):
    """Return (paginated_items, total_count, page, per_page, has_more)."""
    total = len(items)
    start = (page - 1) * per_page
    end = start + per_page
    paginated = items[start:end]
    has_more = end < total
    return paginated, total, page, per_page, has_more


def _parse_pagination(args):
    """Parse pagination query params from request.args. Returns (page, per_page)."""
    try:
        page = max(1, int(args.get("page", "1")))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = max(1, min(GALLERY_PAGE_MAX, int(args.get("limit", str(GALLERY_PAGE_DEFAULT)))))
    except (ValueError, TypeError):
        per_page = GALLERY_PAGE_DEFAULT
    return page, per_page


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


def _render_task_command(template: str, params: dict[str, object]) -> str:
    rendered = template
    for key, value in params.items():
        if isinstance(value, (dict, list)):
            raise ValueError(f"params.{key} must be a scalar value")
        rendered = rendered.replace(f"{{params.{key}}}", shlex.quote(str(value)))

    if "{params." in rendered:
        raise ValueError("missing required params for command template")

    return rendered




def ensure_thumbnail_cache_dir() -> None:
    THUMBNAIL_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def sanitize_rel_path(rel_path: str) -> str:
    normalized = os.path.normpath(rel_path).replace("\\", "/").lstrip("/")
    if normalized.startswith(".."):
        abort(404)
    return normalized


def source_file_path(rel_path: str) -> Path:
    return DATA_FOLDER / sanitize_rel_path(rel_path)


def thumbnail_filename(rel_path: str, source_path: Path) -> str:
    stat = source_path.stat()
    safe_name = rel_path.replace("/", "__")
    return f"{safe_name}.{stat.st_mtime_ns}.{stat.st_size}.jpg"


def generate_thumbnail(source_path: Path, output_path: Path) -> None:
    with Image.open(source_path) as img:
        img = img.convert("RGB")
        img.thumbnail((THUMBNAIL_MAX_SIZE, THUMBNAIL_MAX_SIZE), Image.Resampling.LANCZOS)
        img.save(output_path, format="JPEG", quality=85, optimize=True)


def remove_thumbnail_cache_for(rel_path: str) -> None:
    ensure_thumbnail_cache_dir()
    safe_name = sanitize_rel_path(rel_path).replace("/", "__")
    for cached_file in THUMBNAIL_CACHE_DIR.iterdir():
        if cached_file.name.startswith(f"{safe_name}."):
            with contextlib.suppress(OSError):
                cached_file.unlink()


def run_thumbnail_integrity_check(limit: int | None = None) -> dict[str, int]:
    """Check thumbnails and regenerate missing/invalid entries on demand.

    Args:
        limit: Maximum files to scan. None means no limit.
    """

    ensure_thumbnail_cache_dir()
    excluded_dirs = {THUMBNAIL_CACHE_DIR.name, ".trash"}
    stats = {"checked": 0, "regenerated": 0, "failed": 0}

    for item in DATA_FOLDER.rglob("*"):
        if not item.is_file() or item.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if item.name.startswith("."):
            continue

        rel_path = item.relative_to(DATA_FOLDER)
        if any(part in excluded_dirs or part.startswith(".") for part in rel_path.parts):
            continue

        rel_posix = rel_path.as_posix()
        stats["checked"] += 1
        if limit is not None and stats["checked"] > limit:
            break

        cached_name = thumbnail_filename(rel_posix, item)
        cached_path = THUMBNAIL_CACHE_DIR / cached_name

        needs_regen = not cached_path.exists()
        if not needs_regen:
            try:
                with Image.open(cached_path) as thumb_img:
                    thumb_img.verify()
            except (UnidentifiedImageError, OSError):
                needs_regen = True

        if not needs_regen:
            continue

        try:
            generate_thumbnail(item, cached_path)
            stats["regenerated"] += 1
        except (UnidentifiedImageError, OSError):
            stats["failed"] += 1

    return stats


def folder_cover_rel_path(folder_rel_path: str) -> str | None:
    """Return a cached auto-cover image rel path for a folder, if available."""

    if not AUTO_FOLDER_COVERS_ENABLED:
        return None

    now = time.time()
    cached = _FOLDER_COVER_CACHE.get(folder_rel_path)
    if cached and now - cached[0] < FOLDER_COVER_CACHE_TTL:
        cached_rel = cached[1]
        if cached_rel:
            cached_path = DATA_FOLDER / sanitize_rel_path(cached_rel)
            if cached_path.exists() and cached_path.is_file() and cached_path.suffix.lower() in IMAGE_EXTENSIONS:
                return cached_rel
        else:
            cached = None

    folder_path = DATA_FOLDER / sanitize_rel_path(folder_rel_path) if folder_rel_path else DATA_FOLDER
    if not folder_path.exists() or not folder_path.is_dir():
        _FOLDER_COVER_CACHE[folder_rel_path] = (now, None)
        return None

    cover_rel: str | None = None
    # Bound the scan to prevent unbounded subtree traversal on cache misses.
    candidates = []
    for count, item in enumerate(folder_path.rglob("*")):
        if count >= GALLERY_SCAN_LIMIT:
            break
        candidates.append(item)
    candidates.sort(key=lambda p: p.as_posix().lower())
    for candidate in candidates:
        if not candidate.is_file() or candidate.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        rel_candidate = candidate.relative_to(DATA_FOLDER)
        if any(part in {".thumb_cache", ".trash"} or part.startswith(".") for part in rel_candidate.parts):
            continue
        cover_rel = rel_candidate.as_posix()
        break

    if cover_rel is None:
        _FOLDER_COVER_CACHE.pop(folder_rel_path, None)
    else:
        _FOLDER_COVER_CACHE[folder_rel_path] = (now, cover_rel)
    return cover_rel


def format_size(size: int) -> str:
    value = float(size)
    for unit in ["B", "KB", "MB", "GB"]:
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def is_media_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS + VIDEO_EXTENSIONS


def is_excluded_gallery_path(path: Path) -> bool:
    try:
        rel_parts = path.relative_to(DATA_FOLDER).parts
    except ValueError:
        return True
    return any(part in {THUMBNAIL_CACHE_DIR.name, ".trash"} or part.startswith(".") for part in rel_parts)


def media_metadata(path: Path) -> dict[str, object]:
    rel_path = path.relative_to(DATA_FOLDER).as_posix()
    stat = path.stat()
    media_type = "video" if path.suffix.lower() in VIDEO_EXTENSIONS else "image"
    return {
        "name": path.name,
        "rel_path": rel_path,
        "media_type": media_type,
        "size": stat.st_size,
        "size_human": format_size(stat.st_size),
        "modified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
        "mtime": stat.st_mtime,
        "view_url": url_for("view", filename=rel_path),
        "thumb_url": url_for("thumb", filename=rel_path),
    }


def iter_gallery_media(limit: int | None = None) -> list[Path]:
    """Iterate gallery media files, bounded by scan limit."""
    effective_limit = limit if limit is not None else GALLERY_SCAN_LIMIT
    media: list[Path] = []
    for item in DATA_FOLDER.rglob("*"):
        if len(media) >= effective_limit:
            break
        try:
            if is_media_file(item) and not is_excluded_gallery_path(item):
                media.append(item)
        except (OSError, PermissionError):
            continue
    return sorted(media, key=lambda p: p.relative_to(DATA_FOLDER).as_posix().lower())


def iter_gallery_folders(limit: int | None = None) -> list[Path]:
    """Iterate gallery folders, bounded by scan limit."""
    effective_limit = limit if limit is not None else GALLERY_SCAN_LIMIT
    folders: list[Path] = []
    for item in DATA_FOLDER.rglob("*"):
        if len(folders) >= effective_limit:
            break
        try:
            if item.is_dir() and not is_excluded_gallery_path(item):
                folders.append(item)
        except (OSError, PermissionError):
            continue
    return sorted(folders, key=lambda p: p.relative_to(DATA_FOLDER).as_posix().lower())


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_duplicate_media() -> list[dict[str, object]]:
    by_size: dict[int, list[Path]] = {}
    for item in iter_gallery_media():
        try:
            by_size.setdefault(item.stat().st_size, []).append(item)
        except OSError:
            continue

    groups: list[dict[str, object]] = []
    for same_size in by_size.values():
        if len(same_size) < 2:
            continue
        by_hash: dict[str, list[Path]] = {}
        for item in same_size:
            try:
                by_hash.setdefault(file_sha256(item), []).append(item)
            except OSError:
                continue
        for digest, matches in by_hash.items():
            if len(matches) < 2:
                continue
            matches = sorted(matches, key=lambda p: p.relative_to(DATA_FOLDER).as_posix().lower())
            groups.append(
                {
                    "hash": digest,
                    "size": matches[0].stat().st_size,
                    "keep": matches[0].relative_to(DATA_FOLDER).as_posix(),
                    "duplicates": [p.relative_to(DATA_FOLDER).as_posix() for p in matches[1:]],
                    "all": [p.relative_to(DATA_FOLDER).as_posix() for p in matches],
                }
            )
    return groups


def run_configured_task(payload: dict[str, object]) -> tuple[dict[str, object], int]:
    if not _webhook_enabled():
        return {"error": "Webhook tasks are disabled"}, 404

    task = str(payload.get("task", "")).strip()
    params = payload.get("params") or {}

    if not task:
        return {"error": "task is required"}, 400
    if not isinstance(params, dict):
        return {"error": "params must be an object"}, 400

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


@app.before_request
def check_auth():
    if not is_auth_enabled():
        return None

    if request.path.startswith("/api/llm/"):
        return None

    if request.path.startswith("/images/") or request.path.startswith("/view/"):
        return None

    if (
        request.path.startswith("/thumb/")
        or request.path.startswith("/assets/")
        or request.path in {"/favicon.ico", "/manifest.webmanifest", "/service-worker.js"}
    ):
        return None

    if request.path in ["/login", "/auth", "/logout", "/auth/oidc", "/auth/oidc/callback"]:
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
    return send_from_directory(str(DATA_FOLDER), rel_path)


@app.route("/")
@app.route("/<path:subpath>")
@require_auth
def index(subpath: str = ""):
    import time

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

    for item in sorted(folder_path.iterdir(), key=lambda p: p.name.lower()):
        if item.name in {".thumb_cache", ".trash"}:
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

    # Apply category search filter (root only).
    # Issue #51 expects folder/category name substring matching.
    if search_query and not safe_subpath:
        items = [i for i in items if i.get("is_dir") and search_query in i["name"].lower()]
        stats["folders"] = sum(1 for i in items if i.get("is_dir"))
        stats["images"] = sum(1 for i in items if not i.get("is_dir"))
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
        nav_crumbs=nav_crumbs,
        search_query=search_query,
        category_filter_active=bool(search_query and not safe_subpath),
        bulk_feedback=bulk_feedback,
        app_version=APP_VERSION,
        csrf=csrf_token(),
        theme_color=PWA_THEME_COLOR,
    )


@app.route("/thumb/<path:filename>")
@rate_limit(max_requests=120, window=60)
def thumb(filename: str):
    rel_path = sanitize_rel_path(filename)
    source_path = source_file_path(rel_path)
    if not source_path.exists() or (source_path.suffix.lower() not in IMAGE_EXTENSIONS and source_path.suffix.lower() not in VIDEO_EXTENSIONS):
        return "Not found", 404

    ensure_thumbnail_cache_dir()
    cached_name = thumbnail_filename(rel_path, source_path)
    cached_path = THUMBNAIL_CACHE_DIR / cached_name

    if source_path.suffix.lower() in VIDEO_EXTENSIONS:
        return send_from_directory(str(DATA_FOLDER), rel_path)

    if not cached_path.exists():
        try:
            generate_thumbnail(source_path, cached_path)
        except (UnidentifiedImageError, OSError):
            return "Not found", 404

    return send_from_directory(str(THUMBNAIL_CACHE_DIR), cached_name)


@app.route("/view/<path:filename>")
def view(filename: str):
    rel_path = sanitize_rel_path(filename)
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
            # sanitize_path() rejects paths containing ".." or starting with "/" (security.py:sanitize_path)
            # sanitize_rel_path() further normalizes and double-checks (app.py:sanitize_rel_path)


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

    # Delete selected files
    for rel_path in selected_files:
        if not sanitize_path(rel_path):
            continue
        safe_rel_path = sanitize_rel_path(rel_path)
        file_path = source_file_path(safe_rel_path)
        if file_path.exists() and file_path.is_file() and move_to_trash(file_path, DATA_FOLDER):
            moved_files += 1
            remove_thumbnail_cache_for(safe_rel_path)

    # Delete selected folders
    for rel_path in selected_folders:
        if not sanitize_path(rel_path):
            continue
        safe_rel_path = sanitize_rel_path(rel_path)
        folder_path = DATA_FOLDER / safe_rel_path
        if folder_path.exists() and folder_path.is_dir() and move_to_trash(folder_path, DATA_FOLDER):
            moved_folders += 1
            remove_thumbnail_cache_for(safe_rel_path)

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


@app.route("/tag", methods=["POST"])
@require_auth
@rate_limit(max_requests=20, window=60)
def add_tag():
    if not validate_csrf(request.form.get("csrf_token")):
        log_security_event("add_tag", "denied", reason="invalid_csrf")
        return {"error": "Invalid CSRF token"}, 403

    rel_path = sanitize_rel_path(request.form.get("rel_path", ""))
    tag = request.form.get("tag", "").strip()

    if not rel_path or not tag:
        return {"error": "Missing rel_path or tag"}, 400

    # For now, just log the tag assignment (no backend storage yet)
    # This is a UI-first partial fix for #91
    log_security_event(
        "add_tag",
        "success",
        target=rel_path,
        tag=tag,
    )

    return {"status": "ok"}

@app.route("/recent")
@require_auth
@rate_limit(max_requests=30, window=60)
def recent_view():
    """Show recently added images sorted by modification time (newest first)."""
    import time

    max_items = 50
    images = []
    excluded_dirs = {THUMBNAIL_CACHE_DIR.name, ".trash"}

    def is_image(path: Path) -> bool:
        return path.suffix.lower() in IMAGE_EXTENSIONS

    def is_excluded_from_recent(path: Path) -> bool:
        rel_parts = path.relative_to(DATA_FOLDER).parts
        return any(part in excluded_dirs for part in rel_parts)

    scan_count = 0
    for item in DATA_FOLDER.rglob("*"):
        # Bound the scan to prevent blocking on large galleries/NFS.
        scan_count += 1
        if scan_count > GALLERY_SCAN_LIMIT:
            break
        try:
            if not item.is_file() or not is_image(item):
                continue
            if is_excluded_from_recent(item):
                continue
            if item.name.startswith("."):
                continue

            try:
                mtime = item.stat().st_mtime
                size = item.stat().st_size
            except (OSError, PermissionError):
                # Skip items that become inaccessible during NFS attribute cache inconsistency
                continue

            try:
                rel_path = item.relative_to(DATA_FOLDER).as_posix()
            except (OSError, ValueError):
                continue

            date_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))

            # Get folder path for navigation
            folder_path = os.path.dirname(rel_path)
            folder_url = url_for("index", subpath=folder_path) if folder_path else url_for("index")

            images.append({
                "name": item.name,
                "rel_path": rel_path,
                "url": url_for("view", filename=rel_path),
                "thumb": url_for("thumb", filename=rel_path),
                "added": date_str,
                "size": format_size(size),
                "mtime": mtime,
                "folder_url": folder_url,
            })
        except (OSError, PermissionError):
            # Skip items that raise during iteration
            continue

    images.sort(key=lambda x: x["mtime"], reverse=True)
    images = images[:max_items]

    for img in images:
        del img["mtime"]

    return render_template("recent.html", items=images, theme_color=PWA_THEME_COLOR)


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

    restored = restore_from_trash(item_name, DATA_FOLDER)
    log_security_event("trash_restore", "success" if restored else "not_found", item=item_name)
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
        retention_days = 30
    purge_old_trash(DATA_FOLDER, retention_days)
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
        return redirect(next_url)

    log_security_event("login", "failure", auth_method="local", reason="invalid_password")
    return redirect(url_for("login", error="invalid", next=next_url))


@app.route("/logout")
def logout():
    log_security_event("logout", "success")
    session.clear()
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
@require_auth
@rate_limit(max_requests=20, window=60)
def webhook_run_task():
    # Require WEBHOOK_SECRET as Bearer token when configured, regardless of auth mode.
    # This prevents unauthenticated command execution when AUTH_TYPE=none.
    if WEBHOOK_SECRET:
        auth_header = request.headers.get("Authorization", "")
        scheme, _, token = auth_header.partition(" ")
        if scheme.lower() != "bearer" or not _verify_webhook_secret(token):
            return {"error": "Webhook secret required"}, 401

    if is_auth_enabled() and not validate_csrf(request.headers.get("X-CSRF-Token")):
        return {"error": "Invalid CSRF token"}, 403

    body, status = run_configured_task(request.get_json(silent=True) or {})
    return body, status


@app.route("/api/llm/images")
@require_api_key_with_scope("read")
@rate_limit(max_requests=60, window=60)
def llm_images():
    query = request.args.get("q", "").strip().lower()
    page, per_page = _parse_pagination(request.args)
    all_media = iter_gallery_media()
    filtered: list[dict[str, object]] = []
    for item in all_media:
        rel_path = item.relative_to(DATA_FOLDER).as_posix()
        if query and query not in rel_path.lower() and query not in item.name.lower():
            continue
        filtered.append(media_metadata(item))
    paginated, total, pg, pp, has_more = _paginate(filtered, page=page, per_page=per_page)
    # If scan hit the limit, there may be more unscanned items matching the query
    scan_limited = len(all_media) >= GALLERY_SCAN_LIMIT
    if scan_limited and not has_more:
        has_more = True
    return {"images": paginated, "count": len(paginated), "total": total, "page": pg, "per_page": pp, "has_more": has_more}


@app.route("/api/llm/image/<path:relpath>")
@require_api_key_with_scope("read")
@rate_limit(max_requests=60, window=60)
def llm_image(relpath: str):
    if not sanitize_path(relpath):
        return {"error": "Invalid path"}, 400
    media_path = source_file_path(relpath)
    if not media_path.exists() or not is_media_file(media_path) or is_excluded_gallery_path(media_path):
        return {"error": "Image not found"}, 404
    return media_metadata(media_path)


@app.route("/api/llm/recent")
@require_api_key_with_scope("read")
@rate_limit(max_requests=60, window=60)
def llm_recent():
    page, per_page = _parse_pagination(request.args)
    all_media = sorted(iter_gallery_media(), key=lambda p: p.stat().st_mtime, reverse=True)
    paginated, total, pg, pp, has_more = _paginate(all_media, page=page, per_page=per_page)
    # If scan hit the limit, there may be more recent items beyond the scan window
    scan_limited = len(all_media) >= GALLERY_SCAN_LIMIT
    if scan_limited and not has_more:
        has_more = True
    return {"images": [media_metadata(item) for item in paginated], "count": len(paginated), "total": total, "page": pg, "per_page": pp, "has_more": has_more}


@app.route("/api/llm/folders")
@require_api_key_with_scope("read")
@rate_limit(max_requests=60, window=60)
def llm_folders():
    page, per_page = _parse_pagination(request.args)
    all_folders = [{"rel_path": "", "name": "", "parent": None}]
    for folder in iter_gallery_folders():
        rel_path = folder.relative_to(DATA_FOLDER).as_posix()
        parent = folder.parent.relative_to(DATA_FOLDER).as_posix() if folder.parent != DATA_FOLDER else ""
        all_folders.append({"rel_path": rel_path, "name": folder.name, "parent": parent})
    paginated, total, pg, pp, has_more = _paginate(all_folders, page=page, per_page=per_page)
    scan_limited = len(all_folders) - 1 >= GALLERY_SCAN_LIMIT  # -1 for root entry
    if scan_limited and not has_more:
        has_more = True
    return {"folders": paginated, "count": len(paginated), "total": total, "page": pg, "per_page": pp, "has_more": has_more}


@app.route("/api/llm/tags", methods=["POST"])
@require_api_key_with_scope("read")
@rate_limit(max_requests=30, window=60)
def llm_tags():
    payload = request.get_json(silent=True) or {}
    rel_paths = payload.get("rel_paths") or payload.get("images") or payload.get("rel_path")
    tags = payload.get("tags") or payload.get("tag")
    action = str(payload.get("action", "add")).strip().lower()
    if isinstance(rel_paths, str):
        rel_paths = [rel_paths]
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(rel_paths, list) or not isinstance(tags, list) or action not in {"add", "remove"}:
        return {"error": "rel_paths/images, tags, and action=add|remove are required"}, 400
    updated = []
    for rel_path in rel_paths:
        if not isinstance(rel_path, str) or not sanitize_path(rel_path):
            continue
        safe_rel_path = sanitize_rel_path(rel_path)
        media_path = source_file_path(safe_rel_path)
        if media_path.exists() and is_media_file(media_path) and not is_excluded_gallery_path(media_path):
            updated.append(safe_rel_path)
    log_security_event("llm_tags", "success", action=action, updated=len(updated), tags=[str(tag) for tag in tags])
    return {"status": "ok", "action": action, "updated": updated, "tags": [str(tag) for tag in tags]}


@app.route("/api/llm/delete", methods=["POST"])
@require_api_key_with_scope("write")
@rate_limit(max_requests=20, window=60)
def llm_delete():
    payload = request.get_json(silent=True) or {}
    rel_path = str(payload.get("rel_path") or payload.get("image") or "")
    if not rel_path or not sanitize_path(rel_path):
        return {"error": "Valid rel_path is required"}, 400
    safe_rel_path = sanitize_rel_path(rel_path)
    media_path = source_file_path(safe_rel_path)
    if not media_path.exists() or not is_media_file(media_path) or is_excluded_gallery_path(media_path):
        return {"error": "Image not found"}, 404
    dry_run = bool(payload.get("dry_run", False))
    moved = False
    if not dry_run:
        moved = move_to_trash(media_path, DATA_FOLDER)
        if moved:
            remove_thumbnail_cache_for(safe_rel_path)
    log_security_event("llm_delete", "success" if (moved and not dry_run) else "dry_run", target=safe_rel_path, dry_run=dry_run)
    return {"deleted": moved if not dry_run else True, "rel_path": safe_rel_path, "dry_run": dry_run}, 200


@app.route("/api/llm/bulk-delete", methods=["POST"])
@require_api_key_with_scope("write")
@rate_limit(max_requests=10, window=60)
def llm_bulk_delete():
    payload = request.get_json(silent=True) or {}
    rel_paths = payload.get("rel_paths") or payload.get("images") or []
    if not isinstance(rel_paths, list):
        return {"error": "rel_paths/images must be a list"}, 400
    # Guard: cap item count (issue #199)
    if len(rel_paths) > LLM_BULK_DELETE_MAX_ITEMS:
        log_security_event("llm_bulk_delete", "rejected", reason="exceeds_max_items", count=len(rel_paths), max_items=LLM_BULK_DELETE_MAX_ITEMS)
        return {"error": f"Too many items. Maximum is {LLM_BULK_DELETE_MAX_ITEMS}."}, 422
    dry_run = bool(payload.get("dry_run", False))
    deleted = []
    skipped = []
    for rel_path in rel_paths:
        if not isinstance(rel_path, str) or not sanitize_path(rel_path):
            skipped.append(str(rel_path))
            continue
        safe_rel_path = sanitize_rel_path(rel_path)
        media_path = source_file_path(safe_rel_path)
        if not media_path.exists() or not is_media_file(media_path) or is_excluded_gallery_path(media_path):
            skipped.append(safe_rel_path)
            continue
        if dry_run:
            deleted.append(safe_rel_path)
        elif move_to_trash(media_path, DATA_FOLDER):
            remove_thumbnail_cache_for(safe_rel_path)
            deleted.append(safe_rel_path)
        else:
            skipped.append(safe_rel_path)
    log_security_event("llm_bulk_delete", "success" if (deleted and not dry_run) else "dry_run", deleted=len(deleted), skipped=len(skipped), dry_run=dry_run)
    return {"deleted": deleted, "skipped": skipped, "deleted_count": len(deleted), "skipped_count": len(skipped), "dry_run": dry_run}


@app.route("/api/llm/dedup", methods=["POST"])
@require_api_key_with_scope("write")
@rate_limit(max_requests=5, window=60)
def llm_dedup():
    payload = request.get_json(silent=True) or {}
    remove = bool(payload.get("remove") or payload.get("delete"))
    try:
        limit = max(1, min(100, int(payload.get("limit", "0") or "0"))) if payload.get("limit") else None
    except (ValueError, TypeError):
        limit = None
    groups = find_duplicate_media()
    if limit is not None:
        groups = groups[:limit]
    removed = []
    if remove:
        # Guard: cap total removal count (issue #199)
        estimated_removals = sum(len(g["duplicates"]) for g in groups)
        if estimated_removals > LLM_DEDUP_MAX_REMOVALS:
            log_security_event("llm_dedup", "rejected", reason="exceeds_max_removals", estimated=estimated_removals, max_removals=LLM_DEDUP_MAX_REMOVALS)
            return {"error": f"Too many duplicates to remove. Maximum is {LLM_DEDUP_MAX_REMOVALS}."}, 422
        for group in groups:
            for rel_path in group["duplicates"]:
                media_path = source_file_path(str(rel_path))
                if media_path.exists() and move_to_trash(media_path, DATA_FOLDER):
                    remove_thumbnail_cache_for(str(rel_path))
                    removed.append(str(rel_path))
    log_security_event("llm_dedup", "success", groups=len(groups), removed=len(removed), dry_run=not remove)
    if remove:
        return {"duplicate_groups": groups, "group_count": len(groups), "dry_run": False, "removed": removed, "deleted_count": len(removed)}
    return {"duplicate_groups": groups, "group_count": len(groups), "dry_run": True, "skipped_count": 0, "deleted_count": 0}


@app.route("/api/llm/task/run", methods=["POST"])
@require_api_key_with_scope("write")
@rate_limit(max_requests=10, window=60)
def llm_task_run():
    body, status = run_configured_task(request.get_json(silent=True) or {})
    return body, status


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


@app.route("/auth/oidc")
def oidc_login():
    """Initiate OIDC authentication flow."""
    if not is_oidc_configured():
        return redirect(url_for("login"))

    # Store the return URL in session
    raw_next = request.args.get("next") or url_for("index")
    next_url = raw_next if is_safe_redirect_url(raw_next) else url_for("index")
    session["oidc_next_url"] = next_url

    # Get the callback URL
    callback_url = os.environ.get("OIDC_CALLBACK_URL") or url_for("oidc_callback", _external=True)

    # Redirect to OIDC provider
    return oauth.oidc.authorize_redirect(callback_url)


@app.route("/auth/oidc/callback")
def oidc_callback():
    """Handle OIDC callback."""
    if not is_oidc_configured():
        return redirect(url_for("login"))

    try:
        # Get the access token
        token = oauth.oidc.authorize_access_token()

        # Get user info from the token or userinfo endpoint
        user_info = token.get("userinfo")
        if not user_info:
            # Fetch user info from userinfo endpoint
            resp = oauth.oidc.get("userinfo")
            user_info = resp.json()

        # Extract stable subject ID (always log this for audit trails)
        oidc_sub = user_info.get("sub")

        # Extract user identifier (prefer email, fallback to sub)
        user_id = user_info.get("email") or user_info.get("sub")
        user_name = user_info.get("name") or user_info.get("preferred_username") or user_id

        if not user_id:
            return "Could not identify user from OIDC response", 400

        # Check OIDC authorization (domain/group/claim allowlists)
        allowed, reason = verify_oidc_authorization(user_info)
        if not allowed:
            log_security_event(
                "login", "denied", auth_method="oidc", reason=reason,
                oidc_sub=oidc_sub,
            )
            next_url = session.pop("oidc_next_url", None) or "/"
            return redirect(url_for("login", error="oidc_denied", next=next_url))

        # Set session as permanent and authenticated
        session.permanent = True
        session["authenticated"] = True
        session["user_id"] = user_id
        session["user_name"] = user_name
        session["auth_method"] = "oidc"

        log_security_event("login", "success", auth_method="oidc", oidc_sub=oidc_sub)

        # Redirect to the stored next URL or index
        next_url = session.pop("oidc_next_url", None) or url_for("index")
        return redirect(next_url)

    except Exception as e:
        log_security_event("login", "error", auth_method="oidc", error=type(e).__name__)
        next_url = session.pop("oidc_next_url", None) or "/"
        return redirect(url_for("login", error="oidc_failed", next=next_url))



app.add_url_rule("/health", "health", health, methods=["GET"])
app.add_url_rule("/health/storage", "storage_health", storage_health, methods=["GET"])
app.add_url_rule("/health/storage/read", "storage_health_read", storage_health_read, methods=["GET"])
app.add_url_rule("/health/storage/write", "storage_health_write", storage_health_write, methods=["GET"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
