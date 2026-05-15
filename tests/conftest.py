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

TEST_SECRET = "test-secret-ci-gateway"
TEST_API_KEY = "agent-key"

# Minimal 1x1 red PNG used for dedup tests (sample.png and copy.png have identical bytes)
_MINIMAL_PNG = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82'

# Minimal valid JPEG (10x10 gray) used for search/LLM tests
_MINIMAL_JPEG = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.\' ",#\x1c\x1c(7),01444\x1f\'9=82<.342\xff\xdb\x00C\x01\t\t\t\x0c\x0b\x0c\x18\r\r\x182!\x1c!22222222222222222222222222222222222222222222222222\xff\xc0\x00\x11\x08\x00\n\x00\n\x03\x01"\x00\x02\x11\x01\x03\x11\x01\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n\x16\x17\x18\x19\x1a%&\'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xc4\x00\x1f\x01\x00\x03\x01\x01\x01\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb5\x11\x00\x02\x01\x02\x04\x04\x03\x04\x07\x05\x04\x04\x00\x01\x02w\x00\x01\x02\x03\x11\x04\x05!1\x06\x12AQ\x07aq\x13"2\x81\x08\x14B\x91\xa1\xb1\xc1\t#3R\xf0\x15br\xd1\n\x16$4\xe1%\xf1\x17\x18\x19\x1a&\'()*56789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x82\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00?\x00(\xa2\x8a\x00\xff\xd9'


def build_client(monkeypatch, tmp_path, *, api_keys: str = TEST_API_KEY, auth_type: str = "local", extra_env: dict | None = None):
    """Build a Flask test client with test env vars and isolated module state.

    Creates minimal fixture data:
    - sample.png / copy.png (identical valid PNGs, used for dedup tests)
    - cats/cat.jpg (valid JPEG, used for search/LLM tests)

    Env vars set before app import:
    - DATA_FOLDER      → tmp_path / "data"
    - AUTH_TYPE        → auth_type param
    - ADMIN_PASSWORD   → "pass123" (unless auth_type != "local", then deleted)
    - OIDC_ENABLED     → "false"
    - SECRET_KEY       → TEST_SECRET
    - LLM_API_KEYS     → api_keys param (pass None to leave unset)

    extra_env: optional dict of additional env vars set before app import
    (e.g. GALLERY_AUTO_FOLDER_COVERS, WEBHOOK_ENABLED, OIDC_ISSUER, etc.)

    Returns (client, data_dir).
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / ".thumb_cache").mkdir(exist_ok=True)

    # Duplicate-content pair used by dedup tests (valid 1x1 PNG images)
    (data_dir / "sample.png").write_bytes(_MINIMAL_PNG)
    (data_dir / "copy.png").write_bytes(_MINIMAL_PNG)

    # cats subdir for search/LLM tests (valid JPEG image)
    nested = data_dir / "cats"
    nested.mkdir()
    (nested / "cat.jpg").write_bytes(_MINIMAL_JPEG)

    # Set extra env vars first so they take precedence over defaults
    if extra_env:
        for k, v in extra_env.items():
            monkeypatch.setenv(k, str(v))

    monkeypatch.setenv("DATA_FOLDER", str(data_dir))
    monkeypatch.setenv("AUTH_TYPE", auth_type)
    if auth_type == "local":
        monkeypatch.setenv("ADMIN_PASSWORD", "pass123")
    else:
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
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
