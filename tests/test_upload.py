"""Tests for the in-browser upload button (issue #485).

Uploads land in UPLOAD_DIR (default ``DATA_FOLDER/input``) so ad-hoc
images can be shared without write access to the image share.
"""

import io
import sys
from pathlib import Path

import pytest
from PIL import Image

from conftest import _MINIMAL_PNG, build_client

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_PNG = _MINIMAL_PNG


def _warning_range_png() -> bytes:
    """Valid 16x10 PNG (160 px): above MAX_IMAGE_PIXELS=100, at or below 2x it."""
    buf = io.BytesIO()
    Image.new("RGB", (16, 10)).save(buf, format="PNG")
    return buf.getvalue()


def _isofmt_box(type_: bytes, payload: bytes) -> bytes:
    """Build one ISO-BMFF box: 4-byte big-endian size (payload + header), then type, then payload."""
    return (len(payload) + 8).to_bytes(4, "big") + type_ + payload


def _ftyp(major_brand: bytes = b"isom") -> bytes:
    return _isofmt_box(b"ftyp", major_brand + (512).to_bytes(4, "big") + b"isomiso2mp41")


def _moov() -> bytes:
    """A ``moov`` box carrying a minimum-length version 0 ``mvhd``."""
    return _isofmt_box(b"moov", _isofmt_box(b"mvhd", b"\x00" * 100))


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


def test_upload_rejects_invalid_image_content(monkeypatch, tmp_path):
    client, data_dir = _local_client(monkeypatch, tmp_path)
    csrf = _login(client)

    resp = client.post(
        "/upload",
        data={"csrf_token": csrf, "files": (io.BytesIO(b"not an image"), "evil.png")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 422
    assert not (data_dir / "input" / "evil.png").exists()


def test_upload_rejects_invalid_video_content(monkeypatch, tmp_path):
    client, data_dir = _local_client(monkeypatch, tmp_path)
    csrf = _login(client)

    resp = client.post(
        "/upload",
        data={"csrf_token": csrf, "files": (io.BytesIO(b"not a video"), "evil.mp4")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 422
    assert not (data_dir / "input" / "evil.mp4").exists()


def test_upload_accepts_non_faststart_mp4(monkeypatch, tmp_path):
    """Issue #494: ``moov`` after ``mdat`` is valid; the old 4096-byte window could not see it."""
    client, data_dir = _local_client(monkeypatch, tmp_path)
    csrf = _login(client)
    payload = _ftyp() + _isofmt_box(b"mdat", b"\x00" * (64 * 1024)) + _moov()

    resp = client.post(
        "/upload",
        data={"csrf_token": csrf, "files": (io.BytesIO(payload), "clip.mp4")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    assert (data_dir / "input" / "clip.mp4").read_bytes() == payload


def test_upload_accepts_non_faststart_mov(monkeypatch, tmp_path):
    client, data_dir = _local_client(monkeypatch, tmp_path)
    csrf = _login(client)
    payload = _ftyp(b"qt  ") + _isofmt_box(b"mdat", b"\x00" * (64 * 1024)) + _moov()

    resp = client.post(
        "/upload",
        data={"csrf_token": csrf, "files": (io.BytesIO(payload), "clip.mov")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    assert (data_dir / "input" / "clip.mov").read_bytes() == payload


def test_upload_accepts_mp4_with_64bit_box_sizes(monkeypatch, tmp_path):
    client, data_dir = _local_client(monkeypatch, tmp_path)
    csrf = _login(client)
    mvhd = _isofmt_box(b"mvhd", b"\x00" * 100)
    moov = b"\x00\x00\x00\x01moov" + (len(mvhd) + 16).to_bytes(8, "big") + mvhd
    payload = _ftyp() + _isofmt_box(b"mdat", b"\x00" * (64 * 1024)) + moov

    resp = client.post(
        "/upload",
        data={"csrf_token": csrf, "files": (io.BytesIO(payload), "big.mp4")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    assert (data_dir / "input" / "big.mp4").read_bytes() == payload


def test_upload_accepts_mp4_with_box_extending_to_end_of_file(monkeypatch, tmp_path):
    client, data_dir = _local_client(monkeypatch, tmp_path)
    csrf = _login(client)
    # size 0 declares "this box runs to the end of the file" (32-bit form only).
    payload = _ftyp() + _moov() + b"\x00\x00\x00\x00mdat" + b"\x00" * 128

    resp = client.post(
        "/upload",
        data={"csrf_token": csrf, "files": (io.BytesIO(payload), "tailzero.mp4")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    assert (data_dir / "input" / "tailzero.mp4").read_bytes() == payload


def test_upload_rejects_mp4_without_moov(monkeypatch, tmp_path):
    client, data_dir = _local_client(monkeypatch, tmp_path)
    csrf = _login(client)
    payload = _ftyp() + _isofmt_box(b"mdat", b"\x00" * (64 * 1024))

    resp = client.post(
        "/upload",
        data={"csrf_token": csrf, "files": (io.BytesIO(payload), "nomov.mp4")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 422
    assert not (data_dir / "input" / "nomov.mp4").exists()


def test_upload_rejects_mp4_with_moov_not_first(monkeypatch, tmp_path):
    client, data_dir = _local_client(monkeypatch, tmp_path)
    csrf = _login(client)
    payload = _moov() + _ftyp()

    resp = client.post(
        "/upload",
        data={"csrf_token": csrf, "files": (io.BytesIO(payload), "nofirst.mp4")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 422
    assert not (data_dir / "input" / "nofirst.mp4").exists()


def test_upload_rejects_mp4_box_smaller_than_its_header(monkeypatch, tmp_path):
    client, data_dir = _local_client(monkeypatch, tmp_path)
    csrf = _login(client)
    # A moov box declaring size 4, below its own 8-byte header.
    payload = _ftyp() + (4).to_bytes(4, "big") + b"moov"

    resp = client.post(
        "/upload",
        data={"csrf_token": csrf, "files": (io.BytesIO(payload), "badsize.mp4")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 422
    assert not (data_dir / "input" / "badsize.mp4").exists()


def test_upload_rejects_mp4_with_unrepresentable_box_size(monkeypatch, tmp_path):
    client, data_dir = _local_client(monkeypatch, tmp_path)
    csrf = _login(client)
    # 64-bit box size far beyond the platform offset limit: must not raise out of the validator.
    payload = _ftyp() + b"\x00\x00\x00\x01moov" + (2**63).to_bytes(8, "big")

    resp = client.post(
        "/upload",
        data={"csrf_token": csrf, "files": (io.BytesIO(payload), "huge.mp4")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 422
    assert not (data_dir / "input" / "huge.mp4").exists()


def test_upload_rejects_mp4_with_largesize_below_its_header(monkeypatch, tmp_path):
    client, data_dir = _local_client(monkeypatch, tmp_path)
    csrf = _login(client)
    payload = _ftyp() + b"\x00\x00\x00\x01moov" + (12).to_bytes(8, "big") + _moov()

    resp = client.post(
        "/upload",
        data={"csrf_token": csrf, "files": (io.BytesIO(payload), "small64.mp4")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 422
    assert not (data_dir / "input" / "small64.mp4").exists()


def test_upload_rejects_mp4_with_zero_largesize(monkeypatch, tmp_path):
    client, data_dir = _local_client(monkeypatch, tmp_path)
    csrf = _login(client)
    # A 64-bit box size of 0 is not the 32-bit "runs to end of file" encoding.
    payload = _ftyp() + _moov() + b"\x00\x00\x00\x01free" + (0).to_bytes(8, "big")

    resp = client.post(
        "/upload",
        data={"csrf_token": csrf, "files": (io.BytesIO(payload), "zerolarge.mp4")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 422
    assert not (data_dir / "input" / "zerolarge.mp4").exists()


def test_isobmff_validation_survives_a_forward_only_stream():
    """Without a measurable length the walk still refuses an unrepresentable box size."""
    import os

    from app import _is_valid_isobmff

    class _ForwardOnly:
        """Stream that can only move forwards, like a spooled upload that cannot report its length."""

        def __init__(self, inner):
            self.inner = inner

        def read(self, size=-1):
            return self.inner.read(size)

        def tell(self):
            return self.inner.tell()

        def seek(self, offset, whence=os.SEEK_SET):
            if whence != os.SEEK_CUR:
                raise OSError("stream is forward-only")
            return self.inner.seek(offset, whence)

    oversized = _ftyp() + b"\x00\x00\x00\x01moov" + (2**63).to_bytes(8, "big")
    assert _is_valid_isobmff(_ForwardOnly(io.BytesIO(oversized))) is False
    assert _is_valid_isobmff(_ForwardOnly(io.BytesIO(_ftyp() + _moov()))) is True


def test_isobmff_validation_rejects_truncated_stream_with_unknown_length():
    """A stream that permits forward seeks past EOF but cannot report its length must not pass."""
    import os

    from app import _is_valid_isobmff

    class _SeeksPastEof:
        """Forward seeks always succeed (even past EOF); length cannot be measured."""

        def __init__(self, data):
            self.data = data
            self.pos = 0

        def read(self, size=-1):
            end = len(self.data) if size < 0 else min(self.pos + size, len(self.data))
            chunk = self.data[self.pos:end]
            self.pos = end
            return chunk

        def tell(self):
            return self.pos

        def seek(self, offset, whence=os.SEEK_SET):
            if whence != os.SEEK_CUR:
                raise OSError("stream cannot report its length")
            self.pos += offset
            return self.pos

    # The second box declares a 24-byte body but only 4 payload bytes exist.
    truncated = _ftyp() + (24).to_bytes(4, "big") + b"moov" + b"\x00" * 4
    assert _is_valid_isobmff(_SeeksPastEof(truncated)) is False
    assert _is_valid_isobmff(_SeeksPastEof(_ftyp() + _moov())) is True


def test_upload_rejects_mp4_with_payload_shorter_than_declared(monkeypatch, tmp_path):
    client, data_dir = _local_client(monkeypatch, tmp_path)
    csrf = _login(client)
    # moov claims 1 MiB of payload but only 8 bytes follow: a truncated file.
    payload = _ftyp() + (1024 * 1024).to_bytes(4, "big") + b"moov" + b"\x00" * 8

    resp = client.post(
        "/upload",
        data={"csrf_token": csrf, "files": (io.BytesIO(payload), "shortmoov.mp4")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 422
    assert not (data_dir / "input" / "shortmoov.mp4").exists()


def test_upload_rejects_mp4_past_the_top_level_box_limit(monkeypatch, tmp_path):
    client, data_dir = _local_client(monkeypatch, tmp_path)
    csrf = _login(client)
    filler = _isofmt_box(b"free", b"") * 5000
    payload = _ftyp() + filler + _moov()

    resp = client.post(
        "/upload",
        data={"csrf_token": csrf, "files": (io.BytesIO(payload), "manyboxes.mp4")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 422
    assert not (data_dir / "input" / "manyboxes.mp4").exists()


def test_upload_does_not_follow_symlink_destination(monkeypatch, tmp_path):
    client, data_dir = _local_client(monkeypatch, tmp_path)
    csrf = _login(client)
    input_dir = data_dir / "input"
    input_dir.mkdir()
    outside = tmp_path / "outside.png"
    (input_dir / "target.png").symlink_to(outside)

    resp = client.post(
        "/upload",
        data={"csrf_token": csrf, "files": (io.BytesIO(_PNG), "target.png")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    assert not outside.exists()
    uploaded = [path for path in input_dir.iterdir() if not path.is_symlink()]
    assert len(uploaded) == 1
    assert uploaded[0].read_bytes() == _PNG


def test_upload_sanitizes_traversal_and_null_byte_names(monkeypatch, tmp_path):
    client, data_dir = _local_client(monkeypatch, tmp_path)
    csrf = _login(client)

    resp = client.post(
        "/upload",
        data={"csrf_token": csrf, "files": (io.BytesIO(_PNG), "../safe\x00.png")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    assert (data_dir / "input" / "safe.png").read_bytes() == _PNG


def test_upload_dir_outside_data_folder_fails_at_startup(monkeypatch, tmp_path):
    outside = tmp_path / "shared"
    with pytest.raises(ValueError, match="UPLOAD_DIR must resolve within DATA_FOLDER"):
        _local_client(monkeypatch, tmp_path, extra_env={"UPLOAD_DIR": str(outside)})


def test_upload_dir_symlink_outside_data_folder_fails_at_startup(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    outside = tmp_path / "shared"
    (data_dir / "input").symlink_to(outside)

    with pytest.raises(ValueError, match="UPLOAD_DIR must resolve within DATA_FOLDER"):
        _local_client(monkeypatch, tmp_path, extra_env={"UPLOAD_DIR": str(data_dir / "input")})


def test_upload_rejects_decompression_bomb_warning_image(monkeypatch, tmp_path):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    client, data_dir = _local_client(monkeypatch, tmp_path)
    csrf = _login(client)

    resp = client.post(
        "/upload",
        data={"csrf_token": csrf, "files": (io.BytesIO(_warning_range_png()), "bomb.png")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 422
    assert not (data_dir / "input" / "bomb.png").exists()


def test_upload_batch_skips_warning_image_and_stores_small_image(monkeypatch, tmp_path):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    client, data_dir = _local_client(monkeypatch, tmp_path)
    csrf = _login(client)

    resp = client.post(
        "/upload",
        data={
            "csrf_token": csrf,
            "files": [
                (io.BytesIO(_warning_range_png()), "bomb.png"),
                (io.BytesIO(_PNG), "small.png"),
            ],
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    small = data_dir / "input" / "small.png"
    assert small.exists()
    assert small.read_bytes() == _PNG
    assert not (data_dir / "input" / "bomb.png").exists()


def test_upload_batch_valid_first_then_warning_image(monkeypatch, tmp_path):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    client, data_dir = _local_client(monkeypatch, tmp_path)
    csrf = _login(client)

    resp = client.post(
        "/upload",
        data={
            "csrf_token": csrf,
            "files": [
                (io.BytesIO(_PNG), "small.png"),
                (io.BytesIO(_warning_range_png()), "bomb.png"),
            ],
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    small = data_dir / "input" / "small.png"
    assert small.exists()
    assert small.read_bytes() == _PNG
    assert not (data_dir / "input" / "bomb.png").exists()
