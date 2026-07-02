"""Regression tests for file_sha256() handle-release safety (issue #252)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402


def test_file_sha256_releases_handle_on_exception(tmp_path, monkeypatch):
    """If chunk reading raises mid-iteration, the file handle must be closed."""
    target = tmp_path / "blob.bin"
    target.write_bytes(b"hello world")

    handle = path_open = None
    real_open = Path.open

    class _TrackingHandle:
        def __init__(self, inner):
            self._inner = inner
            self.closed = False

        def read(self, n=-1):
            # Raise after the iterator has consumed the handle once.
            raise RuntimeError("simulated mid-read failure")

        def close(self):
            self.closed = True
            self._inner.close()

    def tracking_open(self, *args, **kwargs):
        inner = real_open(self, *args, **kwargs)
        tracked = _TrackingHandle(inner)
        nonlocal handle
        handle = tracked
        return tracked

    monkeypatch.setattr(Path, "open", tracking_open)

    raised = False
    try:
        app.file_sha256(target)
    except RuntimeError:
        raised = True

    assert raised, "expected simulated read failure to propagate"
    assert handle is not None, "Path.open was not invoked"
    assert handle.closed is True, "file handle was not released on exception"


def test_file_sha256_returns_correct_digest(tmp_path):
    target = tmp_path / "blob.bin"
    target.write_bytes(b"abc")
    import hashlib

    expected = hashlib.sha256(b"abc").hexdigest()
    assert app.file_sha256(target) == expected