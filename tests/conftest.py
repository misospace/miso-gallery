"""Shared pytest bootstrap for miso-gallery tests.

Provides:
- TEST_SECRET: constant for SECRET_KEY env var used in all tests
- TEST_API_KEY: default API key for LLM endpoint tests
- build_client(): builds a Flask test client with isolated DATA_FOLDER and module state
- auth_header(): returns a valid Authorization header dict

Usage in test files::

    from conftest import build_client, auth_header

    def test_something(monkeypatch, tmp_path):
        client, data_dir = build_client(monkeypatch, tmp_path)
        ...
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_SECRET = "test-secret-for-ci"
TEST_API_KEY = "agent-key"


def build_client(monkeypatch, tmp_path, *, api_keys: str = TEST_API_KEY, auth_type: str = "local"):
    """Build a Flask test client with test env vars and isolated module state.

    Creates the same image fixture data as the original _build_client helper:
    - sample.png / copy.png (identical bytes, used for dedup tests)
    - cats/cat.jpg
    - .thumb_cache/hidden.png

    Env vars set before app import:
    - DATA_FOLDER      → tmp_path / "data"
    - AUTH_TYPE        → auth_type param
    - ADMIN_PASSWORD   → "pass123" (unless auth_type != "local")
    - OIDC_ENABLED     → "false"
    - SECRET_KEY       → TEST_SECRET
    - LLM_API_KEYS     → api_keys param (pass None to leave unset)

    Returns (client, data_dir).
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / ".thumb_cache").mkdir(exist_ok=True)
    (data_dir / ".thumb_cache" / "hidden.png").write_bytes(b"hidden-cache")

    # Duplicate-content pair used by dedup tests
    (data_dir / "sample.png").write_bytes(b"image-one")
    (data_dir / "copy.png").write_bytes(b"image-one")

    # cats subdir for search/LLM tests
    nested = data_dir / "cats"
    nested.mkdir()
    (nested / "cat.jpg").write_bytes(b"cat")

    monkeypatch.setenv("DATA_FOLDER", str(data_dir))
    monkeypatch.setenv("AUTH_TYPE", auth_type)
    monkeypatch.setenv("ADMIN_PASSWORD", "pass123" if auth_type == "local" else "")
    monkeypatch.setenv("OIDC_ENABLED", "false")
    monkeypatch.setenv("SECRET_KEY", TEST_SECRET)

    if api_keys is None:
        monkeypatch.delenv("LLM_API_KEYS", raising=False)
    else:
        monkeypatch.setenv("LLM_API_KEYS", api_keys)

    for mod in ("auth", "app"):
        sys.modules.pop(mod, None)

    app_module = importlib.import_module("app")
    app_module.DATA_FOLDER = data_dir
    app_module.THUMBNAIL_CACHE_DIR = data_dir / ".thumb_cache"
    app_module.app.config["TESTING"] = True

    return app_module.app.test_client(), data_dir


def auth_header(token: str = TEST_API_KEY) -> dict:
    """Return a dict suitable for Authorization: Bearer <token> headers."""
    return {"Authorization": f"Bearer {token}"}