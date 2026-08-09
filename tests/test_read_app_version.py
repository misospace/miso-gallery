import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "read_app_version.py"
SPEC = importlib.util.spec_from_file_location("read_app_version", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_reads_version_without_importing_app_dependencies(tmp_path):
    app = tmp_path / "app.py"
    app.write_text(
        'import definitely_missing_dependency\n'
        'APP_VERSION = (os.environ.get("APP_VERSION") or "1.2.3").strip() or "1.2.3"\n'
    )
    assert MODULE.read_app_version(app) == "1.2.3"


def test_rejects_inconsistent_defaults(tmp_path):
    app = tmp_path / "app.py"
    app.write_text(
        'APP_VERSION = (os.environ.get("APP_VERSION") or "1.2.3").strip() or "1.2.4"\n'
    )
    with pytest.raises(ValueError, match="consistent semver"):
        MODULE.read_app_version(app)


def test_bare_annotation_falls_through_to_not_found(tmp_path):
    app = tmp_path / "app.py"
    app.write_text("APP_VERSION: str\n")
    with pytest.raises(ValueError, match="not found"):
        MODULE.read_app_version(app)
