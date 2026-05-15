import re

import pytest

from conftest import build_client


def _extract_csrf(html: str) -> str:
    m = re.search(r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']', html)
    assert m, "csrf_token not found in login form"
    return m.group(1)


def _build_auth_client(monkeypatch, tmp_path, *, auth_type: str, admin_password: str = "", oidc_enabled: bool = False):
    """Thin wrapper around conftest.build_client for auth-specific env vars."""
    extra_env = {}
    if oidc_enabled:
        extra_env["OIDC_ENABLED"] = "true"
        extra_env["OIDC_ISSUER"] = "https://issuer.example"
        extra_env["OIDC_CLIENT_ID"] = "client"
        extra_env["OIDC_CLIENT_SECRET"] = "secret"

    # Override ADMIN_PASSWORD if explicitly provided (e.g. admin_password="pass123")
    if admin_password:
        extra_env["ADMIN_PASSWORD"] = admin_password

    client, _ = build_client(monkeypatch, tmp_path, auth_type=auth_type, extra_env=extra_env)
    return client


def test_auth_none_root_is_public(monkeypatch, tmp_path):
    client = _build_auth_client(monkeypatch, tmp_path, auth_type="none")
    resp = client.get("/")
    assert resp.status_code == 200


def test_auth_local_unauth_redirects_to_login(monkeypatch, tmp_path):
    client = _build_auth_client(monkeypatch, tmp_path, auth_type="local", admin_password="pass123")
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login?next=/" in resp.headers["Location"]


def test_auth_local_password_matrix(monkeypatch, tmp_path):
    client = _build_auth_client(monkeypatch, tmp_path, auth_type="local", admin_password="pass123")

    login_page = client.get("/login")
    csrf = _extract_csrf(login_page.get_data(as_text=True))

    bad = client.post(
        "/auth",
        data={"password": "wrong", "next": "/", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert bad.status_code == 302
    assert "/login?error=invalid" in bad.headers["Location"]

    login_page2 = client.get("/login")
    csrf2 = _extract_csrf(login_page2.get_data(as_text=True))
    ok = client.post(
        "/auth",
        data={"password": "pass123", "next": "/", "csrf_token": csrf2},
        follow_redirects=False,
    )
    assert ok.status_code == 302
    assert ok.headers["Location"].endswith("/")

    root = client.get("/")
    assert root.status_code == 200


def test_auth_oidc_unauth_redirects_and_local_auth_disabled(monkeypatch, tmp_path):
    client = _build_auth_client(monkeypatch, tmp_path, auth_type="oidc", oidc_enabled=True)

    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login?next=/" in resp.headers["Location"]

    # In OIDC mode the local password form may be hidden, so seed CSRF directly.
    with client.session_transaction() as sess:
        sess["csrf_token"] = "test-csrf"

    post = client.post(
        "/auth",
        data={"password": "anything", "next": "/", "csrf_token": "test-csrf"},
        follow_redirects=False,
    )
    assert post.status_code == 302
    assert "error=local_disabled" in post.headers["Location"]


def test_images_route_is_public_even_with_auth_enabled(monkeypatch, tmp_path):
    client = _build_auth_client(monkeypatch, tmp_path, auth_type="local", admin_password="pass123")
    resp = client.get("/images/sample.png", follow_redirects=False)
    assert resp.status_code == 200


def test_root_gallery_renders_inline_details_panel(monkeypatch, tmp_path):
    client = _build_auth_client(monkeypatch, tmp_path, auth_type="none")
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "<summary>Details</summary>" in html
    assert "Path</span><span class=\"image-details-value\">sample.png" in html
    assert "content-visibility:auto" in html
    assert "contain-intrinsic-size:260px 320px" in html
    assert "fetchpriority=\"low\"" in html


def test_bulk_delete_redirects_with_feedback(monkeypatch, tmp_path):
    client = _build_auth_client(monkeypatch, tmp_path, auth_type="none")

    with client.session_transaction() as sess:
        sess["csrf_token"] = "bulk-csrf"

    resp = client.post(
        "/bulk-delete",
        data={
            "csrf_token": "bulk-csrf",
            "current_subpath": "",
            "filenames": ["sample.png"],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "bulk_state=success" in resp.headers["Location"]
    assert "bulk_deleted=1" in resp.headers["Location"]

    follow = client.get(resp.headers["Location"])
    body = follow.get_data(as_text=True)
    assert follow.status_code == 200
    assert "Moved 1 image to trash. Selection cleared." in body


def test_bulk_toolbar_shows_download_unavailable_fallback(monkeypatch, tmp_path):
    client = _build_auth_client(monkeypatch, tmp_path, auth_type="none")
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Download selected (unavailable)" in body
    assert "Bulk download is not available yet. Use each item’s direct view/thumb actions for now." in body
    assert "@media (max-width: 640px)" in body
    assert ".selection-actions button" in body


def test_bulk_toolbar_buttons_reflect_selection_state(monkeypatch, tmp_path):
    client = _build_auth_client(monkeypatch, tmp_path, auth_type="none")
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "if (selectAllBtn) { selectAllBtn.disabled = totalCount === 0 || selectedCount === totalCount; }" in body
    assert "if (deselectAllBtn) { deselectAllBtn.disabled = selectedCount === 0; }" in body
    assert ".toolbar button:disabled { opacity:0.5; cursor:not-allowed; }" in body
