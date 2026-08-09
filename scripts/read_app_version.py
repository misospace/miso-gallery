#!/usr/bin/env python3
"""Read APP_VERSION from app.py using AST parsing (no module import)."""

import ast


def read_app_version(path: str = "app.py") -> str:
    """Return the string value of APP_VERSION defined in *path*."""
    try:
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
    except OSError as exc:
        raise SystemExit(f"Cannot read {path}: {exc}") from exc

    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        raise SystemExit(f"Syntax error in {path}: {exc}") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "APP_VERSION":
                    return _extract_string(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "APP_VERSION"
            and node.value is not None
        ):
                return _extract_string(node.value)

    raise SystemExit(f"APP_VERSION not found in {path}")


def _extract_string(node: ast.expr) -> str:
    """Return the string value of a constant or simple expression node."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    # Handle f-strings, BinOp (concatenation), Call (e.g. os.environ.get(...))
    # by walking child nodes for string constants — best-effort fallback.
    parts: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            parts.append(child.value)
    if parts:
        return parts[0]  # prefer the first literal found
    raise SystemExit("APP_VERSION value is not a simple string literal")


if __name__ == "__main__":
    print(read_app_version())
