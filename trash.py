from __future__ import annotations

import contextlib
import json
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

TRASH_DIR_NAME = ".trash"
META_SUFFIX = ".meta.json"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _from_timestamp(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _parse_deleted_at(raw: str) -> datetime:
    """Parse a stored deleted_at timestamp, treating naive values as UTC.

    Legacy meta files (written before timezone-aware timestamps) lack an
    offset; normalize them so comparisons stay consistent.
    """
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _unique_trash_dest(td: Path, name: str, ts: int) -> Path:
    """Pick a non-clashing destination path under the trash dir."""
    dest = td / f"{ts}_{name}"
    i = 1
    while dest.exists():
        dest = td / f"{ts}_{i}_{name}"
        i += 1
    return dest


def dir_size(path: Path) -> int:
    """Estimate total size of a directory tree in bytes.

    Skips symlinks to prevent information disclosure and DoS via symlink
    cycles or external filesystem traversal.
    """
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_symlink():
                continue
            if entry.is_file():
                with contextlib.suppress(OSError):
                    total += entry.stat().st_size
                # Intentionally suppress OSError (e.g., permission denied) for
                # individual files during size estimation; we only need an estimate.
    except OSError:
        pass
    return total


def trash_dir(data_folder: Path) -> Path:
    path = data_folder / TRASH_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _meta_path(item_path: Path) -> Path:
    return item_path.with_name(item_path.name + META_SUFFIX)


def move_to_trash(file_path: Path, data_folder: Path) -> bool:
    """Move a file or directory to the trash.

    Returns True on success, False if the path does not exist or is inaccessible.
    For directories, the entire tree is moved atomically via rename.
    """
    if not file_path.exists():
        return False

    td = trash_dir(data_folder)
    rel = file_path.relative_to(data_folder).as_posix()
    ts = int(time.time())

    if file_path.is_file():
        dest = _unique_trash_dest(td, file_path.name, ts)
        try:
            file_path.rename(dest)
        except OSError:
            return False
    elif file_path.is_dir():
        dest = _unique_trash_dest(td, file_path.name, ts)
        try:
            # Prefer atomic rename (same filesystem) for immediate move.
            # Fall back to copytree+rmtree only when cross-device rename fails.
            try:
                file_path.rename(dest)
            except OSError:
                shutil.copytree(file_path, dest)
                shutil.rmtree(file_path)
        except OSError:
            # Clean up partial copy on failure
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            return False
    else:
        return False

    try:
        meta = {
            "original": rel,
            "deleted_at": _utcnow().isoformat(),
            "size": dir_size(dest) if dest.is_dir() else dest.stat().st_size,
        }
        _meta_path(dest).write_text(json.dumps(meta))
        return True
    except OSError:
        return False


def list_trash(data_folder: Path) -> list[dict]:
    td = trash_dir(data_folder)
    out = []
    for item in td.iterdir():
        if item.name.endswith(META_SUFFIX):
            continue
        meta_file = _meta_path(item)
        meta = {}
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
            except Exception:
                meta = {}
        size = dir_size(item) if item.is_dir() else item.stat().st_size
        out.append(
            {
                "name": item.name,
                "original": meta.get("original", "unknown"),
                "deleted_at": meta.get("deleted_at", _from_timestamp(item.stat().st_mtime).isoformat()),
                "size": size,
            }
        )
    out.sort(key=lambda x: x["deleted_at"], reverse=True)
    return out


def restore_from_trash(item_name: str, data_folder: Path) -> bool:
    td = trash_dir(data_folder)
    item = td / item_name
    if not item.exists():
        return False

    meta_file = _meta_path(item)
    try:
        meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
        rel = meta.get("original")
        if not rel:
            return False
        dest = data_folder / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            ts = int(time.time())
            if item.is_dir():
                while dest.exists():
                    dest = dest.with_name(f"{dest.stem}_{ts}{dest.suffix}")
                    ts += 1
            else:
                dest = dest.with_name(f"{dest.stem}_{ts}{dest.suffix}")
        shutil.copytree(item, dest) if item.is_dir() else item.rename(dest)
        if meta_file.exists():
            meta_file.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def empty_trash(data_folder: Path) -> int:
    td = trash_dir(data_folder)
    deleted = 0
    for item in td.iterdir():
        try:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
            deleted += 1
        except Exception:
            pass
    return deleted


def purge_old_trash(data_folder: Path, retention_days: int = 30) -> int:
    td = trash_dir(data_folder)
    cutoff = _utcnow() - timedelta(days=retention_days)
    deleted = 0
    for item in td.iterdir():
        if item.name.endswith(META_SUFFIX):
            continue
        meta_file = _meta_path(item)
        deleted_at = _from_timestamp(item.stat().st_mtime)
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
                deleted_at = _parse_deleted_at(meta.get("deleted_at", deleted_at.isoformat()))
            except Exception:
                pass
        if deleted_at < cutoff:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
            if meta_file.exists():
                meta_file.unlink(missing_ok=True)
            deleted += 1
    return deleted
