from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

TRASH_DIR_NAME = ".trash"
META_SUFFIX = ".meta.json"


def trash_dir(data_folder: Path) -> Path:
    path = data_folder / TRASH_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _meta_path(item_path: Path) -> Path:
    return item_path.with_name(item_path.name + META_SUFFIX)


def move_to_trash(file_path: Path, data_folder: Path) -> bool:
    if not file_path.exists() or not file_path.is_file():
        return False

    td = trash_dir(data_folder)
    rel = file_path.relative_to(data_folder).as_posix()
    ts = int(time.time())
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


def list_trash(data_folder: Path) -> list[dict]:
    td = trash_dir(data_folder)
    out = []
    for item in td.iterdir():
        if item.is_dir() or item.name.endswith(META_SUFFIX):
            continue
        meta_file = _meta_path(item)
        meta = {}
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
            except Exception:
                meta = {}
        out.append(
            {
                "name": item.name,
                "original": meta.get("original", "unknown"),
                "deleted_at": meta.get("deleted_at", datetime.fromtimestamp(item.stat().st_mtime).isoformat()),
                "size": item.stat().st_size,
            }
        )
    out.sort(key=lambda x: x["deleted_at"], reverse=True)
    return out


def restore_from_trash(item_name: str, data_folder: Path) -> bool:
    td = trash_dir(data_folder)
    item = td / item_name
    if not item.exists() or not item.is_file():
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
            dest = dest.with_name(f"{dest.stem}_{ts}{dest.suffix}")
        item.rename(dest)
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
        if item.is_dir() or item.name.endswith(META_SUFFIX):
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
            item.unlink(missing_ok=True)
            if meta_file.exists():
                meta_file.unlink(missing_ok=True)
            deleted += 1
    return deleted
