from __future__ import annotations

import contextlib
import os
import time
from collections import OrderedDict
from pathlib import Path

from PIL import Image, UnidentifiedImageError

THUMBNAIL_MAX_SIZE = 400

AUTO_FOLDER_COVERS_ENABLED = False
FOLDER_COVER_CACHE_TTL = 3600
_FOLDER_COVER_CACHE: OrderedDict[str, tuple[float, str | None]] = OrderedDict()
_FOLDER_COVER_MAX_SIZE = 1024


def load_env_config() -> None:
    """(Re)read folder-cover settings from the environment.

    Called once at import and again by ``app.py`` after importing the module so
    that reloading ``app`` re-reads ``GALLERY_AUTO_FOLDER_COVERS`` /
    ``GALLERY_COVER_CACHE_TTL`` exactly as the pre-extraction module-level
    constants did.
    """
    global AUTO_FOLDER_COVERS_ENABLED, FOLDER_COVER_CACHE_TTL
    AUTO_FOLDER_COVERS_ENABLED = os.environ.get("GALLERY_AUTO_FOLDER_COVERS", "false").strip().lower() in {"1", "true", "yes", "on"}
    FOLDER_COVER_CACHE_TTL = max(int(os.environ.get("GALLERY_COVER_CACHE_TTL", "3600") or 3600), 0)


load_env_config()


def _app():
    """Return the live ``app`` module.

    The helpers below rely on ``DATA_FOLDER``, ``THUMBNAIL_CACHE_DIR``,
    ``IMAGE_EXTENSIONS``, ``GALLERY_SCAN_LIMIT``, ``sanitize_rel_path`` and
    ``iter_gallery_items``, which stay in ``app.py`` (gallery-scan extraction is
    tracked separately). Resolving them through the module at call time keeps
    monkeypatching of ``app`` attributes effective and avoids a circular import.
    """
    import app

    return app


def ensure_thumbnail_cache_dir() -> None:
    _app().THUMBNAIL_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def thumbnail_filename(rel_path: str, source_path: Path) -> str:
    file_stat = source_path.stat()
    safe_name = rel_path.replace("/", "__")
    return f"{safe_name}.{file_stat.st_mtime_ns}.{file_stat.st_size}.jpg"


def generate_thumbnail(source_path: Path, output_path: Path) -> None:
    with Image.open(source_path) as img:
        img = img.convert("RGB")
        img.thumbnail((THUMBNAIL_MAX_SIZE, THUMBNAIL_MAX_SIZE), Image.Resampling.LANCZOS)
        img.save(output_path, format="JPEG", quality=85, optimize=True)


def batch_remove_thumbnails(rel_paths: list[str]) -> None:
    """Remove all cached thumbnails matching any of the given rel_paths in a single dir walk.

    Replaces per-path loops (issue #249). One call == one `iterdir()` regardless
    of how many paths are passed.
    """
    if not rel_paths:
        return
    app = _app()
    ensure_thumbnail_cache_dir()
    prefixes = {
        app.sanitize_rel_path(rel_path).replace("/", "__") + "."
        for rel_path in rel_paths
    }
    for cached_file in app.THUMBNAIL_CACHE_DIR.iterdir():
        if cached_file.is_symlink():
            # #445: never unlink through a symlink (a stale bind-mount / NFS
            # target would be deleted as the app user).
            continue
        name = cached_file.name
        if any(name.startswith(prefix) for prefix in prefixes):
            with contextlib.suppress(OSError):
                cached_file.unlink()


def remove_thumbnail_cache_for(rel_path: str) -> None:
    """Remove cached thumbnails for a single rel_path.

    Thin wrapper over batch_remove_thumbnails so existing single-path callers
    keep working; bulk callers should call batch_remove_thumbnails directly.
    """
    batch_remove_thumbnails([rel_path])


def run_thumbnail_integrity_check(limit: int | None = None) -> dict[str, int]:
    """Check thumbnails and regenerate missing/invalid entries on demand.

    Args:
        limit: Maximum files to scan. Defaults to GALLERY_SCAN_LIMIT.
    """
    app = _app()
    effective_limit = limit if limit is not None else app.GALLERY_SCAN_LIMIT

    ensure_thumbnail_cache_dir()
    excluded_dirs = {app.THUMBNAIL_CACHE_DIR.name, ".trash"}
    stats = {"checked": 0, "regenerated": 0, "failed": 0}

    for item in app.DATA_FOLDER.rglob("*"):
        if item.is_symlink():
            # #445: never generate a thumbnail from a symlink target
            # (arbitrary file content outside DATA_FOLDER).
            continue
        if not item.is_file() or item.suffix.lower() not in app.IMAGE_EXTENSIONS:
            continue
        if item.name.startswith("."):
            continue

        rel_path = item.relative_to(app.DATA_FOLDER)
        if any(part in excluded_dirs or part.startswith(".") for part in rel_path.parts):
            continue

        rel_posix = rel_path.as_posix()
        stats["checked"] += 1
        if stats["checked"] > effective_limit:
            break

        cached_name = thumbnail_filename(rel_posix, item)
        cached_path = app.THUMBNAIL_CACHE_DIR / cached_name

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

    app = _app()
    now = time.time()
    cached = _FOLDER_COVER_CACHE.get(folder_rel_path)
    if cached and now - cached[0] < FOLDER_COVER_CACHE_TTL:
        # Move to end (most recently used) for LRU ordering.
        _FOLDER_COVER_CACHE.move_to_end(folder_rel_path)
        cached_rel = cached[1]
        if cached_rel is None:
            # Short-circuit: folder was previously scanned and had no media.
            return None
        # Re-validate the backing file still exists and resolves within DATA_FOLDER.
        try:
            cached_path = (app.DATA_FOLDER / app.sanitize_rel_path(cached_rel)).resolve()
            cached_path.relative_to(app.DATA_FOLDER.resolve())
        except ValueError:
            # Resolved path escapes DATA_FOLDER (e.g. symlink) — treat as missing.
            _FOLDER_COVER_CACHE[folder_rel_path] = (now, None)
            return None
        if cached_path.exists() and cached_path.is_file() and cached_path.suffix.lower() in app.IMAGE_EXTENSIONS:
            return cached_rel
        # Stale entry — fall through to re-scan.

    folder_path = app.DATA_FOLDER / app.sanitize_rel_path(folder_rel_path) if folder_rel_path else app.DATA_FOLDER
    if not folder_path.exists() or not folder_path.is_dir():
        _FOLDER_COVER_CACHE[folder_rel_path] = (now, None)
        while len(_FOLDER_COVER_CACHE) > _FOLDER_COVER_MAX_SIZE:
            _FOLDER_COVER_CACHE.popitem(last=False)
        return None

    # Delegate to iter_gallery_items for bounded, exclusion-aware scanning.
    items = app.iter_gallery_items(kind="media", limit=1, root=folder_path)

    if not items:
        _FOLDER_COVER_CACHE[folder_rel_path] = (now, None)
        while len(_FOLDER_COVER_CACHE) > _FOLDER_COVER_MAX_SIZE:
            _FOLDER_COVER_CACHE.popitem(last=False)
        return None

    # Validate the discovered cover resolves within DATA_FOLDER.
    try:
        resolved = items[0].resolve()
        resolved.relative_to(app.DATA_FOLDER.resolve())
    except ValueError:
        # Symlink or other escape — treat as no cover.
        _FOLDER_COVER_CACHE[folder_rel_path] = (now, None)
        while len(_FOLDER_COVER_CACHE) > _FOLDER_COVER_MAX_SIZE:
            _FOLDER_COVER_CACHE.popitem(last=False)
        return None

    cover_rel = items[0].relative_to(app.DATA_FOLDER).as_posix()
    _FOLDER_COVER_CACHE[folder_rel_path] = (now, cover_rel)
    while len(_FOLDER_COVER_CACHE) > _FOLDER_COVER_MAX_SIZE:
        _FOLDER_COVER_CACHE.popitem(last=False)
    return cover_rel
