import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import health


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
