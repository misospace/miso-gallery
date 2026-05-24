import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import health  # noqa: E402


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
