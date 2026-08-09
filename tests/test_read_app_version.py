"""Tests for scripts/read_app_version.py."""

import os
import sys

import pytest

SCRIPT_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")
sys.path.insert(0, SCRIPT_DIR)


@pytest.fixture
def tmp_py_file(tmp_path):
    """Yield a path to a temporary .py file; caller writes content."""
    return str(tmp_path / "app.py")


class TestReadAppVersion:
    def test_simple_assign(self, tmp_py_file):
        with open(tmp_py_file, "w") as f:
            f.write('APP_VERSION = "1.2.3"\n')
        from read_app_version import read_app_version
        assert read_app_version(tmp_py_file) == "1.2.3"

    def test_annotated_assign(self, tmp_py_file):
        with open(tmp_py_file, "w") as f:
            f.write('APP_VERSION: str = "2.0.0"\n')
        from read_app_version import read_app_version
        assert read_app_version(tmp_py_file) == "2.0.0"

    def test_annotated_assign_no_value(self, tmp_py_file):
        """AnnAssign without a value must not crash (the original bug)."""
        with open(tmp_py_file, "w") as f:
            f.write('APP_VERSION: str\n')
        from read_app_version import read_app_version
        with pytest.raises(SystemExit, match="APP_VERSION not found"):
            read_app_version(tmp_py_file)

    def test_missing_variable(self, tmp_py_file):
        with open(tmp_py_file, "w") as f:
            f.write('OTHER = "x"\n')
        from read_app_version import read_app_version
        with pytest.raises(SystemExit, match="APP_VERSION not found"):
            read_app_version(tmp_py_file)

    def test_missing_file(self):
        from read_app_version import read_app_version
        with pytest.raises(SystemExit, match="Cannot read"):
            read_app_version("/nonexistent/path/app.py")

    def test_syntax_error(self, tmp_py_file):
        with open(tmp_py_file, "w") as f:
            f.write("def foo(\n")
        from read_app_version import read_app_version
        with pytest.raises(SystemExit, match="Syntax error"):
            read_app_version(tmp_py_file)

    def test_default_path(self, tmp_path, monkeypatch):
        app_py = str(tmp_path / "app.py")
        with open(app_py, "w") as f:
            f.write('APP_VERSION = "3.0.0"\n')
        monkeypatch.chdir(str(tmp_path))
        from read_app_version import read_app_version
        assert read_app_version() == "3.0.0"
