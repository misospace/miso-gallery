from __future__ import annotations

import os
from pathlib import Path

from flask import (
    Flask,
    abort,
    redirect,
    render_template_string,
    request,
    send_from_directory,
    session,
    url_for,
)
from PIL import Image, UnidentifiedImageError

from auth import (
    OIDC_CALLBACK_URL,
    OIDC_CLIENT_ID,
    OIDC_CLIENT_SECRET,
    OIDC_ISSUER,
    is_auth_enabled,
    require_auth,
    resolved_auth_mode,
    verify_local_password,
)
from security import add_security_headers, csrf_token, rate_limit, sanitize_path, validate_csrf

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32))
app.after_request(add_security_headers)

DATA_FOLDER = Path(os.environ.get("DATA_FOLDER", "/data"))
THUMBNAIL_CACHE_DIR = DATA_FOLDER / ".thumb_cache"
THUMBNAIL_SIZE = (400, 400)
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
FAVICON_URL = os.environ.get("FAVICON_URL", "").strip()

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Miso Gallery</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background:#0d0d0d; color:#e0e0e0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; min-height:100vh; }
    header { background:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%); padding:20px 30px; display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #333; gap:12px; flex-wrap:wrap; }
    h1 { font-size:1.5rem; background:linear-gradient(90deg,#f5a623,#f76c1c); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
    .header-actions { display:flex; align-items:center; gap:10px; margin-left:auto; }
    .breadcrumb { color:#888; font-size:0.9rem; }
    .breadcrumb a { color:#f5a623; text-decoration:none; }
    .refresh-btn { background:linear-gradient(135deg,#2f2f4f 0%,#243357 100%); color:#f5a623; border:1px solid #4b4b75; border-radius:8px; padding:8px 12px; font-size:0.9rem; cursor:pointer; }
    .container { padding:20px; }
    .toolbar { display:flex; gap:10px; flex-wrap:wrap; margin-bottom:15px; }
    .toolbar button { background:#2a2a2a; color:#f0f0f0; border:1px solid #444; border-radius:6px; padding:8px 12px; cursor:pointer; font-size:0.85rem; }
    .toolbar .danger { background:#a52834; border-color:#dc3545; }
    .toolbar .danger:disabled { opacity:0.5; cursor:not-allowed; }
    .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:15px; }
    .folder,.image-card { background:#1a1a1a; border-radius:10px; overflow:hidden; transition:transform .2s, box-shadow .2s; }
    .folder:hover,.image-card:hover { transform:translateY(-3px); box-shadow:0 8px 25px rgba(245,166,35,.15); }
    .folder { padding:30px; text-align:center; border:1px dashed #444; text-decoration:none; }
    .folder-icon { font-size:3rem; margin-bottom:10px; }
    .folder-name { color:#f5a623; font-weight:500; }
    .image-card { position:relative; border:1px solid transparent; }
    .image-card.selected { border-color:#f5a623; box-shadow:0 0 0 2px rgba(245,166,35,.3); }
    .image-card img { width:100%; height:180px; object-fit:cover; display:block; }
    .image-info { padding:10px; font-size:.8rem; color:#888; }
    .image-name { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .delete-btn { position:absolute; top:10px; right:10px; background:rgba(220,53,69,.9); color:white; border:none; padding:8px 12px; border-radius:5px; cursor:pointer; font-size:.8rem; opacity:0; transition:opacity .2s; }
    .image-card:hover .delete-btn { opacity:1; }
    .selector { position:absolute; top:10px; left:10px; z-index:2; transform:scale(1.2); cursor:pointer; }
    .empty { text-align:center; padding:50px; color:#666; }
    .stats { color:#666; font-size:.85rem; margin-top:20px; text-align:center; }
  </style>
</head>
<body>
  <header>
    <h1>🍲 Miso Gallery</h1>
    <div class="header-actions">
      {% if parent_url %}
      <a class="refresh-btn" href="{{ parent_url }}" title="Go up one level">← Back</a>
      {% endif %}
      <div class="breadcrumb">{{ breadcrumb|safe }}</div>
      <button type="button" id="refreshBtn" class="refresh-btn" title="Refresh current folder">↻ Refresh</button>
    </div>
  </header>
  <div class="container">
    {% if items %}
    <form id="bulkDeleteForm" method="POST" action="/bulk-delete">
      <input type="hidden" name="csrf_token" value="{{ csrf }}">
      <input type="hidden" name="current_subpath" value="{{ current_subpath }}">
      <div class="toolbar">
        <button type="button" id="selectAllBtn">Select all</button>
        <button type="button" id="deselectAllBtn">Deselect all</button>
        <button type="submit" id="bulkDeleteBtn" class="danger" disabled onclick="return confirmBulkDelete()">Delete selected (0)</button>
      </div>
      <div class="grid">
        {% for item in items %}
          {% if item.is_dir %}
            <a href="{{ item.url }}" class="folder">
              <div class="folder-icon">📁</div>
              <div class="folder-name">{{ item.name }}</div>
            </a>
          {% else %}
            <div class="image-card" data-image-card>
              <input class="selector" type="checkbox" name="filenames" value="{{ item.rel_path }}" onchange="syncSelectionState()">
              <a href="{{ item.view_url }}" target="_blank"><img src="{{ item.thumb_url }}" alt="{{ item.name }}" loading="lazy"></a>
              <div class="image-info"><div class="image-name">{{ item.name }}</div><div>{{ item.size }}</div></div>
              <button type="submit" class="delete-btn" formaction="{{ item.delete_url }}" formmethod="POST" onclick="return confirm('Delete {{ item.name }}?')">🗑️</button>
            </div>
          {% endif %}
        {% endfor %}
      </div>
    </form>
    {% else %}
      <div class="empty">No images in this folder</div>
    {% endif %}
    <div class="stats">{{ stats.folders }} folders • {{ stats.images }} images</div>
  </div>
  <script>
    document.getElementById('refreshBtn')?.addEventListener('click', () => window.location.reload());
    function getSelectors() { return Array.from(document.querySelectorAll('input.selector[name="filenames"]')); }
    function syncSelectionState() {
      const selectors = getSelectors();
      const selectedCount = selectors.filter(s => s.checked).length;
      const bulkDeleteBtn = document.getElementById('bulkDeleteBtn');
      selectors.forEach((selector) => selector.closest('[data-image-card]')?.classList.toggle('selected', selector.checked));
      if (bulkDeleteBtn) { bulkDeleteBtn.disabled = selectedCount === 0; bulkDeleteBtn.textContent = `Delete selected (${selectedCount})`; }
    }
    function setAllSelections(checked) { getSelectors().forEach((selector) => selector.checked = checked); syncSelectionState(); }
    function confirmBulkDelete() { const c = getSelectors().filter(s => s.checked).length; return c > 0 && confirm(`Delete ${c} selected image(s)?`); }
    document.getElementById('selectAllBtn')?.addEventListener('click', () => setAllSelections(true));
    document.getElementById('deselectAllBtn')?.addEventListener('click', () => setAllSelections(false));
    syncSelectionState();
  </script>
</body>
</html>
"""

LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Login - Miso Gallery</title>
<style>
 body{background:#0d0d0d;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}
 .card{background:#1a1a1a;padding:32px;border-radius:10px;min-width:320px}
 input,button{width:100%;padding:10px;margin-top:10px;border-radius:6px;border:1px solid #333;background:#111;color:#eee}
 button{cursor:pointer;background:linear-gradient(135deg,#f5a623,#f76c1c);border:none}
 .muted{color:#999;font-size:.9rem;margin-top:8px}
</style></head>
<body><div class="card">
  <h2>🍲 Miso Gallery</h2>
  {% if mode == 'oidc' %}
    <p class="muted">OIDC mode is configured but callback flow is not yet implemented.</p>
    <p class="muted">Please switch to local auth temporarily.</p>
  {% else %}
  <form method="POST" action="/auth">
    <input type="hidden" name="csrf_token" value="{{ csrf }}">
    <input type="password" name="password" placeholder="Password" required>
    <button type="submit">Login</button>
  </form>
  {% endif %}
</div></body></html>
"""


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
        img.thumbnail(THUMBNAIL_SIZE)
        img.save(output_path, format="JPEG", quality=85, optimize=True)


def remove_thumbnail_cache_for(rel_path: str) -> None:
    ensure_thumbnail_cache_dir()
    safe_name = sanitize_rel_path(rel_path).replace("/", "__")
    for cached_file in THUMBNAIL_CACHE_DIR.iterdir():
        if cached_file.name.startswith(f"{safe_name}."):
            try:
                cached_file.unlink()
            except OSError:
                pass


def format_size(size: int) -> str:
    value = float(size)
    for unit in ["B", "KB", "MB", "GB"]:
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


@app.before_request
def check_auth():
    if not is_auth_enabled():
        return None

    # Keep direct assets publicly shareable.
    if request.path.startswith("/view/") or request.path.startswith("/thumb/") or request.path == "/favicon.ico":
        return None

    if request.path in ["/login", "/auth", "/logout"]:
        return None

    if session.get("authenticated"):
        return None

    return redirect(url_for("login", next=request.path))


@app.route("/favicon.ico")
def favicon():
    if FAVICON_URL:
        return redirect(FAVICON_URL)
    return ("", 204)


@app.route("/")
@app.route("/<path:subpath>")
@require_auth
def index(subpath: str = ""):
    safe_subpath = sanitize_rel_path(subpath) if subpath else ""
    folder_path = DATA_FOLDER / safe_subpath
    if not folder_path.exists() or not folder_path.is_dir():
        return "Folder not found", 404

    items = []
    stats = {"folders": 0, "images": 0}

    for item in sorted(folder_path.iterdir(), key=lambda p: p.name.lower()):
        if item.name == ".thumb_cache":
            continue

        rel_path = f"{safe_subpath}/{item.name}".lstrip("/") if safe_subpath else item.name
        rel_path = rel_path.replace("\\", "/")

        if item.is_dir():
            stats["folders"] += 1
            items.append({"name": item.name, "url": url_for("index", subpath=rel_path), "is_dir": True})
        elif item.suffix.lower() in IMAGE_EXTENSIONS:
            stats["images"] += 1
            items.append(
                {
                    "name": item.name,
                    "rel_path": rel_path,
                    "thumb_url": url_for("thumb", filename=rel_path),
                    "view_url": url_for("view", filename=rel_path),
                    "delete_url": url_for("delete", filename=rel_path),
                    "size": format_size(item.stat().st_size),
                    "is_dir": False,
                }
            )

    parent_url = None
    if safe_subpath:
        parts = safe_subpath.split("/")
        crumbs = ['<a href="/">Home</a>']
        for i, part in enumerate(parts[:-1]):
            path = "/".join(parts[: i + 1])
            crumbs.append(f'<a href="/{path}">{part}</a>')
        crumbs.append(parts[-1])
        breadcrumb = " / ".join(crumbs)

        parent_subpath = "/".join(parts[:-1])
        parent_url = url_for("index", subpath=parent_subpath) if parent_subpath else url_for("index")
    else:
        breadcrumb = "All Images"

    return render_template_string(
        HTML_TEMPLATE,
        items=items,
        breadcrumb=breadcrumb,
        parent_url=parent_url,
        stats=stats,
        current_subpath=safe_subpath,
        csrf=csrf_token(),
    )


@app.route("/thumb/<path:filename>")
@rate_limit(max_requests=120, window=60)
def thumb(filename: str):
    rel_path = sanitize_rel_path(filename)
    source_path = source_file_path(rel_path)
    if not source_path.exists() or source_path.suffix.lower() not in IMAGE_EXTENSIONS:
        return "Not found", 404

    ensure_thumbnail_cache_dir()
    cached_name = thumbnail_filename(rel_path, source_path)
    cached_path = THUMBNAIL_CACHE_DIR / cached_name

    if not cached_path.exists():
        try:
            generate_thumbnail(source_path, cached_path)
        except (UnidentifiedImageError, OSError):
            return send_from_directory(str(DATA_FOLDER), rel_path)

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
        return {"error": "Invalid filename"}, 400
    if not validate_csrf(request.form.get("csrf_token")):
        return {"error": "Invalid CSRF token"}, 403

    rel_path = sanitize_rel_path(filename)
    file_path = source_file_path(rel_path)
    if file_path.exists() and file_path.is_file():
        file_path.unlink()
        remove_thumbnail_cache_for(rel_path)

    folder = os.path.dirname(rel_path)
    return redirect(url_for("index", subpath=folder if folder else ""))


@app.route("/bulk-delete", methods=["POST"])
@require_auth
@rate_limit(max_requests=20, window=60)
def bulk_delete():
    if not validate_csrf(request.form.get("csrf_token")):
        return {"error": "Invalid CSRF token"}, 403

    current_subpath = sanitize_rel_path(request.form.get("current_subpath", "")) if request.form.get("current_subpath") else ""
    selected = request.form.getlist("filenames")

    for rel_path in selected:
        if not sanitize_path(rel_path):
            continue
        safe_rel_path = sanitize_rel_path(rel_path)
        file_path = source_file_path(safe_rel_path)
        if file_path.exists() and file_path.is_file():
            file_path.unlink()
            remove_thumbnail_cache_for(safe_rel_path)

    return redirect(url_for("index", subpath=current_subpath))


@app.route("/login")
def login():
    return render_template_string(LOGIN_TEMPLATE, csrf=csrf_token(), mode=resolved_auth_mode())


@app.route("/auth", methods=["POST"])
@rate_limit(max_requests=8, window=60)
def auth():
    if not validate_csrf(request.form.get("csrf_token")):
        return {"error": "Invalid CSRF token"}, 403

    if resolved_auth_mode() != "local":
        return redirect(url_for("login"))

    password = request.form.get("password", "")
    if verify_local_password(password):
        session["authenticated"] = True
        return redirect(request.args.get("next") or url_for("index"))

    return redirect(url_for("login"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
