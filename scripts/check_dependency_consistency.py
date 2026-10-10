"""Check dependency pins against pyproject ranges.

requirements.txt is the exact pinned install manifest. pyproject.toml mirrors
runtime packages with lower bounds equal to those pins. The pip pin is exempt
because it is a tooling/security pin, not an application runtime dependency.
"""

import pathlib
import re
import sys
import tomllib

NON_RUNTIME = {"pip"}


_NAME_PATTERN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")
_LOWER_BOUND_PATTERN = re.compile(r"^\s*(?:===|==|>=|~=)\s*([0-9]+(?:\.[0-9]+)*)")


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def lower_bound(specifier: str) -> tuple[int, ...] | None:
    match = _LOWER_BOUND_PATTERN.match(specifier)
    if match is None:
        return None
    return tuple(int(component) for component in match.group(1).split("."))


def read_requirements(path: pathlib.Path) -> dict[str, str]:
    requirements = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = _NAME_PATTERN.match(line)
        if match is None:
            continue
        name = match.group(1)
        specifier = line[match.end():].strip()
        requirements[normalize_name(name)] = specifier
    return requirements


def read_pyproject_runtime(path: pathlib.Path) -> dict[str, str]:
    with path.open("rb") as project_file:
        project = tomllib.load(project_file)["project"]
    dependencies = {}
    for dependency in project.get("dependencies", []):
        match = _NAME_PATTERN.match(dependency)
        if match is None:
            continue
        name = match.group(1)
        remainder = dependency[match.end():]
        if remainder.startswith("["):
            extras_end = remainder.find("]")
            if extras_end != -1:
                remainder = remainder[extras_end + 1:]
        dependencies[normalize_name(name)] = remainder.strip()
    return dependencies


def check_consistency(
    pyproject_path: pathlib.Path, requirements_path: pathlib.Path
) -> list[str]:
    runtime = {
        name: specifier
        for name, specifier in read_pyproject_runtime(pyproject_path).items()
        if name not in NON_RUNTIME
    }
    pinned = {
        name: specifier
        for name, specifier in read_requirements(requirements_path).items()
        if name not in NON_RUNTIME
    }
    problems = []
    for name in sorted(runtime.keys() - pinned.keys()):
        problems.append(
            f"{name} is declared in pyproject.toml but not pinned in requirements.txt"
        )
    for name in sorted(pinned.keys() - runtime.keys()):
        problems.append(
            f"{name} is pinned in requirements.txt but not declared in pyproject.toml"
        )
    for name in sorted(runtime.keys() & pinned.keys()):
        runtime_floor = lower_bound(runtime[name])
        pinned_floor = lower_bound(pinned[name])
        if runtime_floor is None or pinned_floor is None:
            problems.append(
                f"{name} has a missing lower bound: pyproject.toml {runtime[name]!r} "
                f"({runtime_floor}) vs requirements.txt {pinned[name]!r} ({pinned_floor})"
            )
        elif runtime_floor != pinned_floor:
            problems.append(
                f"{name} floor mismatch: pyproject.toml {runtime[name]!r} "
                f"({runtime_floor}) vs requirements.txt {pinned[name]!r} ({pinned_floor})"
            )
    return sorted(problems)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) > 1:
        print("Usage: check_dependency_consistency.py [directory]", file=sys.stderr)
        return 2
    root = pathlib.Path(arguments[0]) if arguments else pathlib.Path(__file__).resolve().parent.parent
    problems = check_consistency(root / "pyproject.toml", root / "requirements.txt")
    if problems:
        for problem in problems:
            print(f"::error::{problem}")
        return 1
    print("OK: pyproject.toml and requirements.txt dependencies agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
