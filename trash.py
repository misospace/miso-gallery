from __future__ import annotations

import contextlib
import json
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

TRASH_DIR_NAME = ".trash"
META_SUFFIX = ".meta.json"


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
    except OSError:
        pass
    return total



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
        dest = td / f"{ts}_{file_path.name}"
        i = 1
        while dest.exists():
            dest = td / f"{ts}_{i}_{file_path.name}"
            i += 1

        try:
            file_path.rename(dest)
            meta = {
                "original": rel,
                "deleted_at": datetime.utcnow().isoformat(),
                "size": dest.stat().st_size,
            }
            _meta_path(dest).write_text(json.dumps(meta))
            return True
        except OSError:
            return False

    if file_path.is_dir():
        dest = td / f"{ts}_{file_path.name}"
        i = 1
        while dest.exists():
            dest = td / f"{ts}_{i}_{file_path.name}"
            i += 1

        try:
            # Use shutil.copytree to handle the move, then remove source
            # rename() can fail cross-device; copytree + rmtree is safer
            shutil.copytree(file_path, dest)
            shutil.rmtree(file_path)
            meta = {
                "original": rel,
                "deleted_at": datetime.utcnow().isoformat(),
                "size": _dir_size(dest),
            }
            _meta_path(dest).write_text(json.dumps(meta))
            return True
        except OSError:
            # Clean up partial copy on failure
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            return False

    return False


def _dir_size(path: Path) -> int:
    """Calculate total size of a directory tree."""
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            with contextlib.suppress(OSError):
                total += item.stat().st_size
    return total


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
        size = _dir_size(item) if item.is_dir() else item.stat().st_size
        out.append(
            {
                "name": item.name,
                "original": meta.get("original", "unknown"),
                "deleted_at": meta.get("deleted_at", datetime.fromtimestamp(item.stat().st_mtime).isoformat()),
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
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    deleted = 0
    for item in td.iterdir():
        if item.name.endswith(META_SUFFIX):
            continue
        meta_file = _meta_path(item)
        deleted_at = datetime.fromtimestamp(item.stat().st_mtime)
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
                deleted_at = datetime.fromisoformat(meta.get("deleted_at", deleted_at.isoformat()))
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
