"""LLM REST API (/api/llm/*) extracted from app.py (issue #519).

The nine view functions keep their original endpoint names (``llm_images``,
``llm_image``, ``llm_recent``, ``llm_folders``, ``llm_tags``, ``llm_delete``,
``llm_bulk_delete``, ``llm_dedup``, ``llm_task_run``), URL paths, auth scopes,
and rate limits. They are registered onto the Flask app by
:func:`register_llm_routes`, which ``app.py`` calls with its
``log_security_event`` audit logger so this module stays free of app wiring
and importing ``app`` here never becomes circular.
"""

from __future__ import annotations

import os
from pathlib import Path

from flask import request

from auth import require_api_key_with_scope
from security import rate_limit, sanitize_path
from thumbnails import batch_remove_thumbnails, remove_thumbnail_cache_for
from trash import move_to_trash

# Destructive-operation guardrails for the LLM endpoints (issue #199)
LLM_BULK_DELETE_MAX_ITEMS = int(os.environ.get("LLM_BULK_DELETE_MAX_ITEMS", "500"))
LLM_DEDUP_MAX_REMOVALS = int(os.environ.get("LLM_DEDUP_MAX_REMOVALS", "200"))

# LLM enumeration endpoints (/api/llm/images, /api/llm/folders, /api/llm/recent)
# must search and page across the whole gallery, not just the first
# GALLERY_SCAN_LIMIT items (issue #423). They request this higher bound so a
# very large gallery cannot make a single request walk the tree unboundedly.
LLM_ENUMERATION_LIMIT = 50_000


def load_env_config() -> None:
    """(Re)read the LLM guardrail limits from the environment.

    Called once at import and again by ``app.py`` after importing the module so
    that reloading ``app`` re-reads ``LLM_BULK_DELETE_MAX_ITEMS`` /
    ``LLM_DEDUP_MAX_REMOVALS`` exactly as the pre-extraction module-level
    constants did.
    """
    global LLM_BULK_DELETE_MAX_ITEMS, LLM_DEDUP_MAX_REMOVALS
    LLM_BULK_DELETE_MAX_ITEMS = int(os.environ.get("LLM_BULK_DELETE_MAX_ITEMS", "500"))
    LLM_DEDUP_MAX_REMOVALS = int(os.environ.get("LLM_DEDUP_MAX_REMOVALS", "200"))


load_env_config()


def _app():
    """Return the live ``app`` module.

    The endpoints below rely on ``DATA_FOLDER`` and the gallery-scan/pagination
    helpers, the tag-store singleton, ``media_metadata``,
    ``find_duplicate_media`` and the configured-task runner, which stay in
    ``app.py`` (gallery-scan extraction is tracked separately). Resolving them
    through the module at call time keeps monkeypatching of ``app`` attributes
    effective and avoids a circular import.
    """
    import app

    return app


def register_llm_routes(app, log_security_event) -> None:
    """Register the /api/llm/* REST routes on ``app``.

    ``log_security_event`` is the app's structured audit logger; it is passed
    in (rather than imported from ``app``) to keep this module importable
    without the Flask app module.
    """

    @app.route("/api/llm/images")
    @require_api_key_with_scope("read")
    @rate_limit(max_requests=60, window=60)
    def llm_images():
        app_module = _app()
        query = request.args.get("q", "").strip().lower()
        page, per_page = app_module._parse_pagination(request.args)
        all_media = app_module.iter_gallery_items(kind="media", limit=LLM_ENUMERATION_LIMIT)
        filtered: list[Path] = []
        # Guard the post-walk relative_to() calls (issue #480): a path that no longer
        # resolves against DATA_FOLDER is dropped rather than raising ValueError and
        # returning 500 for the whole request.
        for item in all_media:
            rel_path = app_module._gallery_item_rel_path(item)
            if rel_path is None:
                continue
            if query and query not in rel_path.lower() and query not in item.name.lower():
                continue
            filtered.append(item)
        paginated, total, pg, pp, has_more = app_module._paginate(filtered, page=page, per_page=per_page)
        has_more = app_module._apply_scan_limit(has_more, len(all_media))
        # Re-resolve each page item; drop any that no longer resolve against
        # DATA_FOLDER (issue #480) so a vanished file cannot raise here.
        page_rel: dict[Path, str] = {}
        for item in paginated:
            rel_path = app_module._gallery_item_rel_path(item)
            if rel_path is not None:
                page_rel[item] = rel_path
        tags_by_path = app_module._tag_store().get_tags_for_paths(list(page_rel.values()))
        # Guard the per-item media_metadata() stat/relative_to calls (issue #480):
        # drop items that vanish between the walk and metadata so the page stays 200.
        images: list[dict[str, object]] = []
        for item in paginated:
            rel_path = page_rel.get(item)
            if rel_path is None:
                continue
            try:
                images.append(app_module.media_metadata(item, tags_by_path.get(rel_path, [])))
            except (OSError, ValueError):
                continue
        return {
            "images": images,
            "count": len(images),
            "total": total,
            "page": pg,
            "per_page": pp,
            "has_more": has_more,
        }

    @app.route("/api/llm/image/<path:relpath>")
    @require_api_key_with_scope("read")
    @rate_limit(max_requests=60, window=60)
    def llm_image(relpath: str):
        app_module = _app()
        if not sanitize_path(relpath):
            return {"error": "Invalid path"}, 400
        media_path = app_module.source_file_path(relpath)
        # Reject symlinks under DATA_FOLDER that resolve outside it (e.g.
        # foo.png -> /etc/passwd), mirroring the guard in folder_cover_rel_path().
        # This is defense-in-depth: is_media_file() also enforces the rule, so a
        # read-scoped LLM API key holder cannot obtain media_metadata() for an
        # arbitrary filesystem target (#447).
        if media_path.is_symlink():
            try:
                media_path.resolve().relative_to(app_module.DATA_FOLDER.resolve())
            except ValueError:
                return {"error": "Image not found"}, 404
        if not media_path.exists() or not app_module.is_media_file(media_path) or app_module.is_excluded_gallery_path(media_path):
            return {"error": "Image not found"}, 404
        return app_module.media_metadata(media_path)

    @app.route("/api/llm/recent")
    @require_api_key_with_scope("read")
    @rate_limit(max_requests=60, window=60)
    def llm_recent():
        app_module = _app()
        page, per_page = app_module._parse_pagination(request.args)
        # Guard the post-walk sort key (issue #480): iter_gallery_items swallows
        # OSError during the walk, but the sort's stat() runs afterwards, so a file
        # that vanished (or hit an NFS attr-cache blip) between the walk and the sort
        # raised an uncaught OSError, returning 500 for the whole request. Capture the
        # mtime in the guarded loop instead: a file whose stat raises here is dropped
        # and counted, and the sort uses the captured value so no second stat can race.
        all_media: list[tuple[float, Path]] = []
        skipped = 0
        for item in app_module.iter_gallery_items(kind="media", limit=LLM_ENUMERATION_LIMIT):
            try:
                mtime = item.stat().st_mtime
            except (OSError, ValueError):
                skipped += 1
                continue
            all_media.append((mtime, item))
        all_media.sort(key=lambda entry: entry[0], reverse=True)
        paginated, total, pg, pp, has_more = app_module._paginate(all_media, page=page, per_page=per_page)
        has_more = app_module._apply_scan_limit(has_more, len(all_media))
        # Re-resolve each page item; drop any that no longer resolve against
        # DATA_FOLDER (issue #480) so a vanished file cannot raise here.
        page_rel: dict[Path, str] = {}
        for _mtime, item in paginated:
            rel_path = app_module._gallery_item_rel_path(item)
            if rel_path is not None:
                page_rel[item] = rel_path
        tags_by_path = app_module._tag_store().get_tags_for_paths(list(page_rel.values()))
        # Guard the per-item media_metadata() stat/relative_to calls (issue #480):
        # drop items that vanish between the walk and metadata so the page stays 200.
        images: list[dict[str, object]] = []
        for _mtime, item in paginated:
            rel_path = page_rel.get(item)
            if rel_path is None:
                continue
            try:
                images.append(app_module.media_metadata(item, tags_by_path.get(rel_path, [])))
            except (OSError, ValueError):
                continue
        return {
            "images": images,
            "count": len(images),
            "total": total,
            "page": pg,
            "per_page": pp,
            "has_more": has_more,
            "skipped_count": skipped,
        }

    @app.route("/api/llm/folders")
    @require_api_key_with_scope("read")
    @rate_limit(max_requests=60, window=60)
    def llm_folders():
        app_module = _app()
        page, per_page = app_module._parse_pagination(request.args)
        all_folders = [{"rel_path": "", "name": "", "parent": None}]
        # Guard the post-walk relative_to() calls (issue #480): folder.relative_to and
        # its parent's relative_to raise ValueError if the directory is no longer
        # relative to DATA_FOLDER (e.g. moved or replaced by a symlink mid-walk).
        # Without this guard a single such folder returns 500 for the whole request;
        # skip it and continue with the rest of the page instead.
        for folder in app_module.iter_gallery_items(kind="folders", limit=LLM_ENUMERATION_LIMIT):
            try:
                rel_path = folder.relative_to(app_module.DATA_FOLDER).as_posix()
                parent = (
                    ""
                    if folder.parent == app_module.DATA_FOLDER
                    else folder.parent.relative_to(app_module.DATA_FOLDER).as_posix()
                )
            except (OSError, ValueError):
                continue
            all_folders.append({"rel_path": rel_path, "name": folder.name, "parent": parent})
        paginated, total, pg, pp, has_more = app_module._paginate(all_folders, page=page, per_page=per_page)
        # Exclude the synthetic root entry ({"rel_path": "", ...}) from the
        # user-visible total/has_more counts — it is a navigation helper, not a
        # real folder. Without this, ``total`` would include the root while
        # ``has_more`` already excluded it (``len(all_folders) - 1``), so under
        # heavy mid-walk churn — e.g. every walked folder unreadable — the
        # response reported ``total=1`` (just the root) alongside ``has_more``
        # math that used 0 visible folders, misleading machine clients
        # (issue #480 review).
        visible_count = len(all_folders) - 1  # -1 for synthetic root entry
        return {
            "folders": paginated,
            "count": len(paginated),
            "total": visible_count,
            "page": pg,
            "per_page": pp,
            "has_more": app_module._apply_scan_limit(has_more, visible_count),
        }

    @app.route("/api/llm/tags", methods=["POST"])
    @require_api_key_with_scope("write")
    @rate_limit(max_requests=30, window=60)
    def llm_tags():
        app_module = _app()
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
        normalized_tags = sorted({str(tag).strip() for tag in tags if str(tag).strip()})
        store = app_module._tag_store()
        updated = []
        for rel_path in rel_paths:
            if not isinstance(rel_path, str) or not sanitize_path(rel_path):
                continue
            safe_rel_path = app_module.sanitize_rel_path(rel_path)
            media_path = app_module.source_file_path(safe_rel_path)
            if media_path.exists() and app_module.is_media_file(media_path) and not app_module.is_excluded_gallery_path(media_path):
                if action == "remove":
                    store.remove_tags(safe_rel_path, normalized_tags)
                else:
                    store.add_tags(safe_rel_path, normalized_tags)
                updated.append(safe_rel_path)
        log_security_event("llm_tags", "success", action=action, updated=len(updated), tags=normalized_tags)
        return {"status": "ok", "action": action, "updated": updated, "tags": normalized_tags}

    @app.route("/api/llm/delete", methods=["POST"])
    @require_api_key_with_scope("write")
    @rate_limit(max_requests=20, window=60)
    def llm_delete():
        app_module = _app()
        payload = request.get_json(silent=True) or {}
        rel_path = str(payload.get("rel_path") or payload.get("image") or "")
        if not rel_path or not sanitize_path(rel_path):
            return {"error": "Valid rel_path is required"}, 400
        safe_rel_path = app_module.sanitize_rel_path(rel_path)
        media_path = app_module.source_file_path(safe_rel_path)
        if not media_path.exists() or not app_module.is_media_file(media_path) or app_module.is_excluded_gallery_path(media_path):
            return {"error": "Image not found"}, 404
        dry_run = bool(payload.get("dry_run", False))
        moved = False
        if not dry_run:
            moved = move_to_trash(media_path, app_module.DATA_FOLDER)
            if moved:
                remove_thumbnail_cache_for(safe_rel_path)
        log_security_event("llm_delete", "success" if (moved and not dry_run) else "dry_run", target=safe_rel_path, dry_run=dry_run)
        app_module._invalidate_gallery_scan_cache()
        return {"deleted": moved if not dry_run else True, "rel_path": safe_rel_path, "dry_run": dry_run}, 200

    @app.route("/api/llm/bulk-delete", methods=["POST"])
    @require_api_key_with_scope("write")
    @rate_limit(max_requests=10, window=60)
    def llm_bulk_delete():
        app_module = _app()
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
        purged_thumbnails: list[str] = []
        for rel_path in rel_paths:
            if not isinstance(rel_path, str) or not sanitize_path(rel_path):
                log_security_event("llm_bulk_delete", "denied", reason="invalid_path", rel_path=str(rel_path))
                skipped.append(str(rel_path))
                continue
            safe_rel_path = app_module.sanitize_rel_path(rel_path)
            media_path = app_module.source_file_path(safe_rel_path)
            if not media_path.exists() or not app_module.is_media_file(media_path) or app_module.is_excluded_gallery_path(media_path):
                skipped.append(safe_rel_path)
                continue
            if dry_run:
                deleted.append(safe_rel_path)
            elif move_to_trash(media_path, app_module.DATA_FOLDER):
                purged_thumbnails.append(safe_rel_path)
                deleted.append(safe_rel_path)
            else:
                skipped.append(safe_rel_path)
        if purged_thumbnails:
            batch_remove_thumbnails(purged_thumbnails)
        log_security_event("llm_bulk_delete", "success" if (deleted and not dry_run) else "dry_run", deleted=len(deleted), skipped=len(skipped), dry_run=dry_run)
        app_module._invalidate_gallery_scan_cache()
        return {"deleted": deleted, "skipped": skipped, "deleted_count": len(deleted), "skipped_count": len(skipped), "dry_run": dry_run}

    @app.route("/api/llm/dedup", methods=["POST"])
    @require_api_key_with_scope("write")
    @rate_limit(max_requests=5, window=60)
    def llm_dedup():
        app_module = _app()
        payload = request.get_json(silent=True) or {}
        remove = bool(payload.get("remove") or payload.get("delete"))
        try:
            limit = max(1, min(100, int(payload.get("limit", "0") or "0"))) if payload.get("limit") else None
        except (ValueError, TypeError):
            limit = None
        groups = app_module.find_duplicate_media()
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
                    media_path = app_module.source_file_path(str(rel_path))
                    if media_path.exists() and move_to_trash(media_path, app_module.DATA_FOLDER):
                        removed.append(str(rel_path))
            if removed:
                batch_remove_thumbnails([str(r) for r in removed])
        log_security_event("llm_dedup", "success", groups=len(groups), removed=len(removed), dry_run=not remove)
        if remove:
            return {"duplicate_groups": groups, "group_count": len(groups), "dry_run": False, "removed": removed, "deleted_count": len(removed)}
        return {"duplicate_groups": groups, "group_count": len(groups), "dry_run": True, "skipped_count": 0, "deleted_count": 0}

    @app.route("/api/llm/task/run", methods=["POST"])
    @require_api_key_with_scope("write")
    @rate_limit(max_requests=10, window=60)
    def llm_task_run():
        app_module = _app()
        data = request.get_json(silent=True) or {}
        task = data.get("task", "")
        if task:
            validation_error = app_module._validate_task_name(task)
            if validation_error is not None:
                return {"error": validation_error}, 400
        body, status = app_module.run_configured_task(data)
        app_module._invalidate_gallery_scan_cache()
        return body, status
