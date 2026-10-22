"""Regression tests for the ``mise run bootstrap`` task and its version sources.

Issue #571: the bootstrap task hard-coded ``setuptools==84.0.0`` while the
committed constraint lock pinned ``setuptools==84.2.1``. pip rejected the
bootstrap with ``ResolutionImpossible`` before the editable install ran, and
every dependency update that moved the constraint re-created the conflict.

The committed lock files are the single version authority: the bootstrap task
may not request a version of its own, the constraint lock must pin the build
backend, and the committed tool lock must ship the artifacts of the pinned
Python version.
"""

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).parents[1]
CONFIG = REPO / ".mise" / "config.toml"
CONSTRAINTS = REPO / ".mise" / "requirements-constraints.txt"
TOOL_LOCK = REPO / ".mise" / "mise.lock"

CONSTRAINT_FILE = ".mise/requirements-constraints.txt"
SETUPTOOLS = "setuptools"

VERSION_SPECIFIER = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*\s*(?:===|==|!=|<=|>=|~=|<|>)\s*[^\s,]+"
)
CONSTRAINT_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:===|==)\s*([^\s;]+)")
SETUPTOOLS_REQUEST = re.compile(rf"{SETUPTOOLS}\s*(?:===|==)\s*([^\s,]+)")
VERSION_LIKE = re.compile(r"\d+\.\d+\.\d+")


def _logical_lines(script):
    """Return the script as logical lines, joining backslash continuations."""
    lines = []
    pending = ""
    for raw_line in script.splitlines():
        line = pending + raw_line
        if line.rstrip().endswith("\\"):
            pending = line.rstrip()[:-1] + " "
            continue
        lines.append(line)
        pending = ""
    if pending:
        lines.append(pending)
    return lines


def _bootstrap_script():
    """Return the shell script of the ``bootstrap`` task."""
    config = tomllib.loads(CONFIG.read_text())
    return config["tasks"]["bootstrap"]["run"]


def _install_invocations(script):
    """Return the arguments of every ``pip install`` invocation in ``script``."""
    invocations = []
    for line in _logical_lines(script):
        marker = "pip install"
        if marker not in line:
            continue
        invocations.append(line.split(marker, 1)[1].strip())
    return invocations


def _constraint_pins():
    """Return the exact version pins of the committed constraint lock."""
    pins = {}
    for line in CONSTRAINTS.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = CONSTRAINT_PIN.match(line)
        if match is not None:
            pins[match.group(1).lower()] = match.group(2)
    return pins


def test_bootstrap_installs_dependencies_through_the_constraint_lock():
    """Every install in the task must resolve against the committed constraint
    lock; an install that bypasses it can pick a version the lock forbids."""
    invocations = _install_invocations(_bootstrap_script())
    assert invocations, "bootstrap task must install dependencies"
    for invocation in invocations:
        assert CONSTRAINT_FILE in invocation, (
            f"bootstrap install must use the constraint lock {CONSTRAINT_FILE}; "
            f"got {invocation!r}"
        )


def test_bootstrap_task_requests_no_version_specifier():
    """The task must request packages by name only. A version of its own
    contradicts the constraint lock and fails resolution, and it goes stale as
    soon as the lock advances."""
    for invocation in _install_invocations(_bootstrap_script()):
        match = VERSION_SPECIFIER.search(invocation)
        assert match is None, (
            f"bootstrap task must not request a version of its own; got "
            f"{match.group(0)!r} in {invocation!r}"
        )


def test_bootstrap_installs_setuptools_by_name():
    """setuptools is the build backend, so the task installs it under its bare
    name and lets the constraint lock choose the version."""
    invocations = [
        invocation
        for invocation in _install_invocations(_bootstrap_script())
        if SETUPTOOLS in invocation.split()
    ]
    assert invocations, "bootstrap task must install setuptools"
    for invocation in invocations:
        assert CONSTRAINT_FILE in invocation, (
            f"setuptools install must use the constraint lock {CONSTRAINT_FILE}; "
            f"got {invocation!r}"
        )


def test_constraint_lock_pins_setuptools_exactly():
    """The constraint lock must pin setuptools, otherwise the bootstrap
    install would resolve an unpinned build backend."""
    pins = _constraint_pins()
    assert SETUPTOOLS in pins, f"constraint lock must pin {SETUPTOOLS}"
    assert pins[SETUPTOOLS].strip(), f"{SETUPTOOLS} must be pinned to an exact version"


def test_bootstrap_setuptools_version_comes_from_the_constraint_lock():
    """The version installed by the bootstrap must be the one the constraint
    lock selects: the task requests no setuptools version of its own."""
    script = _bootstrap_script()
    requested = {
        match.group(1)
        for invocation in _install_invocations(script)
        for match in SETUPTOOLS_REQUEST.finditer(invocation)
    }
    assert not requested, (
        f"bootstrap must not request its own {SETUPTOOLS} version; "
        f"got {sorted(requested)}"
    )


def test_tool_lock_ships_the_pinned_python_version():
    """The pinned Python version and the committed tool lock must agree. A
    lock that still ships the artifacts of an older release makes
    ``mise install --locked python`` fail before the bootstrap can run."""
    config = tomllib.loads(CONFIG.read_text())
    lock = tomllib.loads(TOOL_LOCK.read_text())
    pinned = config["tools"]["python"]
    entries = lock["tools"]["python"]
    matching = [entry for entry in entries if entry.get("version") == pinned]
    assert matching, f"tool lock does not lock python {pinned}: {entries}"
    entry = matching[0]
    assert set(entry.get("specifiers", [])) == {pinned}, (
        f"tool lock specifiers must cover only python {pinned}: "
        f"{entry.get('specifiers')}"
    )
    platforms = {
        name: artifact
        for name, artifact in entry.items()
        if name.startswith("platforms.")
    }
    assert platforms, "tool lock must ship platform artifacts"
    for name, artifact in platforms.items():
        versions = set(VERSION_LIKE.findall(artifact["url"]))
        assert versions == {pinned}, (
            f"{name} artifact ships python {sorted(versions)}, "
            f"pinned version is {pinned}"
        )
