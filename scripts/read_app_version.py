#!/usr/bin/env python3
"""Read APP_VERSION from app.py without importing application dependencies."""

import ast
import re
import sys
from pathlib import Path

SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-.][0-9A-Za-z.-]+)?$")


def read_app_version(path: Path) -> str:
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "APP_VERSION" for target in targets):
            continue
        if isinstance(node, ast.AnnAssign) and node.value is None:
            continue
        values = {
            child.value
            for child in ast.walk(node.value)
            if isinstance(child, ast.Constant)
            and isinstance(child.value, str)
            and SEMVER.fullmatch(child.value)
        }
        if len(values) != 1:
            raise ValueError("APP_VERSION must contain one consistent semver default")
        return values.pop()
    raise ValueError("APP_VERSION assignment not found")


if __name__ == "__main__":
    source = Path(sys.argv[1] if len(sys.argv) > 1 else "app.py")
    try:
        print(read_app_version(source))
    except (OSError, SyntaxError, ValueError) as exc:
        raise SystemExit(f"Unable to read APP_VERSION from {source}: {exc}") from exc
