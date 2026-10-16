"""Gallery scanning, media metadata, pagination, and the scan cache.

Extracted from ``app.py`` (issue #521) following the sibling-module pattern
used by ``thumbnails.py`` / ``llm_api.py``.

This module resolves ``DATA_FOLDER``, ``THUMBNAIL_CACHE_DIR``, ``TAG_DATABASE``,
``IMAGE_EXTENSIONS``/``VIDEO_EXTENSIONS`` and the gallery scan/pagination
constants through the live ``app`` module at call time (see ``_app()`` below):
``app.py`` re-imports every symbol defined here, so ``app.X`` stays the single
patch surface the routes, ``llm_api``/``thumbnails`` and the test suite use
(``monkeypatch.setattr(app, "GALLERY_SCAN_LIMIT", ...)`` and friends must keep
working exactly as before the move). The scan cache dict itself is module
local; ``app._gallery_scan_cache`` re-exports the identical object, so the
mutation-based test interactions (``clear()`` / item assignment) behave as
before. ``tests/conftest.py`` re-imports this module together with ``app`` so
environment-derived constants, the cache, and the ``_tag_store`` singleton are
rebuilt per test bootstrap, as they were when all of this lived in ``app.py``.
"""

from __future__ import annotations

import hashlib
import heapq
import os
import stat
import time
from collections import OrderedDict
from pathlib import Path

from flask import abort, url_for

from tag_store import TagStore


def _app():
    """Return the live ``app`` module.

    The helpers below rely on ``DATA_FOLDER``, ``THUMBNAIL_CACHE_DIR``,
    ``TAG_DATABASE``, ``IMAGE_EXTENSIONS``/``VIDEO_EXTENSIONS`` and the
    constants re-exported into ``app.py`` below. Resolving them through the
    module at call time keeps monkeypatching of ``app`` attributes effective
    and avoids a circular import.
    """
    import app

    return app


# Bounded pagination defaults for gallery endpoints
GALLERY_PAGE_DEFAULT = 50
GALLERY_PAGE_MAX = 500
GALLERY_SCAN_LIMIT = int(os.environ.get("GALLERY_SCAN_LIMIT", "5000"))

# Shared filesystem scan cache (TTL-based) to avoid redundant rglob passes
GALLERY_SCAN_CACHE_TTL = max(int(os.environ.get("GALLERY_SCAN_CACHE_TTL", "10") or 10), 1)
# LRU cap on distinct (kind, limit, root) keys so repeated scans of many
# subfolders/limits cannot retain memory indefinitely (issue #420).
GALLERY_SCAN_CACHE_MAX_ENTRIES = 256
_gallery_scan_cache: OrderedDict[str, tuple[float, object]] = OrderedDict()


def _paginate(items, page=1, per_page=GALLERY_PAGE_DEFAULT):
    """Return (paginated_items, total_count, page, per_page, has_more)."""
    total = len(items)
    start = (page - 1) * per_page
    end = start + per_page
    paginated = items[start:end]
    has_more = end < total
    return paginated, total, page, per_page, has_more


def _apply_scan_limit(has_more: bool, scanned_count: int) -> bool:
    """More results may exist beyond the page when the scan hit GALLERY_SCAN_LIMIT."""
    return has_more or scanned_count >= _app().GALLERY_SCAN_LIMIT


def _parse_pagination(args):
    """Parse pagination query params from request.args. Returns (page, per_page)."""
    app = _app()
    try:
        page = max(1, int(args.get("page", "1")))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = max(1, min(app.GALLERY_PAGE_MAX, int(args.get("limit", str(app.GALLERY_PAGE_DEFAULT)))))
    except (ValueError, TypeError):
        per_page = app.GALLERY_PAGE_DEFAULT
    return page, per_page


def sanitize_rel_path(rel_path: str) -> str:
    normalized = os.path.normpath(rel_path).replace("\\", "/").lstrip("/")
    if normalized.startswith(".."):
        abort(404)
    return normalized


def source_file_path(rel_path: str) -> Path:
    return _app().DATA_FOLDER / sanitize_rel_path(rel_path)


def format_size(size: int) -> str:
    value = float(size)
    for unit in ["B", "KB", "MB", "GB"]:
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def is_media_file(path: Path) -> bool:
    app = _app()
    if not path.is_file() or path.suffix.lower() not in app.IMAGE_EXTENSIONS + app.VIDEO_EXTENSIONS:
        return False
    # Reject symlinks that resolve outside DATA_FOLDER (e.g. foo.png -> /etc/passwd).
    # Symlinks that stay inside DATA_FOLDER are still served.
    try:
        path.resolve().relative_to(app.DATA_FOLDER.resolve())
    except ValueError:
        return False
    return True


def is_excluded_gallery_path(path: Path) -> bool:
    app = _app()
    try:
        rel_parts = path.relative_to(app.DATA_FOLDER).parts
    except ValueError:
        return True
    return any(part in {app.THUMBNAIL_CACHE_DIR.name, ".trash"} or part.startswith(".") for part in rel_parts)


def _gallery_item_rel_path(item: Path) -> str | None:
    """Gallery-relative POSIX path for a walked item, or ``None`` if it is
    no longer usable.

    ``iter_gallery_items`` swallows ``OSError``/``PermissionError`` *during* the
    bounded walk, but the post-walk steps (the ``/api/llm/recent`` sort key, the
    ``/api/llm/images`` ``media_metadata`` page, and the ``/api/llm/folders``
    ``relative_to`` calls) run after the walk has returned. A file that vanished
    or became unreadable between the walk and those steps raises ``OSError`` or
    ``ValueError`` from ``Path.stat``/``relative_to`` — which, unguarded, returns
    500 for the whole request even though only one file is bad. These helpers
    let callers drop the unreadable item instead (issue #480).
    """
    try:
        return item.relative_to(_app().DATA_FOLDER).as_posix()
    except (OSError, ValueError):
        return None


def _tag_store() -> TagStore:
    """Get or create the singleton TagStore instance.

    Caches the TagStore at module scope so only one SQLite connection is opened
    per process, avoiding WAL contention and file-descriptor churn under
    Gunicorn multi-worker deployments.
    """
    if not hasattr(_tag_store, "_instance"):
        _tag_store._instance = TagStore(_app().TAG_DATABASE)
    return _tag_store._instance


def media_metadata(path: Path, tags: list[str] | None = None) -> dict[str, object]:
    app = _app()
    rel_path = path.relative_to(app.DATA_FOLDER).as_posix()
    file_stat = path.stat()
    media_type = "video" if path.suffix.lower() in app.VIDEO_EXTENSIONS else "image"
    return {
        "name": path.name,
        "rel_path": rel_path,
        "media_type": media_type,
        "tags": _tag_store().get_tags(rel_path) if tags is None else tags,
        "size": file_stat.st_size,
        "size_human": format_size(file_stat.st_size),
        "modified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(file_stat.st_mtime)),
        "mtime": file_stat.st_mtime,
        "view_url": url_for("view", filename=rel_path),
        "thumb_url": url_for("thumb", filename=rel_path),
    }



def _invalidate_gallery_scan_cache() -> None:
    """Clear the shared filesystem scan cache.

    Call this after any write operation (delete, upload, tag, move) so that
    subsequent reads re-scan the gallery directory tree instead of returning a
    stale snapshot.
    """
    _gallery_scan_cache.clear()


# The HTML /recent page has the same bounded-enumeration problem as the LLM
# endpoints (issue #436): it must show the globally newest media even when
# the newest file lives beyond the first GALLERY_SCAN_LIMIT items in the
# recursive traversal. We request this higher bound for the same reason as
# llm_api.LLM_ENUMERATION_LIMIT, so a single request walks at most this many files.
RECENT_ENUMERATION_LIMIT = 50_000


def iter_gallery_items(
    kind: str = "media",
    limit: int | None = None,
    root: Path | None = None,
) -> list[Path]:
    """Centralized bounded iterator for gallery filesystem scans.

    Replaces duplicated rglob patterns across iter_gallery_items (kind="media"),
    iter_gallery_items (kind="folders"), folder_cover_rel_path, and recent_view.

    Results are cached per-process with a short TTL (GALLERY_SCAN_CACHE_TTL)
    so repeated scans within the window reuse a snapshot instead of re-walking
    the entire gallery directory tree. Small-limit queries (e.g. folder covers
    with limit=1) bypass the cache to maintain immediate consistency when files
    are added or deleted directly on disk.

    Args:
        kind: "media" (files only), "folders" (dirs only), or "all" (both).
        limit: Maximum items to return. Defaults to GALLERY_SCAN_LIMIT.
        root: Optional root directory to scan. Defaults to DATA_FOLDER.

    Returns:
        Sorted list of Path objects within the bound.
    """
    app = _app()
    effective_limit = limit if limit is not None else app.GALLERY_SCAN_LIMIT
    scan_root = root if root is not None else app.DATA_FOLDER

    # Cache key includes kind, limit, and root so each unique query is cached separately.
    cache_key = (kind, effective_limit, str(scan_root))

    # Check cache (skip for small limits to maintain immediate consistency)
    now = time.time()
    if effective_limit > 1:
        cached = _gallery_scan_cache.get(cache_key)
        if cached is not None:
            ts, cached_results = cached
            if now - ts < app.GALLERY_SCAN_CACHE_TTL:
                # Refresh LRU position so hot keys survive eviction.
                _gallery_scan_cache.move_to_end(cache_key)
                return cached_results
            # Expired: drop the stale entry rather than letting it linger.
            del _gallery_scan_cache[cache_key]

    results: list[Path] = []
    # Collect first, sort second, then truncate. Collecting before truncating is
    # required so a small limit (e.g. folder covers asking for a single image)
    # returns the sorted-first item rather than an arbitrary filesystem-ordered
    # prefix. The collection cap is the scan safety bound (GALLERY_SCAN_LIMIT);
    # the requested limit only constrains how many we return.
    # The collection cap is the larger of the requested limit and the scan
    # safety bound, so callers that ask for more than GALLERY_SCAN_LIMIT
    # (e.g. the LLM enumeration endpoints, issue #423) are not silently
    # truncated to the safety bound.
    collection_cap = max(effective_limit, app.GALLERY_SCAN_LIMIT)
    for item in scan_root.rglob("*"):
        if len(results) >= collection_cap:
            break
        try:
            # Skip symlinks to prevent filesystem traversal outside the data root.
            if item.is_symlink():
                continue
            if kind == "media" and not (item.is_file() and is_media_file(item)):
                continue
            if kind == "folders" and not item.is_dir():
                continue
            # "all" accepts everything
            if is_excluded_gallery_path(item):
                continue
            results.append(item)
        except (OSError, PermissionError):
            continue
    sorted_results = sorted(results, key=lambda p: p.relative_to(app.DATA_FOLDER).as_posix().lower())[:effective_limit]

    # Store in cache (only for larger queries), evicting the oldest entry
    # once the cap is exceeded so the cache stays bounded (issue #420).
    if effective_limit > 1:
        _gallery_scan_cache[cache_key] = (now, sorted_results)
        while len(_gallery_scan_cache) > app.GALLERY_SCAN_CACHE_MAX_ENTRIES:
            _gallery_scan_cache.popitem(last=False)
    return sorted_results


def recent_gallery_entries(
    max_items: int,
    root: Path | None = None,
) -> tuple[list[tuple[str, float, int]], bool]:
    """Return the globally newest media as (rel_path, mtime, size), newest first.

    Backs the HTML /recent page (issue #436). Unlike iter_gallery_items +
    per-path Path.stat(), this walk costs a single lstat per file and
    heap-selects the newest max_items entries during the walk instead of
    materialising the whole enumeration, so large galleries stay cheap to
    serve. The walk is safety-bounded by RECENT_ENUMERATION_LIMIT media files
    and applies the same exclusions as iter_gallery_items (symlinks,
    dot-files/dot-dirs, .thumb_cache, .trash).

    Args:
        max_items: Number of newest entries to return.
        root: Optional root directory to scan. Defaults to DATA_FOLDER.

    Returns:
        (entries, scan_truncated) where scan_truncated is True only when the
        walk stopped at RECENT_ENUMERATION_LIMIT while more media remained
        (Issue #349). Results are cached in _gallery_scan_cache, sharing the
        TTL and the _invalidate_gallery_scan_cache() invalidation with
        iter_gallery_items.
    """
    app = _app()
    scan_root = root if root is not None else app.DATA_FOLDER
    max_items = max(1, max_items)
    cache_key = ("recent", max_items, str(scan_root))
    now = time.time()
    cached = _gallery_scan_cache.get(cache_key)
    if cached is not None:
        ts, payload = cached
        if now - ts < app.GALLERY_SCAN_CACHE_TTL:
            _gallery_scan_cache.move_to_end(cache_key)
            return payload
        del _gallery_scan_cache[cache_key]

    # Media suffix lookup; IMAGE_EXTENSIONS and VIDEO_EXTENSIONS are already
    # lowercase. Built once per (uncached) walk from the live app attributes.
    media_suffixes = frozenset(app.IMAGE_EXTENSIONS + app.VIDEO_EXTENSIONS)
    pruned_dir_names = {app.THUMBNAIL_CACHE_DIR.name, ".trash"}
    data_root = str(scan_root)
    # Min-heap of (mtime, lowercase rel path, rel path, size): the root is the
    # worst kept entry (oldest, ties by path), so a newer candidate evicts it.
    # The lowercase rel path as second field reproduces iter_gallery_items'
    # path ordering as the mtime tie-break.
    heap: list[tuple[float, str, str, int]] = []
    scanned = 0
    scan_truncated = False

    for dirpath, dirnames, filenames in os.walk(data_root):
        # Prune excluded directories instead of walking into them and
        # filtering per entry — everything below them is excluded too, so
        # this matches is_excluded_gallery_path while skipping whole subtrees.
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in pruned_dir_names]
        for name in filenames:
            if scanned >= app.RECENT_ENUMERATION_LIMIT:
                # A further media file exists beyond the bound: the newest
                # items may hide past it (Issue #349).
                scan_truncated = True
                break
            if name.startswith(".") or name in pruned_dir_names:
                continue
            if os.path.splitext(name)[1].lower() not in media_suffixes:
                continue
            entry_path = os.path.join(dirpath, name)
            try:
                # Single lstat per file: the mode also rejects symlinks
                # (including to-file links that escape DATA_FOLDER), FIFOs
                # and sockets, matching the symlink skip in iter_gallery_items.
                st = os.lstat(entry_path)
            except OSError:
                # Vanished mid-walk / NFS attribute cache inconsistency.
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            scanned += 1
            rel = os.path.relpath(entry_path, data_root).replace(os.sep, "/")
            key = (st.st_mtime, rel.lower())
            if len(heap) < max_items:
                heapq.heappush(heap, (*key, rel, st.st_size))
            elif key > heap[0][:2]:
                heapq.heapreplace(heap, (*key, rel, st.st_size))
        else:
            continue
        break  # scan bound hit: stop the walk entirely

    entries = [
        (rel, mtime, size)
        for mtime, _order, rel, size in sorted(heap, key=lambda item: (-item[0], item[1]))
    ]
    payload = (entries, scan_truncated)
    _gallery_scan_cache[cache_key] = (now, payload)
    while len(_gallery_scan_cache) > app.GALLERY_SCAN_CACHE_MAX_ENTRIES:
        _gallery_scan_cache.popitem(last=False)
    return payload


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    handle = path.open("rb")
    try:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    finally:
        handle.close()
    return digest.hexdigest()


def find_duplicate_media(limit: int | None = None) -> list[dict[str, object]]:
    """Find duplicate media files by size then SHA-256 hash.

    Args:
        limit: Maximum number of duplicate groups to return. Defaults to GALLERY_SCAN_LIMIT.
               Applied only to the final output (hash phase is bounded by iter_gallery_items(kind="media")).
    """
    app = _app()
    effective_limit = limit if limit is not None else app.GALLERY_SCAN_LIMIT
    by_size: dict[int, list[Path]] = {}
    for item in iter_gallery_items(kind="media"):
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
            if len(groups) >= effective_limit:
                return groups
            matches = sorted(matches, key=lambda p: p.relative_to(app.DATA_FOLDER).as_posix().lower())
            groups.append(
                {
                    "hash": digest,
                    "size": matches[0].stat().st_size,
                    "keep": matches[0].relative_to(app.DATA_FOLDER).as_posix(),
                    "duplicates": [p.relative_to(app.DATA_FOLDER).as_posix() for p in matches[1:]],
                    "all": [p.relative_to(app.DATA_FOLDER).as_posix() for p in matches],
                }
            )
    return groups
