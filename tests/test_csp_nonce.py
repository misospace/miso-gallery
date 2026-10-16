"""Regression tests for issue #534: the per-request CSP nonce must reach templates.

The CSP header carries `script-src 'self' 'nonce-<g.csp_nonce>'` (security.py).
Templates render `<script nonce="{{ csp_nonce }}">`; if the nonce never reaches
the template context the attribute renders empty and browsers reject every
inline script. These tests assert the rendered nonce equals the nonce in the
response's own Content-Security-Policy header.
"""

from __future__ import annotations

import re

from PIL import Image

from conftest import build_client

_NONCE_ATTR_RE = re.compile(r'nonce="([^"]*)"')
_CSP_NONCE_RE = re.compile(r"'nonce-([^']+)'")

MINIMAL_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c"
    b"\x0e\x0c\x0c\x0e\x0c\x0a\x08\x09\x09\x0a\x0e\x12\x14\x0f\x0e\x0d\x0e"
    b"\x14\x18\x1c\x17\x14\x12\x15\x1c\x1e&\x1d\x1a\x1d!#\x1f\x1d\x1e\x1f"
    b"'\x2c\x38%''%\x2c\x1f\x1e!\x2c5\x2c\x30\x35\x38\x38\x38\x38\x38\x38"
    b"\x38\x38\x38\x38\x38\x38\x38\x38\x38\x38\x38\x38\x38\x38\x38\x38\x38"
    b"\x38\x38\x38\x38\x38\x38\x38\x38\x38\x38\x38\x38\x38\x38\x38\x38\x38"
    b"\x38\x38\x38\x38\x38\x38\x38\x38\x38\x38\x38\x38\x38\x38\x38\x38\x38"
    b'\xff\xc0\x00\x11\x08\x00\n\x00\n\x03\x01"\x00\x02\x11\x01\x03\x11\x01'
    b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4"
    b"\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00"
    b'\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07"q\x14\x02\x81'
    b"\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n\x16\x17\x18\x19\x1a%&'"
    b"()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89"
    b"\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8"
    b"\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7"
    b"\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5"
    b"\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda"
    b"\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00?\x00(\xa2\x8a\x28\xff\xd9"
)


def _build_client(monkeypatch, tmp_path):
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")
    (data_dir / "cats").mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (64, 64), color="red")
    img.save(data_dir / "cats" / "cat.jpg")
    return client


def _assert_nonce_matches_csp(resp, label: str) -> None:
    assert resp.status_code == 200, f"{label}: expected 200, got {resp.status_code}"
    csp = resp.headers.get("Content-Security-Policy", "")
    header_nonces = _CSP_NONCE_RE.findall(csp)
    assert len(header_nonces) == 1, f"{label}: expected one nonce in CSP header, got {csp!r}"
    header_nonce = header_nonces[0]
    assert header_nonce, f"{label}: CSP header nonce is empty"

    html = resp.get_data(as_text=True)
    nonces = _NONCE_ATTR_RE.findall(html)
    assert nonces, f"{label}: no nonce= attributes in rendered HTML"
    assert set(nonces) == {header_nonce}, (
        f"{label}: rendered nonces {set(nonces)!r} do not match CSP header nonce {header_nonce!r}"
    )


def test_index_inline_script_nonce_matches_csp_header(monkeypatch, tmp_path):
    client = _build_client(monkeypatch, tmp_path)
    _assert_nonce_matches_csp(client.get("/"), "/")


def test_recent_inline_script_nonce_matches_csp_header(monkeypatch, tmp_path):
    client = _build_client(monkeypatch, tmp_path)
    _assert_nonce_matches_csp(client.get("/recent"), "/recent")


def test_nonce_is_per_request(monkeypatch, tmp_path):
    client = _build_client(monkeypatch, tmp_path)
    first = client.get("/")
    second = client.get("/")
    first_nonce = _CSP_NONCE_RE.search(first.headers["Content-Security-Policy"]).group(1)
    second_nonce = _CSP_NONCE_RE.search(second.headers["Content-Security-Policy"]).group(1)
    assert first_nonce != second_nonce, "nonce should be regenerated per request"
    _assert_nonce_matches_csp(second, "/ (second request)")
