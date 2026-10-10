import importlib.util
from pathlib import Path

REPO = Path(__file__).parents[1]
SCRIPT = REPO / "scripts" / "check_dependency_consistency.py"
SPEC = importlib.util.spec_from_file_location("check_dependency_consistency", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def write_manifests(tmp_path, dependencies, requirements):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project]\nname = 'test-project'\ndependencies = [\n"
        + "".join(f"    {dependency!r},\n" for dependency in dependencies)
        + "]\n"
    )
    requirements_file = tmp_path / "requirements.txt"
    requirements_file.write_text(requirements)
    return pyproject, requirements_file


def test_repo_manifests_are_consistent():
    assert MODULE.check_consistency(REPO / "pyproject.toml", REPO / "requirements.txt") == []


def test_missing_cryptography_is_flagged(tmp_path):
    pyproject, requirements = write_manifests(
        tmp_path, ["Flask>=3.1.3,<4"], "flask==3.1.3\ncryptography>=41.0.0\n"
    )
    problems = MODULE.check_consistency(pyproject, requirements)
    assert any("cryptography" in problem for problem in problems)


def test_floor_drift_is_flagged(tmp_path):
    pyproject, requirements = write_manifests(
        tmp_path, ["Flask>=3.1.3,<4"], "flask==3.1.4\n"
    )
    problems = MODULE.check_consistency(pyproject, requirements)
    assert any("flask" in problem for problem in problems)


def test_undeclared_requirement_is_flagged(tmp_path):
    pyproject, requirements = write_manifests(
        tmp_path, ["Flask>=3.1.3,<4"], "flask==3.1.3\nextra-package==1.0.0\n"
    )
    problems = MODULE.check_consistency(pyproject, requirements)
    assert any("extra-package" in problem for problem in problems)


def test_pip_is_exempt(tmp_path):
    pyproject, requirements = write_manifests(
        tmp_path, ["Flask>=3.1.3,<4", "Pillow>=12.3.0,<13"],
        "flask==3.1.3\npillow==12.3.0\npip>=26.1.2\n"
    )
    assert MODULE.check_consistency(pyproject, requirements) == []


def test_name_normalization(tmp_path):
    pyproject, requirements = write_manifests(
        tmp_path, ["Flask>=3.1.3,<4", "Pillow>=12.3.0,<13"],
        "flask==3.1.3\npillow==12.3.0\n"
    )
    assert MODULE.check_consistency(pyproject, requirements) == []
    assert MODULE.normalize_name("a_B.c---D") == "a-b-c-d"


def test_lower_bound_parsing():
    assert MODULE.lower_bound("==3.1.3") == (3, 1, 3)
    assert MODULE.lower_bound(">=41.0.0") == (41, 0, 0)
    assert MODULE.lower_bound(">=3.1.3,<4") == (3, 1, 3)
    assert MODULE.lower_bound("<4") is None


def test_comments_and_blanks_ignored(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("\n# comment\nFlask==3.1.3  # trailing comment\n\n")
    assert MODULE.read_requirements(requirements) == {"flask": "==3.1.3"}
