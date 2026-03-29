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

from flask import Blueprint, jsonify

DATA_FOLDER = Path(os.environ.get("DATA_FOLDER", "/data"))
THUMBNAIL_CACHE_DIR = DATA_FOLDER / ".thumb_cache"
STORAGE_HEALTH_SIGNAL_FILE = Path(
    os.environ.get("STORAGE_HEALTH_SIGNAL_FILE", "/tmp/miso-gallery-storage-unhealthy.signal")
)

health_bp = Blueprint("health", __name__)


def check_storage_read(path: Path) -> tuple[bool, str]:
    """Check if we can read from the storage path."""
    try:
        if not path.exists():
            return False, f"Path does not exist: {path}"
        if not os.access(path, os.R_OK):
            return False, f"Path is not readable: {path}"
        # Try to list directory
        list(path.iterdir())
        return True, "Read access OK"
    except PermissionError as e:
        return False, f"Permission denied reading {path}: {e}"
    except OSError as e:
        return False, f"OS error reading {path}: {e}"


def check_storage_write(path: Path) -> tuple[bool, str]:
    """Check if we can write to the storage path (safe test)."""
    try:
        if not path.exists():
            return False, f"Path does not exist: {path}"
        if not os.access(path, os.W_OK):
            return False, f"Path is not writable: {path}"
        
        # Safe write test - create temp file and delete
        with tempfile.NamedTemporaryFile(dir=path, delete=False) as tmp:
            tmp.write(b"health probe write test")
            tmp_path = tmp.name
        
        # Cleanup
        os.unlink(tmp_path)
        return True, "Write access OK"
    except PermissionError as e:
        return False, f"Permission denied writing to {path}: {e}"
    except OSError as e:
        return False, f"OS error writing to {path}: {e}"


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


def get_storage_health() -> dict[str, Any]:
    """Get comprehensive storage health status."""
    health: dict[str, Any] = {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "signal_file": str(STORAGE_HEALTH_SIGNAL_FILE),
        "checks": {
            "data_folder": {"path": str(DATA_FOLDER)},
            "thumbnail_cache": {"path": str(THUMBNAIL_CACHE_DIR)},
        },
    }
    
    # Check data folder read
    read_ok, read_msg = check_storage_read(DATA_FOLDER)
    health["checks"]["data_folder"]["read"] = {
        "ok": read_ok,
        "message": read_msg,
    }
    
    # Check data folder write (safe test)
    write_ok, write_msg = check_storage_write(DATA_FOLDER)
    health["checks"]["data_folder"]["write"] = {
        "ok": write_ok,
        "message": write_msg,
    }
    
    # Check thumbnail cache read
    thumb_read_ok, thumb_read_msg = check_storage_read(THUMBNAIL_CACHE_DIR)
    health["checks"]["thumbnail_cache"]["read"] = {
        "ok": thumb_read_ok,
        "message": thumb_read_msg,
    }
    
    # Check thumbnail cache write (safe test)
    thumb_write_ok, thumb_write_msg = check_storage_write(THUMBNAIL_CACHE_DIR)
    health["checks"]["thumbnail_cache"]["write"] = {
        "ok": thumb_write_ok,
        "message": thumb_write_msg,
    }
    
    # Determine overall status
    if not (read_ok and write_ok and thumb_read_ok and thumb_write_ok):
        health["status"] = "unhealthy"

    update_unhealthy_signal(health)
    return health


@health_bp.route("/health/storage")
def storage_health() -> tuple[Any, int]:
    """Return storage health status."""
    health = get_storage_health()
    status_code = 200 if health["status"] == "healthy" else 503
    return jsonify(health), status_code


@health_bp.route("/health/storage/read")
def storage_health_read() -> tuple[Any, int]:
    """Return read-only storage health status."""
    health = get_storage_health()
    status_code = 200 if health["status"] == "healthy" else 503
    return jsonify({
        "status": health["status"],
        "timestamp": health["timestamp"],
        "checks": {
            "data_folder": health["checks"]["data_folder"]["read"],
            "thumbnail_cache": health["checks"]["thumbnail_cache"]["read"],
        },
    }), status_code


@health_bp.route("/health/storage/write")
def storage_health_write() -> tuple[Any, int]:
    """Return write-capable storage health status."""
    health = get_storage_health()
    status_code = 200 if health["status"] == "healthy" else 503
    return jsonify({
        "status": health["status"],
        "timestamp": health["timestamp"],
        "checks": {
            "data_folder": health["checks"]["data_folder"]["write"],
            "thumbnail_cache": health["checks"]["thumbnail_cache"]["write"],
        },
    }), status_code



@health_bp.route("/health")
def health() -> tuple[Any, int]:
    """Return root health endpoint with version and storage status."""
    storage_health_data = get_storage_health()
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
