#!/usr/bin/env python3
"""
Storage health probe for miso-gallery.

Provides health check endpoint for DATA_FOLDER/NFS path:
- read/list test
- optional safe write test
- clear unhealthy signal/logs
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import jsonify

DATA_FOLDER = Path(os.environ.get("DATA_FOLDER", "/data"))
THUMBNAIL_CACHE_DIR = DATA_FOLDER / ".thumb_cache"
STORAGE_HEALTH_SIGNAL_FILE = Path(
    os.environ.get("STORAGE_HEALTH_SIGNAL_FILE", "/tmp/miso-gallery-storage-unhealthy.signal")
)


def check_storage_read(path: Path) -> tuple[bool, str]:
    """Check if we can read from the storage path."""
    try:
        if not path.exists():
            return False, "Path does not exist"
        if not os.access(path, os.R_OK):
            return False, "Path is not readable"
        list(path.iterdir())
        return True, "Read access OK"
    except PermissionError:
        return False, "Permission denied reading"
    except OSError:
        return False, "OS error reading"


def check_storage_write(path: Path) -> tuple[bool, str]:
    """Check if we can write to the storage path (safe test)."""
    try:
        if not path.exists():
            return False, "Path does not exist"
        if not os.access(path, os.W_OK):
            return False, "Path is not writable"

        with tempfile.NamedTemporaryFile(dir=path, delete=False) as tmp:
            tmp.write(b"health probe write test")
            tmp_path = tmp.name

        os.unlink(tmp_path)
        return True, "Write access OK"
    except PermissionError:
        return False, "Permission denied writing"
    except OSError:
        return False, "OS error writing"


def update_unhealthy_signal(health: dict[str, Any]) -> None:
    """Persist or clear unhealthy storage signal file."""
    try:
        if health["status"] == "unhealthy":
            STORAGE_HEALTH_SIGNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
            STORAGE_HEALTH_SIGNAL_FILE.write_text(
                (
                    f"status={health['status']}\n"
                    f"timestamp={health['timestamp']}\n"
                    f"data_folder={DATA_FOLDER}\n"
                    f"thumbnail_cache={THUMBNAIL_CACHE_DIR}\n"
                ),
                encoding="utf-8",
            )
        elif STORAGE_HEALTH_SIGNAL_FILE.exists():
            STORAGE_HEALTH_SIGNAL_FILE.unlink()
    except OSError:
        # Signal file update should not break the health endpoint.
        pass


def _new_health_payload() -> dict[str, Any]:
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": {
            "data_folder": {},
            "thumbnail_cache": {},
        },
    }


STORAGE_PROBE_TARGETS: tuple[str, ...] = ("data_folder", "thumbnail_cache")


def _collect_storage_health(kinds: tuple[str, ...]) -> dict[str, Any]:
    """Run the given probe kinds against both storage targets and merge results.

    One shared loop for the three endpoint variants (read-only, write-only,
    comprehensive): every variant probed the same two paths with the same
    checkers. Paths and checkers resolve through module globals at call time,
    so tests keep monkeypatching ``DATA_FOLDER`` / ``check_storage_read`` etc.
    """
    checkers = {"read": check_storage_read, "write": check_storage_write}
    paths = {"data_folder": DATA_FOLDER, "thumbnail_cache": THUMBNAIL_CACHE_DIR}

    health: dict[str, Any] = _new_health_payload()
    all_ok = True
    for target in STORAGE_PROBE_TARGETS:
        for kind in kinds:
            ok, message = checkers[kind](paths[target])
            health["checks"][target][kind] = {
                "ok": ok,
                "message": message,
            }
            all_ok = all_ok and ok

    if not all_ok:
        health["status"] = "unhealthy"

    return health


def get_storage_read_health() -> dict[str, Any]:
    """Get read-only storage health status without write probes or signal updates."""
    return _collect_storage_health(("read",))


def get_storage_write_health() -> dict[str, Any]:
    """Get write-probe storage health status."""
    health = _collect_storage_health(("write",))
    update_unhealthy_signal(health)
    return health


def get_storage_health() -> dict[str, Any]:
    """Get comprehensive storage health status."""
    health = _collect_storage_health(("read", "write"))
    update_unhealthy_signal(health)
    return health


def storage_health() -> tuple[Any, int]:
    """Return storage health status."""
    health = get_storage_health()
    status_code = 200 if health["status"] == "healthy" else 503
    return jsonify(health), status_code


def storage_health_read() -> tuple[Any, int]:
    """Return read-only storage health status."""
    health = get_storage_read_health()
    status_code = 200 if health["status"] == "healthy" else 503
    return jsonify({
        "status": health["status"],
        "timestamp": health["timestamp"],
        "checks": {
            "data_folder": health["checks"]["data_folder"]["read"],
            "thumbnail_cache": health["checks"]["thumbnail_cache"]["read"],
        },
    }), status_code


def storage_health_write() -> tuple[Any, int]:
    """Return write-capable storage health status."""
    health = get_storage_write_health()
    status_code = 200 if health["status"] == "healthy" else 503
    return jsonify({
        "status": health["status"],
        "timestamp": health["timestamp"],
        "checks": {
            "data_folder": health["checks"]["data_folder"]["write"],
            "thumbnail_cache": health["checks"]["thumbnail_cache"]["write"],
        },
    }), status_code


def health() -> tuple[Any, int]:
    """Return root health endpoint with version and read-only storage status.

    Root /health is intentionally read-only to avoid write probes mutating
    storage during ordinary health checks (e.g. on NFS or during load).
    Write-capable probes are available on /health/storage/write.
    """
    storage_health_data = get_storage_read_health()
    app_version = os.environ.get("APP_VERSION") or "v0.1.x"

    health_data = {
        "status": storage_health_data["status"],
        "version": app_version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "storage": {
            "status": storage_health_data["status"],
            "data_folder": storage_health_data["checks"]["data_folder"],
            "thumbnail_cache": storage_health_data["checks"]["thumbnail_cache"],
        },
    }

    status_code = 200 if health_data["status"] == "healthy" else 503
    return jsonify(health_data), status_code
