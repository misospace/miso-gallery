"""Tests for the in-browser upload button (issue #485).

Uploads land in UPLOAD_DIR (default ``DATA_FOLDER/input``) so ad-hoc
images can be shared without write access to the image share.
"""

import io
import sys
from pathlib import Path

from conftest import _MINIMAL_PNG, build_client

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_PNG = _MINIMAL_PNG


def _local_client(monkeypatch, tmp_path, extra_env=None):
    """Build a local-auth client. Returns (client, data_dir)."""
    env = {"ALLOW_INMEMORY_RATE_LIMIT": "1"}
    if extra_env:
        env.update(extra_env)
    return build_client(monkeypatch, tmp_path, extra_env=env)


def setup_function():
    """Reset the in-memory rate limiter to avoid cross-test pollution."""
    from security import FALLBACK_LIMITER

    FALLBACK_LIMITER.reset()


def _login(client, password="pass123"):
    """Log in with local password auth; returns the current session CSRF token."""
    resp = client.get("/login")
    assert resp.status_code == 200
    resp = client.post("/auth", data={"csrf_token": "wrong", "password": password})
    assert resp.status_code == 403, "invalid CSRF must be rejected"
    with client.session_transaction() as sess:
        csrf = sess["csrf_token"]
    resp = client.post("/auth", data={"csrf_token": csrf, "password": password})
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert sess.get("authenticated") is True
        return sess["csrf_token"]


def test_upload_button_rendered_on_root_page(monkeypatch, tmp_path):
    client, _ = _local_client(monkeypatch, tmp_path)
    _login(client)

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="uploadForm"' in html
    assert 'action="/upload"' in html
    assert 'name="files"' in html
    # Destination is the input dir under the gallery root (root/input/*)
    assert "input" in html


def test_upload_button_hidden_in_subfolder(monkeypatch, tmp_path):
    client, _ = _local_client(monkeypatch, tmp_path)
    _login(client)

    resp = client.get("/cats")
    assert resp.status_code == 200
    assert 'id="uploadForm"' not in resp.get_data(as_text=True)


def test_upload_saves_image_to_input_dir_and_redirects(monkeypatch, tmp_path):
    client, data_dir = _local_client(monkeypatch, tmp_path)
    csrf = _login(client)

    resp = client.post(
        "/upload",
        data={"csrf_token": csrf, "files": (io.BytesIO(_PNG), "ad-hoc.png")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] in ("/", "/input")

    saved = data_dir / "input" / "ad-hoc.png"
    assert saved.exists()
    assert saved.read_bytes() == _PNG


def test_upload_duplicate_name_is_renamed(monkeypatch, tmp_path):
    client, data_dir = _local_client(monkeypatch, tmp_path)
    csrf = _login(client)
    (data_dir / "input").mkdir(parents=True, exist_ok=True)
    (data_dir / "input" / "dup.png").write_bytes(_PNG)

    resp = client.post(
        "/upload",
        data={"csrf_token": csrf, "files": (io.BytesIO(_PNG), "dup.png")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302

    uploaded = [p for p in (data_dir / "input").iterdir() if p.name != "dup.png"]
    assert len(uploaded) == 1
    assert (data_dir / "input" / "dup.png").read_bytes() == _PNG  # original untouched
    # The redirect must not 404: destination folder exists inside the gallery root.
    follow = client.get(resp.headers["Location"])
    assert follow.status_code == 200


def test_upload_rejects_non_image(monkeypatch, tmp_path):
    client, data_dir = _local_client(monkeypatch, tmp_path)
    csrf = _login(client)

    resp = client.post(
        "/upload",
        data={"csrf_token": csrf, "files": (io.BytesIO(b"rm -rf /"), "note.txt")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 422
    assert not (data_dir / "input").exists() or not any((data_dir / "input").iterdir())


def test_upload_invalid_csrf_rejected(monkeypatch, tmp_path):
    client, data_dir = _local_client(monkeypatch, tmp_path)
    _login(client)

    resp = client.post(
        "/upload",
        data={"csrf_token": "attacker", "files": (io.BytesIO(_PNG), "x.png")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 403
    assert not (data_dir / "input").exists()


def test_upload_unauthenticated_does_not_save(monkeypatch, tmp_path):
    client, data_dir = _local_client(monkeypatch, tmp_path)

    resp = client.post(
        "/upload",
        data={"csrf_token": "x", "files": (io.BytesIO(_PNG), "x.png")},
        content_type="multipart/form-data",
    )
    # Unauthenticated browsers are redirected to the login page; no file is saved.
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    assert not (data_dir / "input").exists()
    assert not (data_dir / "x.png").exists()


def test_upload_custom_upload_dir_outside_gallery_redirects_to_root(monkeypatch, tmp_path):
    outside = tmp_path / "shared"
    client, data_dir = _local_client(monkeypatch, tmp_path, extra_env={"UPLOAD_DIR": str(outside)})
    csrf = _login(client)

    resp = client.post(
        "/upload",
        data={"csrf_token": csrf, "files": (io.BytesIO(_PNG), "shot.png")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"
    assert (outside / "shot.png").read_bytes() == _PNG
    # Button must not advertise the destination in the UI (it is outside the gallery).
    html = client.get("/").get_data(as_text=True)
    assert 'id="uploadForm"' not in html
