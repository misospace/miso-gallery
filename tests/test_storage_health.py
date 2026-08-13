import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402
import health  # noqa: E402


@pytest.fixture
def flask_ctx():
    with app_module.app.app_context():
        yield app_module.app


def test_unhealthy_creates_signal_file(monkeypatch, tmp_path):
    data = tmp_path / "data"
    thumb = data / ".thumb_cache"
    data.mkdir()
    thumb.mkdir()
    signal = tmp_path / "storage-unhealthy.signal"

    monkeypatch.setattr(health, "DATA_FOLDER", data)
    monkeypatch.setattr(health, "THUMBNAIL_CACHE_DIR", thumb)
    monkeypatch.setattr(health, "STORAGE_HEALTH_SIGNAL_FILE", signal)

    def fake_read(_path: Path):
        return True, "Read OK"

    def fake_write(path: Path):
        if path == thumb:
            return False, "Write failed"
        return True, "Write OK"

    monkeypatch.setattr(health, "check_storage_read", fake_read)
    monkeypatch.setattr(health, "check_storage_write", fake_write)

    result = health.get_storage_health()

    assert result["status"] == "unhealthy"
    assert signal.exists()
    text = signal.read_text(encoding="utf-8")
    assert "unhealthy" in text
    assert str(data) in text


def test_healthy_clears_existing_signal_file(monkeypatch, tmp_path):
    data = tmp_path / "data"
    thumb = data / ".thumb_cache"
    data.mkdir()
    thumb.mkdir()
    signal = tmp_path / "storage-unhealthy.signal"
    signal.write_text("stale unhealthy state", encoding="utf-8")

    monkeypatch.setattr(health, "DATA_FOLDER", data)
    monkeypatch.setattr(health, "THUMBNAIL_CACHE_DIR", thumb)
    monkeypatch.setattr(health, "STORAGE_HEALTH_SIGNAL_FILE", signal)

    def always_ok(_path: Path):
        return True, "OK"

    monkeypatch.setattr(health, "check_storage_read", always_ok)
    monkeypatch.setattr(health, "check_storage_write", always_ok)

    result = health.get_storage_health()

    assert result["status"] == "healthy"
    assert not signal.exists()


def test_read_health_does_not_run_write_probe_or_touch_signal(monkeypatch, tmp_path):
    data = tmp_path / "data"
    thumb = data / ".thumb_cache"
    data.mkdir()
    thumb.mkdir()
    signal = tmp_path / "storage-unhealthy.signal"
    signal.write_text("existing signal", encoding="utf-8")

    monkeypatch.setattr(health, "DATA_FOLDER", data)
    monkeypatch.setattr(health, "THUMBNAIL_CACHE_DIR", thumb)
    monkeypatch.setattr(health, "STORAGE_HEALTH_SIGNAL_FILE", signal)

    read_paths = []

    def fake_read(path: Path):
        read_paths.append(path)
        return True, "Read OK"

    def fail_if_written(_path: Path):
        raise AssertionError("read health must not run write probes")

    monkeypatch.setattr(health, "check_storage_read", fake_read)
    monkeypatch.setattr(health, "check_storage_write", fail_if_written)

    result = health.get_storage_read_health()

    assert result["status"] == "healthy"
    assert read_paths == [data, thumb]
    assert set(result["checks"]["data_folder"]) == {"read"}
    assert set(result["checks"]["thumbnail_cache"]) == {"read"}
    assert signal.read_text(encoding="utf-8") == "existing signal"


def test_write_health_runs_write_probe_and_updates_signal(monkeypatch, tmp_path):
    data = tmp_path / "data"
    thumb = data / ".thumb_cache"
    data.mkdir()
    thumb.mkdir()
    signal = tmp_path / "storage-unhealthy.signal"

    monkeypatch.setattr(health, "DATA_FOLDER", data)
    monkeypatch.setattr(health, "THUMBNAIL_CACHE_DIR", thumb)
    monkeypatch.setattr(health, "STORAGE_HEALTH_SIGNAL_FILE", signal)

    def fail_if_read(_path: Path):
        raise AssertionError("write health should not run read probes")

    def fake_write(path: Path):
        if path == thumb:
            return False, "Write failed"
        return True, "Write OK"

    monkeypatch.setattr(health, "check_storage_read", fail_if_read)
    monkeypatch.setattr(health, "check_storage_write", fake_write)

    result = health.get_storage_write_health()

    assert result["status"] == "unhealthy"
    assert set(result["checks"]["data_folder"]) == {"write"}
    assert set(result["checks"]["thumbnail_cache"]) == {"write"}
    assert signal.exists()


# ---------------------------------------------------------------------------
# check_storage_read / check_storage_write failure-branch coverage
# ---------------------------------------------------------------------------


def test_check_storage_read_returns_not_exists(monkeypatch, tmp_path):
    missing = tmp_path / "does-not-exist"
    ok, msg = health.check_storage_read(missing)
    assert ok is False
    assert "does not exist" in msg


def test_check_storage_read_returns_not_readable(monkeypatch, tmp_path):
    p = tmp_path / "dir"
    p.mkdir()
    real_access = health.os.access

    def fake_access(path, mode):
        if path == p and mode == health.os.R_OK:
            return False
        return real_access(path, mode)

    monkeypatch.setattr(health.os, "access", fake_access)
    ok, msg = health.check_storage_read(p)
    assert ok is False
    assert "not readable" in msg


def test_check_storage_read_handles_permission_error(monkeypatch, tmp_path):
    p = tmp_path / "dir"
    p.mkdir()

    def fake_iterdir(_self):
        raise PermissionError("denied")

    monkeypatch.setattr(health.Path, "iterdir", fake_iterdir)
    ok, msg = health.check_storage_read(p)
    assert ok is False
    assert "Permission denied" in msg


def test_check_storage_read_handles_os_error(monkeypatch, tmp_path):
    p = tmp_path / "dir"
    p.mkdir()

    def fake_iterdir(_self):
        raise OSError("io boom")

    monkeypatch.setattr(health.Path, "iterdir", fake_iterdir)
    ok, msg = health.check_storage_read(p)
    assert ok is False
    assert "OS error" in msg


def test_check_storage_write_returns_not_exists(monkeypatch, tmp_path):
    missing = tmp_path / "does-not-exist"
    ok, msg = health.check_storage_write(missing)
    assert ok is False
    assert "does not exist" in msg


def test_check_storage_write_returns_not_writable(monkeypatch, tmp_path):
    p = tmp_path / "dir"
    p.mkdir()
    real_access = health.os.access

    def fake_access(path, mode):
        if path == p and mode == health.os.W_OK:
            return False
        return real_access(path, mode)

    monkeypatch.setattr(health.os, "access", fake_access)
    ok, msg = health.check_storage_write(p)
    assert ok is False
    assert "not writable" in msg


def test_check_storage_write_handles_permission_error(monkeypatch, tmp_path):
    p = tmp_path / "dir"
    p.mkdir()

    def fake_ntf(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(health.tempfile, "NamedTemporaryFile", fake_ntf)
    ok, msg = health.check_storage_write(p)
    assert ok is False
    assert "Permission denied" in msg


def test_check_storage_write_handles_os_error(monkeypatch, tmp_path):
    p = tmp_path / "dir"
    p.mkdir()

    def fake_ntf(*args, **kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(health.tempfile, "NamedTemporaryFile", fake_ntf)
    ok, msg = health.check_storage_write(p)
    assert ok is False
    assert "OS error" in msg


def test_check_storage_write_removes_temp_file_on_success(tmp_path):
    p = tmp_path / "dir"
    p.mkdir()
    ok, msg = health.check_storage_write(p)
    assert ok is True
    assert msg == "Write access OK"


# ---------------------------------------------------------------------------
# update_unhealthy_signal OSError branch
# ---------------------------------------------------------------------------


def test_update_unhealthy_signal_swallows_oserror_when_writing(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    signal = tmp_path / "subdir" / "signal.file"

    monkeypatch.setattr(health, "DATA_FOLDER", data)
    monkeypatch.setattr(health, "THUMBNAIL_CACHE_DIR", data / ".thumb_cache")
    monkeypatch.setattr(health, "STORAGE_HEALTH_SIGNAL_FILE", signal)

    def fake_write_text(self, *args, **kwargs):
        raise OSError("cannot write")

    monkeypatch.setattr(health.Path, "write_text", fake_write_text)

    payload = {
        "status": "unhealthy",
        "timestamp": "2026-08-13T00:00:00+00:00",
    }
    # Must not raise.
    health.update_unhealthy_signal(payload)


def test_update_unhealthy_signal_swallows_oserror_when_unlinking(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    signal = tmp_path / "signal.file"
    signal.write_text("stale", encoding="utf-8")

    monkeypatch.setattr(health, "DATA_FOLDER", data)
    monkeypatch.setattr(health, "THUMBNAIL_CACHE_DIR", data / ".thumb_cache")
    monkeypatch.setattr(health, "STORAGE_HEALTH_SIGNAL_FILE", signal)

    def fake_unlink(self, *args, **kwargs):
        raise OSError("cannot unlink")

    monkeypatch.setattr(health.Path, "unlink", fake_unlink)

    payload = {
        "status": "healthy",
        "timestamp": "2026-08-13T00:00:00+00:00",
    }
    # Must not raise.
    health.update_unhealthy_signal(payload)


def test_update_unhealthy_signal_creates_parent_dir(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    signal = tmp_path / "new" / "deep" / "signal.file"

    monkeypatch.setattr(health, "DATA_FOLDER", data)
    monkeypatch.setattr(health, "THUMBNAIL_CACHE_DIR", data / ".thumb_cache")
    monkeypatch.setattr(health, "STORAGE_HEALTH_SIGNAL_FILE", signal)

    payload = {
        "status": "unhealthy",
        "timestamp": "2026-08-13T00:00:00+00:00",
    }
    health.update_unhealthy_signal(payload)
    assert signal.exists()


# ---------------------------------------------------------------------------
# Endpoint wrappers (storage_health_read, storage_health_write, health)
# ---------------------------------------------------------------------------


def test_storage_health_read_endpoint_returns_503_on_unreadable(monkeypatch, tmp_path, flask_ctx):
    data = tmp_path / "data"
    thumb = data / ".thumb_cache"
    data.mkdir()
    thumb.mkdir()
    signal = tmp_path / "storage-unhealthy.signal"

    monkeypatch.setattr(health, "DATA_FOLDER", data)
    monkeypatch.setattr(health, "THUMBNAIL_CACHE_DIR", thumb)
    monkeypatch.setattr(health, "STORAGE_HEALTH_SIGNAL_FILE", signal)

    def fake_read(path: Path):
        if path == data:
            return False, "Path is not readable"
        return True, "Read OK"

    monkeypatch.setattr(health, "check_storage_read", fake_read)

    payload, status = health.storage_health_read()
    assert status == 503
    assert payload.get_json()["status"] == "unhealthy"
    assert payload.get_json()["checks"]["data_folder"]["ok"] is False


def test_storage_health_read_endpoint_returns_200_on_healthy(monkeypatch, tmp_path, flask_ctx):
    data = tmp_path / "data"
    thumb = data / ".thumb_cache"
    data.mkdir()
    thumb.mkdir()
    signal = tmp_path / "storage-unhealthy.signal"

    monkeypatch.setattr(health, "DATA_FOLDER", data)
    monkeypatch.setattr(health, "THUMBNAIL_CACHE_DIR", thumb)
    monkeypatch.setattr(health, "STORAGE_HEALTH_SIGNAL_FILE", signal)

    def always_ok(_path: Path):
        return True, "OK"

    monkeypatch.setattr(health, "check_storage_read", always_ok)

    payload, status = health.storage_health_read()
    assert status == 200
    assert payload.get_json()["status"] == "healthy"


def test_storage_health_write_endpoint_returns_503_on_unwritable(monkeypatch, tmp_path, flask_ctx):
    data = tmp_path / "data"
    thumb = data / ".thumb_cache"
    data.mkdir()
    thumb.mkdir()
    signal = tmp_path / "storage-unhealthy.signal"

    monkeypatch.setattr(health, "DATA_FOLDER", data)
    monkeypatch.setattr(health, "THUMBNAIL_CACHE_DIR", thumb)
    monkeypatch.setattr(health, "STORAGE_HEALTH_SIGNAL_FILE", signal)

    def fake_write(path: Path):
        if path == thumb:
            return False, "Path is not writable"
        return True, "Write OK"

    monkeypatch.setattr(health, "check_storage_write", fake_write)

    payload, status = health.storage_health_write()
    assert status == 503
    body = payload.get_json()
    assert body["status"] == "unhealthy"
    assert body["checks"]["thumbnail_cache"]["ok"] is False


def test_storage_health_write_endpoint_returns_200_on_healthy(monkeypatch, tmp_path, flask_ctx):
    data = tmp_path / "data"
    thumb = data / ".thumb_cache"
    data.mkdir()
    thumb.mkdir()
    signal = tmp_path / "storage-unhealthy.signal"

    monkeypatch.setattr(health, "DATA_FOLDER", data)
    monkeypatch.setattr(health, "THUMBNAIL_CACHE_DIR", thumb)
    monkeypatch.setattr(health, "STORAGE_HEALTH_SIGNAL_FILE", signal)

    def always_ok(_path: Path):
        return True, "OK"

    monkeypatch.setattr(health, "check_storage_write", always_ok)

    payload, status = health.storage_health_write()
    assert status == 200
    assert payload.get_json()["status"] == "healthy"


def test_root_health_endpoint_reports_version_and_storage(monkeypatch, tmp_path, flask_ctx):
    data = tmp_path / "data"
    thumb = data / ".thumb_cache"
    data.mkdir()
    thumb.mkdir()
    signal = tmp_path / "storage-unhealthy.signal"

    monkeypatch.setattr(health, "DATA_FOLDER", data)
    monkeypatch.setattr(health, "THUMBNAIL_CACHE_DIR", thumb)
    monkeypatch.setattr(health, "STORAGE_HEALTH_SIGNAL_FILE", signal)
    monkeypatch.setenv("APP_VERSION", "v9.9.9")

    def fake_read(path: Path):
        return True, "Read OK"

    monkeypatch.setattr(health, "check_storage_read", fake_read)

    payload, status = health.health()
    body = payload.get_json()
    assert status == 200
    assert body["version"] == "v9.9.9"
    assert body["status"] == "healthy"
    assert body["storage"]["status"] == "healthy"
    assert "data_folder" in body["storage"]
    assert "thumbnail_cache" in body["storage"]


def test_root_health_endpoint_defaults_version_when_env_unset(monkeypatch, tmp_path, flask_ctx):
    data = tmp_path / "data"
    thumb = data / ".thumb_cache"
    data.mkdir()
    thumb.mkdir()
    signal = tmp_path / "storage-unhealthy.signal"

    monkeypatch.setattr(health, "DATA_FOLDER", data)
    monkeypatch.setattr(health, "THUMBNAIL_CACHE_DIR", thumb)
    monkeypatch.setattr(health, "STORAGE_HEALTH_SIGNAL_FILE", signal)
    monkeypatch.delenv("APP_VERSION", raising=False)

    def fake_read(path: Path):
        return True, "Read OK"

    monkeypatch.setattr(health, "check_storage_read", fake_read)

    payload, status = health.health()
    body = payload.get_json()
    assert status == 200
    assert body["version"] == "v0.1.x"


def test_root_health_endpoint_uses_only_read_probe(monkeypatch, tmp_path, flask_ctx):
    data = tmp_path / "data"
    thumb = data / ".thumb_cache"
    data.mkdir()
    thumb.mkdir()
    signal = tmp_path / "storage-unhealthy.signal"

    monkeypatch.setattr(health, "DATA_FOLDER", data)
    monkeypatch.setattr(health, "THUMBNAIL_CACHE_DIR", thumb)
    monkeypatch.setattr(health, "STORAGE_HEALTH_SIGNAL_FILE", signal)

    def fail_write(_path: Path):
        raise AssertionError("root /health must not run write probes")

    def fake_read(path: Path):
        return True, "Read OK"

    monkeypatch.setattr(health, "check_storage_read", fake_read)
    monkeypatch.setattr(health, "check_storage_write", fail_write)

    payload, status = health.health()
    body = payload.get_json()
    assert status == 200
    # Read-only storage status; no `write` key present.
    assert "write" not in body["storage"]["data_folder"]
    assert "write" not in body["storage"]["thumbnail_cache"]


def test_root_health_endpoint_returns_503_when_storage_unhealthy(monkeypatch, tmp_path, flask_ctx):
    data = tmp_path / "data"
    thumb = data / ".thumb_cache"
    data.mkdir()
    thumb.mkdir()
    signal = tmp_path / "storage-unhealthy.signal"

    monkeypatch.setattr(health, "DATA_FOLDER", data)
    monkeypatch.setattr(health, "THUMBNAIL_CACHE_DIR", thumb)
    monkeypatch.setattr(health, "STORAGE_HEALTH_SIGNAL_FILE", signal)
    monkeypatch.setenv("APP_VERSION", "v1.0.0")

    def fake_read(path: Path):
        if path == data:
            return False, "Path is not readable"
        return True, "Read OK"

    monkeypatch.setattr(health, "check_storage_read", fake_read)

    payload, status = health.health()
    body = payload.get_json()
    assert status == 503
    assert body["status"] == "unhealthy"
    assert body["storage"]["status"] == "unhealthy"
    assert body["version"] == "v1.0.0"
