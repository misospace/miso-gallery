from __future__ import annotations

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
    render_template_string,
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
    require_auth,
    resolved_auth_mode,
    verify_local_password,
)
from security import add_security_headers, csrf_token, rate_limit, sanitize_path, validate_csrf
from trash import empty_trash, list_trash, move_to_trash, purge_old_trash, restore_from_trash

DATA_FOLDER = Path(os.environ.get("DATA_FOLDER", "/data"))
THUMBNAIL_CACHE_DIR = DATA_FOLDER / ".thumb_cache"


def resolve_secret_key() -> str:
    configured = os.environ.get("SECRET_KEY", "").strip()
    if configured:
        return configured

    key_file = Path(os.environ.get("SECRET_KEY_FILE", str(DATA_FOLDER / ".miso-gallery-secret-key")))

    try:
        if key_file.exists():
            persisted = key_file.read_text(encoding="utf-8").strip()
            if persisted:
                return persisted
    except OSError:
        pass

    generated = secrets.token_urlsafe(48)
    try:
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(generated, encoding="utf-8")
        os.chmod(key_file, 0o600)
    except OSError:
        pass

    return generated


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


def log_security_event(event: str, outcome: str, **fields: object) -> None:
    """Emit a structured log line for security/access events.

    Intentionally avoids logging secrets (passwords, OIDC tokens, raw userinfo).
    """

    try:
        payload: dict[str, object] = {
            "event": event,
            "outcome": outcome,
            "path": request.path,
            "method": request.method,
            "remote_addr": _client_ip(),
            "user_id": session.get("user_id"),
            "user_name": session.get("user_name"),
            "auth_method": session.get("auth_method") or ("local" if session.get("authenticated") else None),
        }
        payload.update(fields)
        payload = {k: v for k, v in payload.items() if v is not None}
        app.logger.info(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    except Exception:
        # Never break request handling due to logging
        return


# Configure OAuth for OIDC if enabled
configure_oauth(app)
THUMBNAIL_MAX_SIZE = 600
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
FAVICON_URL = os.environ.get("FAVICON_URL", "").strip()

PWA_THEME_COLOR = "#0d0d0d"
PWA_APP_NAME = "Miso Gallery"
APP_VERSION = (os.environ.get("APP_VERSION") or "0.1.12").strip() or "0.1.12"
WEBHOOK_TASK_PREFIX = "WEBHOOK_TASK_"
AUTO_FOLDER_COVERS_ENABLED = os.environ.get("GALLERY_AUTO_FOLDER_COVERS", "false").strip().lower() in {"1", "true", "yes", "on"}
FOLDER_COVER_CACHE_TTL = max(int(os.environ.get("GALLERY_COVER_CACHE_TTL", "3600") or 3600), 0)
_FOLDER_COVER_CACHE: dict[str, tuple[float, str | None]] = {}


def _webhook_enabled() -> bool:
    return os.environ.get("WEBHOOK_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


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


SERVICE_WORKER_TEMPLATE = """
const CACHE_VERSION = "miso-gallery-v1";
const CORE_ASSETS = [
  "/",
  "/recent",
  "/trash",
  "/manifest.webmanifest",
  "/assets/icon-192.png",
  "/assets/icon-512.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => cache.addAll(CORE_ASSETS)).catch(() => undefined)
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_VERSION).map((key) => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const requestUrl = new URL(request.url);
  if (requestUrl.origin !== self.location.origin) return;

  if (request.destination === "image") {
    event.respondWith(
      caches.match(request).then((cached) => {
        if (cached) return cached;
        return fetch(request).then((response) => {
          if (response && response.status === 200) {
            const copy = response.clone();
            caches.open(CACHE_VERSION).then((cache) => cache.put(request, copy));
          }
          return response;
        });
      })
    );
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response && response.status === 200) {
            const copy = response.clone();
            caches.open(CACHE_VERSION).then((cache) => cache.put(request, copy));
          }
          return response;
        })
        .catch(() => caches.match(request).then((cached) => cached || caches.match("/")))
    );
  }
});
"""

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="theme-color" content="{{ theme_color }}">
  <link rel="manifest" href="/manifest.webmanifest">
  <link rel="apple-touch-icon" href="/assets/icon-192.png">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="Miso Gallery">
  <meta name="mobile-web-app-capable" content="yes">
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
    .nav-toggle { background:linear-gradient(135deg,#2f2f4f 0%,#243357 100%); color:#f5a623; border:1px solid #4b4b75; border-radius:10px; padding:10px 12px; font-size:1rem; cursor:pointer; line-height:1; }
    .drawer-overlay { position:fixed; inset:0; background:rgba(0,0,0,.55); opacity:0; pointer-events:none; transition:opacity .2s ease; z-index:999; }
    .drawer-overlay.open { opacity:1; pointer-events:auto; }
    .drawer { position:fixed; top:0; left:0; height:100vh; width:300px; max-width:85vw; background:#121217; border-right:1px solid #2f2f2f; transform:translateX(-105%); transition:transform .2s ease; z-index:1000; padding:16px; display:flex; flex-direction:column; gap:14px; }
    .drawer.open { transform:translateX(0); }
    .drawer-header { display:flex; align-items:center; justify-content:space-between; gap:12px; }
    .drawer-brand { font-weight:700; color:#f5a623; }
    .drawer-close { background:transparent; border:1px solid #333; color:#ddd; border-radius:10px; padding:8px 10px; cursor:pointer; }
    .drawer-path { color:#9aa1a8; font-size:.9rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .drawer-links { display:flex; flex-direction:column; gap:6px; }
    .drawer-links a { display:block; padding:10px 10px; border-radius:10px; text-decoration:none; color:#e0e0e0; border:1px solid transparent; }
    .drawer-links a:hover { background:#1b1b26; border-color:#2c2c3a; }
    .drawer-links a.current { border-color:rgba(245,166,35,.35); background:rgba(245,166,35,.08); }
    .drawer-divider { height:1px; background:#2a2a2a; margin:6px 0; }
    .container { padding:20px; }
    .toolbar { display:flex; gap:10px; flex-wrap:wrap; margin-bottom:15px; }
    .filter-summary { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin:-4px 0 15px; color:#cfcfcf; }
    .filter-chip { display:inline-flex; align-items:center; gap:6px; padding:6px 10px; border-radius:999px; background:rgba(245,166,35,.12); border:1px solid rgba(245,166,35,.35); color:#f6c36d; font-size:.92rem; }
    .clear-filter-link { color:#f5a623; text-decoration:none; font-size:.92rem; }
    .clear-filter-link:hover { text-decoration:underline; }
    .toolbar button { background:#2a2a2a; color:#f0f0f0; border:1px solid #444; border-radius:6px; padding:8px 12px; cursor:pointer; font-size:0.85rem; }
    .toolbar .danger { background:#a52834; border-color:#dc3545; }
    .toolbar .danger:disabled { opacity:0.5; cursor:not-allowed; }
    .selection-actions { display:none; align-items:center; gap:10px; padding:10px 12px; margin:-4px 0 15px; background:#171717; border:1px solid #343434; border-radius:8px; }
    .selection-actions.active { display:flex; flex-wrap:wrap; }
    .selection-count { color:#f5a623; font-weight:600; }
    .selection-actions .ghost-btn { background:transparent; color:#c9c9c9; border:1px solid #4a4a4a; }
    .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:15px; }
    .folder-card,.image-card { background:#1a1a1a; border-radius:10px; overflow:hidden; transition:transform .2s, box-shadow .2s; position:relative; }
    .folder-card:hover,.image-card:hover { transform:translateY(-3px); box-shadow:0 8px 25px rgba(245,166,35,.15); }
    .folder-card { border:1px dashed #444; }
    .folder-card.selected { border-color:#f5a623; box-shadow:0 0 0 2px rgba(245,166,35,.3); }
    .folder { display:block; padding:30px; text-align:center; text-decoration:none; position:relative; min-height:180px; }
    .folder-icon { font-size:3rem; margin-bottom:10px; }
    .folder-preview { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }
    .folder-name { color:#f5a623; font-weight:500; position:relative; z-index:1; text-shadow:0 2px 8px rgba(0,0,0,.7); background:rgba(0,0,0,.3); display:inline-block; padding:4px 8px; border-radius:6px; }
    .image-card { position:relative; border:1px solid transparent; }
    .image-card.selected { border-color:#f5a623; box-shadow:0 0 0 2px rgba(245,166,35,.3); }
    .image-card img { width:100%; height:180px; object-fit:cover; display:block; }
    .image-info { padding:10px; font-size:.8rem; color:#888; }
    .image-name { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .delete-btn { position:absolute; top:10px; right:10px; background:rgba(220,53,69,.9); color:white; border:none; padding:8px 12px; border-radius:5px; cursor:pointer; font-size:.8rem; opacity:0; transition:opacity .2s; }
    .image-card:hover .delete-btn { opacity:1; }
    .thumb-preview-btn { position:absolute; bottom:10px; right:10px; background:rgba(245,166,35,.95); color:#0d0d0d; border:none; padding:6px 10px; border-radius:5px; cursor:pointer; font-size:.75rem; font-weight:600; opacity:0; transition:opacity .2s; text-decoration:none; display:flex; align-items:center; gap:4px; z-index:3; }
    .thumb-preview-btn:hover { background:#f5a623; }
    .image-card:hover .thumb-preview-btn { opacity:1; }
    .selector { position:absolute; top:10px; left:10px; z-index:2; transform:scale(1.2); cursor:pointer; }
    .empty { text-align:center; padding:50px; color:#666; }
    .stats { color:#666; font-size:.85rem; margin-top:20px; text-align:center; }
  </style>
</head>
<body>
  <header>
    <button type="button" id="navToggleBtn" class="nav-toggle" aria-label="Open menu" aria-expanded="false">☰</button>
    <h1>🍲 Miso Gallery</h1>
    <div class="header-actions">
      {% if parent_url %}
      <a class="refresh-btn" href="{{ parent_url }}" title="Go up one level">← Back</a>
      {% endif %}
      <div class="breadcrumb">{{ breadcrumb|safe }}</div>
      <a class="refresh-btn" href="/trash" title="Open trash bin">🗑️ Trash</a>
      <a class="refresh-btn" href="/recent" title="Recent uploads">📅 Recent</a>
      <button type="button" id="installPwaBtn" class="refresh-btn" title="Install app" hidden>⬇ Install</button>
      <button type="button" id="refreshBtn" class="refresh-btn" title="Refresh current folder">↻ Refresh</button>
    </div>
  </header>

  <div id="drawerOverlay" class="drawer-overlay"></div>
  <nav id="drawer" class="drawer" aria-label="Navigation">
    <div class="drawer-header">
      <div class="drawer-brand">🍲 Miso Gallery</div>
      <button type="button" id="drawerCloseBtn" class="drawer-close" aria-label="Close menu">✕</button>
    </div>
    <div class="drawer-path">Path: {{ current_subpath if current_subpath else '/' }}</div>
    <div class="drawer-links">
      <a href="/">🏠 Home</a>
      {% for crumb in nav_crumbs %}
        <a href="{{ crumb.url }}" class="{% if crumb.is_current %}current{% endif %}">📁 {{ crumb.name }}</a>
      {% endfor %}
      <div class="drawer-divider"></div>
      <a href="/recent">📅 Recent</a>
      <a href="/trash">🗑️ Trash</a>
      <a href="/settings">⚙️ Settings</a>
      <a href="/about">ℹ️ About ({{ app_version }})</a>
      <a href="/logout">🚪 Logout</a>
    </div>
  </nav>

  <div class="container">
    <form method="GET" action="{{ url_for('index', subpath=current_subpath) }}" style="margin-bottom:15px;">
      <input id="categorySearch" type="text" name="q" placeholder="Filter categories..." value="{{ search_query }}" autocomplete="off" spellcheck="false" aria-label="Filter categories by name" style="padding:6px 10px; border-radius:4px; border:1px solid #444; background:#2a2a2a; color:#e0e0e0; min-width: 240px;">
      <button type="submit" class="refresh-btn" style="margin-left:5px;">🔍 Apply filter</button>
      {% if category_filter_active %}
      <a href="{{ url_for('index', subpath=current_subpath) }}" class="refresh-btn" style="margin-left:5px; text-decoration:none; display:inline-flex; align-items:center;">✕ Clear</a>
      {% endif %}
    </form>
    {% if category_filter_active %}
    <div class="filter-summary" role="status" aria-live="polite">
      <span>Active filter:</span>
      <span class="filter-chip">Category name contains “{{ search_query }}”</span>
      <a href="{{ url_for('index', subpath=current_subpath) }}" class="clear-filter-link">Clear filter</a>
    </div>
    {% endif %}
    {% if items %}
    <form id="bulkDeleteForm" method="POST" action="/bulk-delete">
      <input type="hidden" name="csrf_token" value="{{ csrf }}">
      <input type="hidden" name="current_subpath" value="{{ current_subpath }}">
      <div class="toolbar">
        <button type="button" id="selectAllBtn">Select all</button>
        <button type="button" id="deselectAllBtn">Deselect all</button>
      </div>
      <div id="selectionActions" class="selection-actions" aria-live="polite">
        <span id="selectionCount" class="selection-count">0 selected</span>
        <button type="button" id="clearSelectionBtn" class="ghost-btn">Clear selection</button>
        <button type="submit" id="bulkDeleteBtn" class="danger" disabled onclick="return confirmBulkDelete()">Delete selected (0)</button>
      </div>
      <div class="grid">
        {% for item in items %}
          {% if item.is_dir %}
            <div class="folder-card" data-folder-card>
              <input class="selector" type="checkbox" name="folders" value="{{ item.rel_path }}" onchange="syncSelectionState()">
              <a href="{{ item.url }}" class="folder">
                {% if item.cover_thumb_url %}
                  <img class="folder-preview" src="{{ item.cover_thumb_url }}" alt="{{ item.name }} folder preview" loading="lazy" decoding="async">
                {% else %}
                  <div class="folder-icon">📁</div>
                {% endif %}
                <div class="folder-name">{{ item.name }}</div>
              </a>
            </div>
          {% else %}
            <div class="image-card" data-image-card>
              <input class="selector" type="checkbox" name="filenames" value="{{ item.rel_path }}" onchange="syncSelectionState()">
              <a href="{{ item.view_url }}" target="_blank"><img src="{{ item.thumb_url }}" alt="{{ item.name }}" loading="lazy" decoding="async"></a>
              <div class="image-info"><div class="image-name">{{ item.name }}</div><div>{{ item.size }}</div></div>
              <button type="submit" class="delete-btn" formaction="{{ item.delete_url }}" formmethod="POST" onclick="return confirm('Delete {{ item.name }}?')">🗑️</button>
              <a href="{{ item.thumb_url }}" target="_blank" class="thumb-preview-btn" title="View thumbnail only">🖼️ Thumb</a>
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
    let deferredInstallPrompt = null;
    const installBtn = document.getElementById('installPwaBtn');

    if ('serviceWorker' in navigator) {
      window.addEventListener('load', () => {
        navigator.serviceWorker.register('/service-worker.js').catch(() => undefined);
      });
    }

    window.addEventListener('beforeinstallprompt', (event) => {
      event.preventDefault();
      deferredInstallPrompt = event;
      if (installBtn) installBtn.hidden = false;
    });

    installBtn?.addEventListener('click', async () => {
      if (!deferredInstallPrompt) return;
      deferredInstallPrompt.prompt();
      const result = await deferredInstallPrompt.userChoice.catch(() => null);
      deferredInstallPrompt = null;
      if (result?.outcome === 'dismissed') {
        installBtn.hidden = false;
        return;
      }
      installBtn.hidden = true;
    });

    window.addEventListener('appinstalled', () => {
      deferredInstallPrompt = null;
      if (installBtn) installBtn.hidden = true;
    });

    const drawer = document.getElementById('drawer');
    const drawerOverlay = document.getElementById('drawerOverlay');
    const navToggleBtn = document.getElementById('navToggleBtn');
    const drawerCloseBtn = document.getElementById('drawerCloseBtn');

    function setDrawerOpen(open) {
      drawer?.classList.toggle('open', open);
      drawerOverlay?.classList.toggle('open', open);
      navToggleBtn?.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    navToggleBtn?.addEventListener('click', () => setDrawerOpen(true));
    drawerCloseBtn?.addEventListener('click', () => setDrawerOpen(false));
    drawerOverlay?.addEventListener('click', () => setDrawerOpen(false));
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') setDrawerOpen(false); });

    document.getElementById('refreshBtn')?.addEventListener('click', () => window.location.reload());

    // Category search (Issue #51)
    const categorySearchInput = document.getElementById('categorySearch');
    function applyCategorySearchFilter() {
      if (!categorySearchInput) return;
      const query = (categorySearchInput.value || '').trim().toLowerCase();
      const folderCards = Array.from(document.querySelectorAll('[data-folder-card]'));
      if (!folderCards.length) return;
      folderCards.forEach((card) => {
        const name = (card.querySelector('.folder-name')?.textContent || '').toLowerCase();
        card.style.display = !query || name.includes(query) ? '' : 'none';
      });
    }
    categorySearchInput?.addEventListener('input', applyCategorySearchFilter);
    applyCategorySearchFilter();

    function getSelectors() { return Array.from(document.querySelectorAll('input.selector[name="filenames"], input.selector[name="folders"]')); }
    function syncSelectionState() {
      const selectors = getSelectors();
      const selectedCount = selectors.filter(s => s.checked).length;
      const bulkDeleteBtn = document.getElementById('bulkDeleteBtn');
      const selectionActions = document.getElementById('selectionActions');
      const selectionCount = document.getElementById('selectionCount');
      const clearSelectionBtn = document.getElementById('clearSelectionBtn');
      selectors.forEach((selector) => {
        const card = selector.closest('[data-image-card]') || selector.closest('[data-folder-card]');
        card?.classList.toggle('selected', selector.checked);
      });
      if (bulkDeleteBtn) { bulkDeleteBtn.disabled = selectedCount === 0; bulkDeleteBtn.textContent = `Delete selected (${selectedCount})`; }
      if (selectionCount) { selectionCount.textContent = `${selectedCount} selected`; }
      if (selectionActions) { selectionActions.classList.toggle('active', selectedCount > 0); }
      if (clearSelectionBtn) { clearSelectionBtn.disabled = selectedCount === 0; }
    }
    function setAllSelections(checked) { getSelectors().forEach((selector) => selector.checked = checked); syncSelectionState(); }
    function confirmBulkDelete() { const c = getSelectors().filter(s => s.checked).length; return c > 0 && confirm(`Delete ${c} selected image(s)?`); }
    document.getElementById('selectAllBtn')?.addEventListener('click', () => setAllSelections(true));
    document.getElementById('deselectAllBtn')?.addEventListener('click', () => setAllSelections(false));
    document.getElementById('clearSelectionBtn')?.addEventListener('click', () => setAllSelections(false));
    syncSelectionState();
  </script>
</body>
</html>
"""

LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><meta name="theme-color" content="{{ theme_color }}"><link rel="manifest" href="/manifest.webmanifest"><link rel="apple-touch-icon" href="/assets/icon-192.png"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><meta name="apple-mobile-web-app-title" content="Miso Gallery"><meta name="mobile-web-app-capable" content="yes"><title>Login - Miso Gallery</title>
<style>
 body{background:#0d0d0d;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}
 .card{background:#1a1a1a;padding:32px;border-radius:10px;min-width:320px;max-width:420px;border:1px solid #2f2f2f}
 input,button{width:100%;padding:10px;margin-top:10px;border-radius:6px;border:1px solid #333;background:#111;color:#eee}
 button{cursor:pointer;background:linear-gradient(135deg,#f5a623,#f76c1c);border:none}
 button.oidc-btn{background:linear-gradient(135deg,#2f2f4f 0%,#243357 100%);border:1px solid #4b4b75;color:#f5a623}
 button.oidc-btn:hover{background:linear-gradient(135deg,#3f3f5f 0%,#344367 100%)}
 .muted{color:#999;font-size:.9rem;margin-top:8px;text-align:center}
 .divider{display:flex;align-items:center;text-align:center;color:#666;margin:20px 0}
 .divider::before,.divider::after{content:'';flex:1;border-bottom:1px solid #333}
 .divider::before{margin-right:10px}
 .divider::after{margin-left:10px}
 h2{margin-bottom:4px;text-align:center}
 .subtitle{color:#9aa1a8;font-size:.92rem;text-align:center;margin-bottom:8px}
 .alert{margin-top:10px;padding:10px;border-radius:6px;font-size:.9rem;border:1px solid #4a2a2a;background:#2a1515;color:#ffb4b4}
 .note{margin-top:10px;color:#777;font-size:.82rem;text-align:center}
</style></head>
<body><div class="card">
  <h2>🍲 Miso Gallery</h2>
  <p class="subtitle">Sign in to view and manage your gallery.</p>

  {% if error %}
  <div class="alert">{{ error }}</div>
  {% endif %}

  {% if oidc_enabled %}
  <form method="GET" action="/auth/oidc">
    <input type="hidden" name="next" value="{{ next_url }}">
    <button type="submit" class="oidc-btn">Continue with {{ oidc_label }}</button>
  </form>
  {% endif %}

  {% if local_enabled %}
  {% if oidc_enabled %}<div class="divider">or</div>{% endif %}
  <form method="POST" action="/auth">
    <input type="hidden" name="csrf_token" value="{{ csrf }}">
    <input type="hidden" name="next" value="{{ next_url }}">
    <input type="password" name="password" placeholder="Password" autocomplete="current-password" required>
    <button type="submit">Login with Password</button>
  </form>
  {% endif %}

  {% if not oidc_enabled and not local_enabled %}
  <p class="muted">No authentication method is configured.</p>
  {% endif %}

  <p class="note">Need access? Ask your administrator.</p>
</div>
<script>
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/service-worker.js').catch(() => undefined);
    });
  }
</script>
</body></html>
"""

TRASH_TEMPLATE = """
<!DOCTYPE html>
<html><head><meta charset=\"UTF-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\"><meta name=\"theme-color\" content=\"{{ theme_color }}\"><link rel=\"manifest\" href=\"/manifest.webmanifest\"><link rel=\"apple-touch-icon\" href=\"/assets/icon-192.png\"><meta name=\"apple-mobile-web-app-capable\" content=\"yes\"><meta name=\"apple-mobile-web-app-status-bar-style\" content=\"black-translucent\"><meta name=\"apple-mobile-web-app-title\" content=\"Miso Gallery\"><meta name=\"mobile-web-app-capable\" content=\"yes\"><title>Trash - Miso Gallery</title>
<style>
 body{background:#0d0d0d;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;margin:0}
 .wrap{max-width:1000px;margin:0 auto;padding:24px}
 a{color:#f5a623;text-decoration:none}
 table{width:100%;border-collapse:collapse;margin-top:16px}
 th,td{padding:10px;border-bottom:1px solid #333;text-align:left}
 button{padding:8px 12px;border-radius:6px;border:1px solid #444;background:#222;color:#eee;cursor:pointer}
 .danger{background:#8b1e2b;border-color:#b91c1c}
</style></head>
<body><div class=\"wrap\">
  <h2>🗑️ Trash</h2>
  <p><a href=\"/\">← Back to Gallery</a></p>
  <form method=\"POST\" action=\"/trash/empty\" onsubmit=\"return confirm('Permanently delete all trashed items?')\">
    <input type=\"hidden\" name=\"csrf_token\" value=\"{{ csrf }}\">
    <button class=\"danger\" type=\"submit\">Empty Trash</button>
  </form>
  <table>
    <thead><tr><th>File</th><th>Original Path</th><th>Deleted At</th><th>Size</th><th>Action</th></tr></thead>
    <tbody>
    {% for item in items %}
      <tr>
        <td>{{ item.name }}</td>
        <td>{{ item.original }}</td>
        <td>{{ item.deleted_at }}</td>
        <td>{{ item.size }}</td>
        <td>
          <form method=\"POST\" action=\"/trash/restore/{{ item.name }}\" style=\"display:inline\">
            <input type=\"hidden\" name=\"csrf_token\" value=\"{{ csrf }}\">
            <button type=\"submit\">Restore</button>
          </form>
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
</div>
<script>
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/service-worker.js').catch(() => undefined);
    });
  }
</script>
</body></html>
"""

RECENT_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="theme-color" content="{{ theme_color }}">
  <link rel="manifest" href="/manifest.webmanifest">
  <link rel="apple-touch-icon" href="/assets/icon-192.png">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="Miso Gallery">
  <meta name="mobile-web-app-capable" content="yes">
  <title>Recent - Miso Gallery</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background:#0d0d0d; color:#e0e0e0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; min-height:100vh; }
    header { background:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%); padding:20px 30px; display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #333; gap:12px; flex-wrap:wrap; }
    h1 { font-size:1.5rem; background:linear-gradient(90deg,#f5a623,#f76c1c); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
    .header-actions { display:flex; align-items:center; gap:10px; margin-left:auto; }
    .refresh-btn { background:linear-gradient(135deg,#2f2f4f 0%,#243357 100%); color:#f5a623; border:1px solid #4b4b75; border-radius:8px; padding:8px 12px; font-size:0.9rem; cursor:pointer; text-decoration:none; }
    .container { padding:20px; }
    .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:15px; }
    .image-card { background:#1a1a1a; border-radius:10px; overflow:hidden; transition:transform .2s, box-shadow .2s; }
    .image-card:hover { transform:translateY(-3px); box-shadow:0 8px 25px rgba(245,166,35,.15); }
    .image-card-link { display:block; }
    .image-card img { width:100%; height:180px; object-fit:cover; display:block; }
    .image-info { padding:10px; font-size:.8rem; color:#888; }
    .image-name { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .image-date { color:#666; font-size:.75rem; margin-top:4px; }
    .thumb-preview-btn { position:absolute; bottom:10px; right:10px; background:rgba(245,166,35,.95); color:#0d0d0d; border:none; padding:6px 10px; border-radius:5px; cursor:pointer; font-size:.75rem; font-weight:600; opacity:0; transition:opacity .2s; text-decoration:none; display:flex; align-items:center; gap:4px; z-index:3; }
    .thumb-preview-btn:hover { background:#f5a623; }
    .folder-nav-btn { position:absolute; bottom:10px; left:10px; background:rgba(59,130,246,.95); color:#0d0d0d; border:none; padding:6px 10px; border-radius:5px; cursor:pointer; font-size:.75rem; font-weight:600; opacity:0; transition:opacity .2s; text-decoration:none; display:flex; align-items:center; gap:4px; z-index:3; }
    .folder-nav-btn:hover { background:#3b82f6; }
    .image-card:hover .folder-nav-btn, .image-card:hover .thumb-preview-btn { opacity:1; }
    .empty { text-align:center; padding:50px; color:#666; }
  </style>
</head>
<body>
<header>
  <h1>🍲 Recent</h1>
  <div class="header-actions">
    <button type="button" id="refreshRecentBtn" class="refresh-btn" title="Refresh recent images">↻ Refresh</button>
    <button type="button" id="installPwaBtn" class="refresh-btn" hidden>⬇ Install</button>
    <a href="/" class="refresh-btn">← Gallery</a>
  </div>
</header>
<div class="container">
  <h2 style="margin-bottom:20px;font-size:1.2rem;color:#888;">Recently Added ({{ items|length }})</h2>
  {% if items %}
    <div class="grid">
    {% for item in items %}
      <div class="image-card" style="position:relative;">
        <a href="{{ item.url }}" class="image-card-link" target="_blank">
          <img src="{{ item.thumb }}" alt="{{ item.name }}" loading="lazy" decoding="async">
          <div class="image-info">
            <div class="image-name">{{ item.name }}</div>
            <div class="image-date">{{ item.added }}</div>
          </div>
        </a>
        {% if item.folder_url %}
        <a href="{{ item.folder_url }}" class="folder-nav-btn" title="Go to folder">📁 Folder</a>
        {% endif %}
        <a href="{{ item.thumb }}" target="_blank" class="thumb-preview-btn" title="View thumbnail only">🖼️ Thumb</a>
      </div>
    {% endfor %}
    </div>
  {% else %}
    <div class="empty">No recent images found</div>
  {% endif %}
</div>
<script>
  let deferredInstallPrompt = null;
  const installBtn = document.getElementById('installPwaBtn');
  const refreshBtn = document.getElementById('refreshRecentBtn');

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/service-worker.js').catch(() => undefined);
    });
  }

  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    deferredInstallPrompt = event;
    if (installBtn) installBtn.hidden = false;
  });

  refreshBtn?.addEventListener('click', () => {
    window.location.reload();
  });

  installBtn?.addEventListener('click', async () => {
    if (!deferredInstallPrompt) return;
    deferredInstallPrompt.prompt();
    const result = await deferredInstallPrompt.userChoice.catch(() => null);
    deferredInstallPrompt = null;
    if (result?.outcome === 'dismissed') {
      installBtn.hidden = false;
      return;
    }
    installBtn.hidden = true;
  });

  window.addEventListener('appinstalled', () => {
    deferredInstallPrompt = null;
    if (installBtn) installBtn.hidden = true;
  });
</script>
</body>
</html>
"""

ABOUT_TEMPLATE = """
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><meta name="theme-color" content="{{ theme_color }}"><link rel="manifest" href="/manifest.webmanifest"><link rel="apple-touch-icon" href="/assets/icon-192.png"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><meta name="apple-mobile-web-app-title" content="Miso Gallery"><meta name="mobile-web-app-capable" content="yes"><title>About - Miso Gallery</title>
<style>
 body{background:#0d0d0d;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;margin:0}
 .wrap{max-width:900px;margin:0 auto;padding:24px}
 a{color:#f5a623;text-decoration:none}
 .card{background:#141414;border:1px solid #2f2f2f;border-radius:12px;padding:18px;margin-top:14px}
 .row{display:flex;justify-content:space-between;gap:12px;padding:8px 0;border-bottom:1px solid #242424}
 .row:last-child{border-bottom:none}
 .k{color:#9aa1a8}
</style></head>
<body><div class="wrap">
  <h2>ℹ️ About</h2>
  <p><a href="/">← Back to Gallery</a></p>
  <div class="card">
    <div class="row"><div class="k">Version</div><div>{{ app_version }}</div></div>
    <div class="row"><div class="k">Auth enabled</div><div>{{ auth_enabled }}</div></div>
    <div class="row"><div class="k">Auth mode</div><div>{{ auth_mode }}</div></div>
    <div class="row"><div class="k">OIDC configured</div><div>{{ oidc_configured }}</div></div>
  </div>
</div></body></html>
"""

SETTINGS_TEMPLATE = """
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><meta name="theme-color" content="{{ theme_color }}"><link rel="manifest" href="/manifest.webmanifest"><link rel="apple-touch-icon" href="/assets/icon-192.png"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><meta name="apple-mobile-web-app-title" content="Miso Gallery"><meta name="mobile-web-app-capable" content="yes"><title>Settings - Miso Gallery</title>
<style>
 body{background:#0d0d0d;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;margin:0}
 .wrap{max-width:900px;margin:0 auto;padding:24px}
 a{color:#f5a623;text-decoration:none}
 .card{background:#141414;border:1px solid #2f2f2f;border-radius:12px;padding:18px;margin-top:14px}
 .row{display:flex;justify-content:space-between;gap:12px;padding:8px 0;border-bottom:1px solid #242424}
 .row:last-child{border-bottom:none}
 .k{color:#9aa1a8}
 .note{color:#888;margin-top:10px;font-size:.9rem}
 .btn{margin-top:12px;padding:10px 14px;border-radius:8px;border:1px solid #4b4b75;background:linear-gradient(135deg,#2f2f4f 0%,#243357 100%);color:#f5a623;cursor:pointer}
 .ok{margin-top:12px;padding:10px 12px;border-radius:8px;border:1px solid #245f3d;background:#10271b;color:#9de2b4}
</style></head>
<body><div class="wrap">
  <h2>⚙️ Settings</h2>
  <p><a href="/">← Back to Gallery</a></p>
  <div class="card">
    <div class="row"><div class="k">Data folder</div><div>{{ data_folder }}</div></div>
    <div class="row"><div class="k">Thumbnail cache</div><div>{{ thumb_cache }}</div></div>
    <div class="row"><div class="k">Rate limiting</div><div>enabled</div></div>
  </div>

  <form method="POST" action="/maintenance/thumbnails/regenerate">
    <input type="hidden" name="csrf_token" value="{{ csrf }}">
    <button class="btn" type="submit">🧰 Run thumbnail integrity check</button>
  </form>

  {% if maintenance_result %}
    <div class="ok">Checked: {{ maintenance_result.checked }} • Regenerated: {{ maintenance_result.regenerated }} • Failed: {{ maintenance_result.failed }}</div>
  {% endif %}

  <p class="note">This page intentionally avoids showing secrets or raw environment variables.</p>
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
        img.thumbnail((THUMBNAIL_MAX_SIZE, THUMBNAIL_MAX_SIZE), Image.Resampling.LANCZOS)
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


def run_thumbnail_integrity_check() -> dict[str, int]:
    """Check thumbnails and regenerate missing/invalid entries on demand."""

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
    candidates = sorted(folder_path.rglob("*"), key=lambda p: p.as_posix().lower())
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


@app.before_request
def check_auth():
    if not is_auth_enabled():
        return None

    # Keep direct assets publicly shareable.
    if (
        request.path.startswith("/view/")
        or request.path.startswith("/thumb/")
        or request.path.startswith("/images/")
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
    # Search query for filtering items
    search_query = request.args.get('q', '').strip().lower()
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
            crumbs.append(f'<a href="/{path}">{part}</a>')
        crumbs.append(parts[-1])
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

    return render_template_string(
        HTML_TEMPLATE,
        items=items,
        breadcrumb=breadcrumb,
        parent_url=parent_url,
        stats=stats,
        current_subpath=safe_subpath,
        nav_crumbs=nav_crumbs,
        search_query=search_query,
        category_filter_active=bool(search_query and not safe_subpath),
        app_version=APP_VERSION,
        csrf=csrf_token(),
        theme_color=PWA_THEME_COLOR,
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

    moved_files = 0
    moved_folders = 0

    # Delete selected files
    for rel_path in selected_files:
        if not sanitize_path(rel_path):
            continue
        safe_rel_path = sanitize_rel_path(rel_path)
        file_path = source_file_path(safe_rel_path)
        if file_path.exists() and file_path.is_file():
            if move_to_trash(file_path, DATA_FOLDER):
                moved_files += 1
                remove_thumbnail_cache_for(safe_rel_path)

    # Delete selected folders
    for rel_path in selected_folders:
        if not sanitize_path(rel_path):
            continue
        safe_rel_path = sanitize_rel_path(rel_path)
        folder_path = DATA_FOLDER / safe_rel_path
        if folder_path.exists() and folder_path.is_dir():
            if move_to_trash(folder_path, DATA_FOLDER):
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

    return redirect(url_for("index", subpath=current_subpath))


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

    try:
        for item in DATA_FOLDER.rglob("*"):
            if not item.is_file() or not is_image(item):
                continue
            if is_excluded_from_recent(item):
                continue
            if item.name.startswith("."):
                continue
            try:
                mtime = item.stat().st_mtime
            except OSError:
                continue

            rel_path = item.relative_to(DATA_FOLDER).as_posix()
            date_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
            
            # Get folder path for navigation
            folder_path = os.path.dirname(rel_path)
            folder_url = url_for("index", subpath=folder_path) if folder_path else url_for("index")

            images.append({
                "name": item.name,
                "url": url_for("view", filename=rel_path),
                "thumb": url_for("thumb", filename=rel_path),
                "added": date_str,
                "mtime": mtime,
                "folder_url": folder_url,
            })
    except Exception:
        pass

    images.sort(key=lambda x: x["mtime"], reverse=True)
    images = images[:max_items]

    for img in images:
        del img["mtime"]

    return render_template_string(RECENT_TEMPLATE, items=images, theme_color=PWA_THEME_COLOR)


@app.route("/trash")
@require_auth
@rate_limit(max_requests=30, window=60)
def trash_view():
    items = list_trash(DATA_FOLDER)
    return render_template_string(TRASH_TEMPLATE, items=items, csrf=csrf_token(), theme_color=PWA_THEME_COLOR)


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
    next_url = request.args.get("next") or "/"

    error_code = (request.args.get("error") or "").strip().lower()
    error_map = {
        "invalid": "Invalid password. Please try again.",
        "oidc_failed": "OIDC login failed. Please try again.",
        "oidc_disabled": "OIDC is not configured.",
        "local_disabled": "Password login is disabled.",
    }
    error = error_map.get(error_code)

    return render_template_string(
        LOGIN_TEMPLATE,
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

    next_url = request.form.get("next") or request.args.get("next") or url_for("index")

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

    stats = run_thumbnail_integrity_check()
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
    if not _webhook_enabled():
        return {"error": "Webhook tasks are disabled"}, 404

    payload = request.get_json(silent=True) or {}
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
        timeout = max(1, min(600, int(os.environ.get("WEBHOOK_TASK_TIMEOUT", "30"))))
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
    log_security_event(
        "webhook_task",
        "success" if success else "error",
        task=task,
        exit_code=completed.returncode,
    )

    return {
        "task": task,
        "success": success,
        "exitCode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


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

    return render_template_string(
        SETTINGS_TEMPLATE,
        theme_color=PWA_THEME_COLOR,
        data_folder=str(DATA_FOLDER),
        thumb_cache=str(THUMBNAIL_CACHE_DIR),
        maintenance_result=maintenance_result,
        csrf=csrf_token(),
    )


@app.route("/about")
@require_auth
def about_view():
    return render_template_string(
        ABOUT_TEMPLATE,
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
    next_url = request.args.get("next") or request.referrer or url_for("index")
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

        # Extract user identifier (prefer email, fallback to sub)
        user_id = user_info.get("email") or user_info.get("sub")
        user_name = user_info.get("name") or user_info.get("preferred_username") or user_id

        if not user_id:
            return "Could not identify user from OIDC response", 400

        # Set session as permanent and authenticated
        session.permanent = True
        session["authenticated"] = True
        session["user_id"] = user_id
        session["user_name"] = user_name
        session["auth_method"] = "oidc"

        log_security_event("login", "success", auth_method="oidc")

        # Redirect to the stored next URL or index
        next_url = session.pop("oidc_next_url", None) or url_for("index")
        return redirect(next_url)

    except Exception as e:
        log_security_event("login", "error", auth_method="oidc", error=type(e).__name__)
        next_url = session.pop("oidc_next_url", None) or "/"
        return redirect(url_for("login", error="oidc_failed", next=next_url))


# Health routes
from health import health, storage_health, storage_health_read, storage_health_write

app.add_url_rule("/health", "health", health, methods=["GET"])
app.add_url_rule("/health/storage", "storage_health", storage_health, methods=["GET"])
app.add_url_rule("/health/storage/read", "storage_health_read", storage_health_read, methods=["GET"])
app.add_url_rule("/health/storage/write", "storage_health_write", storage_health_write, methods=["GET"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
