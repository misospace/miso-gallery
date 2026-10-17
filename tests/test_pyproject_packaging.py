import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]

# conftest.py is test-harness support, not shipped runtime code; exclude it so
# the guard does not fail on it (issue #546).
TEST_HARNESS_MODULES = {"conftest"}


def dockerfile_runtime_modules(dockerfile_text):
    """Return the module names whose .py files are COPY'd by the Dockerfile."""
    modules = set()
    for line in dockerfile_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY "):
            continue
        tokens = stripped.split()
        # "COPY <sources...> <dest>": sources are all tokens but the first and last.
        for token in tokens[1:-1]:
            if token.endswith(".py"):
                modules.add(token[: -len(".py")])
    return modules


def pyproject_py_modules(pyproject_path):
    with open(pyproject_path, "rb") as fh:
        data = tomllib.load(fh)
    return set(data["tool"]["setuptools"]["py-modules"])


def packaging_drift(dockerfile_text, pyproject_path):
    copied = dockerfile_runtime_modules(dockerfile_text) - TEST_HARNESS_MODULES
    declared = pyproject_py_modules(pyproject_path) - TEST_HARNESS_MODULES
    problems = [
        f"{name}: in [tool.setuptools] py-modules but not copied by the Dockerfile"
        for name in sorted(declared - copied)
    ]
    problems += [
        f"{name}: copied by the Dockerfile but missing from [tool.setuptools] py-modules"
        for name in sorted(copied - declared)
    ]
    return problems


def write_fixtures(tmp_path, py_modules, copied_modules):
    pyproject = tmp_path / "pyproject.toml"
    entries = "".join(f'    "{name}",\n' for name in py_modules)
    pyproject.write_text(
        "[tool.setuptools]\npy-modules = [\n" + entries + "]\n"
    )
    sources = " ".join(f"{name}.py" for name in copied_modules)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(f"FROM python:3.15-slim\nCOPY {sources} ./\n")
    return dockerfile, pyproject


def test_repo_packaging_is_consistent():
    assert packaging_drift((REPO / "Dockerfile").read_text(), REPO / "pyproject.toml") == []


def test_module_only_in_pyproject_is_flagged(tmp_path):
    dockerfile, pyproject = write_fixtures(tmp_path, ["app", "newmod"], ["app"])
    problems = packaging_drift(dockerfile.read_text(), pyproject)
    assert problems == ["newmod: in [tool.setuptools] py-modules but not copied by the Dockerfile"]


def test_module_only_in_dockerfile_is_flagged(tmp_path):
    dockerfile, pyproject = write_fixtures(tmp_path, ["app"], ["app", "newmod"])
    problems = packaging_drift(dockerfile.read_text(), pyproject)
    assert problems == ["newmod: copied by the Dockerfile but missing from [tool.setuptools] py-modules"]


def test_conftest_is_exempt(tmp_path):
    dockerfile, pyproject = write_fixtures(tmp_path, ["app", "conftest"], ["app"])
    assert packaging_drift(dockerfile.read_text(), pyproject) == []


def test_empty_lists_are_consistent(tmp_path):
    dockerfile, pyproject = write_fixtures(tmp_path, [], [])
    assert packaging_drift(dockerfile.read_text(), pyproject) == []


def test_non_python_copy_sources_are_ignored(tmp_path):
    dockerfile, pyproject = write_fixtures(tmp_path, ["app"], ["app"])
    dockerfile.write_text(
        "COPY requirements.txt .\nCOPY templates/ templates/\n"
        + dockerfile.read_text()
    )
    assert packaging_drift(dockerfile.read_text(), pyproject) == []


def test_pyproject_py_modules_requires_the_table(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[project]\nname = 'x'\n")
    with pytest.raises(KeyError):
        pyproject_py_modules(pyproject)
