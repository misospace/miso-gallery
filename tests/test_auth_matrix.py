import re
import sys
from pathlib import Path

from conftest import build_client

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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


# ---------------------------------------------------------------------------
# verify_oidc_authorization tests
# ---------------------------------------------------------------------------

def _reload_auth(monkeypatch, extra_env: dict | None = None):
    """Force-reload the auth module so env var changes take effect."""
    if extra_env:
        for k, v in extra_env.items():
            monkeypatch.setenv(k, str(v))
    sys.modules.pop("auth", None)
    import auth  # noqa: F401


def _user_info(email="alice@example.com", groups=None, claims=None):
    """Build a minimal OIDC user_info dict."""
    info: dict = {"email": email}
    if groups is not None:
        info["groups"] = groups
    if claims:
        info.update(claims)
    return info


def test_oidc_auth_no_config_allows_all(monkeypatch, tmp_path):
    """With no authorization config set, all users pass."""
    _reload_auth(monkeypatch, extra_env={
        "OIDC_ALLOWED_DOMAINS": "",
        "OIDC_ALLOWED_GROUPS": "",
        "OIDC_REQUIRED_CLAIMS": "",
    })
    import auth
    allowed, reason = auth.verify_oidc_authorization(_user_info())
    assert allowed is True
    assert reason is None


def test_oidc_auth_domain_allowlist_pass(monkeypatch, tmp_path):
    """User email domain matches the allowlist → allowed."""
    _reload_auth(monkeypatch, extra_env={
        "OIDC_ALLOWED_DOMAINS": "example.com,corp.io",
        "OIDC_ALLOWED_GROUPS": "",
        "OIDC_REQUIRED_CLAIMS": "",
    })
    import auth
    allowed, reason = auth.verify_oidc_authorization(_user_info(email="alice@example.com"))
    assert allowed is True
    assert reason is None

    # Case-insensitive domain check
    allowed2, _ = auth.verify_oidc_authorization(_user_info(email="BOB@EXAMPLE.COM"))
    assert allowed2 is True


def test_oidc_auth_domain_allowlist_fail(monkeypatch, tmp_path):
    """User email domain NOT in allowlist → denied."""
    _reload_auth(monkeypatch, extra_env={
        "OIDC_ALLOWED_DOMAINS": "example.com",
        "OIDC_ALLOWED_GROUPS": "",
        "OIDC_REQUIRED_CLAIMS": "",
    })
    import auth
    allowed, reason = auth.verify_oidc_authorization(_user_info(email="bob@evil.com"))
    assert allowed is False
    assert "domain 'evil.com' not in allowed domains" in reason


def test_oidc_auth_domain_allowlist_no_email(monkeypatch, tmp_path):
    """User has no email → denied."""
    _reload_auth(monkeypatch, extra_env={
        "OIDC_ALLOWED_DOMAINS": "example.com",
        "OIDC_ALLOWED_GROUPS": "",
        "OIDC_REQUIRED_CLAIMS": "",
    })
    import auth
    allowed, reason = auth.verify_oidc_authorization({"sub": "abc123"})
    assert allowed is False
    assert "no email in user info" in reason


def test_oidc_auth_group_allowlist_pass(monkeypatch, tmp_path):
    """User belongs to an allowed group → allowed."""
    _reload_auth(monkeypatch, extra_env={
        "OIDC_ALLOWED_DOMAINS": "",
        "OIDC_ALLOWED_GROUPS": "admins,developers",
        "OIDC_REQUIRED_CLAIMS": "",
    })
    import auth
    allowed, reason = auth.verify_oidc_authorization(
        _user_info(groups=["users", "developers"])
    )
    assert allowed is True
    assert reason is None


def test_oidc_auth_group_allowlist_fail(monkeypatch, tmp_path):
    """User belongs to no allowed group → denied."""
    _reload_auth(monkeypatch, extra_env={
        "OIDC_ALLOWED_DOMAINS": "",
        "OIDC_ALLOWED_GROUPS": "admins,developers",
        "OIDC_REQUIRED_CLAIMS": "",
    })
    import auth
    allowed, reason = auth.verify_oidc_authorization(
        _user_info(groups=["users", "guests"])
    )
    assert allowed is False
    assert "no matching group" in reason


def test_oidc_auth_group_allowlist_no_groups(monkeypatch, tmp_path):
    """User has no groups field → denied."""
    _reload_auth(monkeypatch, extra_env={
        "OIDC_ALLOWED_DOMAINS": "",
        "OIDC_ALLOWED_GROUPS": "admins",
        "OIDC_REQUIRED_CLAIMS": "",
    })
    import auth
    allowed, reason = auth.verify_oidc_authorization(_user_info())
    assert allowed is False
    assert "no matching group" in reason


def test_oidc_auth_required_claims_pass(monkeypatch, tmp_path):
    """All required claims match → allowed."""
    _reload_auth(monkeypatch, extra_env={
        "OIDC_ALLOWED_DOMAINS": "",
        "OIDC_ALLOWED_GROUPS": "",
        "OIDC_REQUIRED_CLAIMS": '{"department": "engineering", "role": "engineer"}',
    })
    import auth
    allowed, reason = auth.verify_oidc_authorization(
        _user_info(claims={"department": "engineering", "role": "engineer"})
    )
    assert allowed is True
    assert reason is None


def test_oidc_auth_required_claims_fail(monkeypatch, tmp_path):
    """One required claim mismatch → denied."""
    _reload_auth(monkeypatch, extra_env={
        "OIDC_ALLOWED_DOMAINS": "",
        "OIDC_ALLOWED_GROUPS": "",
        "OIDC_REQUIRED_CLAIMS": '{"department": "engineering"}',
    })
    import auth
    allowed, reason = auth.verify_oidc_authorization(
        _user_info(claims={"department": "marketing"})
    )
    assert allowed is False
    assert "claim 'department' mismatch" in reason


def test_oidc_auth_required_claims_missing(monkeypatch, tmp_path):
    """Required claim not present in user info → denied."""
    _reload_auth(monkeypatch, extra_env={
        "OIDC_ALLOWED_DOMAINS": "",
        "OIDC_ALLOWED_GROUPS": "",
        "OIDC_REQUIRED_CLAIMS": '{"department": "engineering"}',
    })
    import auth
    allowed, reason = auth.verify_oidc_authorization(_user_info())
    assert allowed is False
    assert "claim 'department' mismatch" in reason


def test_oidc_auth_combined_domain_and_group_fail_on_domain(monkeypatch, tmp_path):
    """When both domain and group are configured, domain check runs first."""
    _reload_auth(monkeypatch, extra_env={
        "OIDC_ALLOWED_DOMAINS": "example.com",
        "OIDC_ALLOWED_GROUPS": "admins",
        "OIDC_REQUIRED_CLAIMS": "",
    })
    import auth
    # Wrong domain → denied even though group matches
    allowed, reason = auth.verify_oidc_authorization(
        _user_info(email="bob@evil.com", groups=["admins"])
    )
    assert allowed is False
    assert "domain 'evil.com' not in allowed domains" in reason


def test_oidc_auth_combined_domain_and_group_fail_on_group(monkeypatch, tmp_path):
    """Domain passes but group fails → denied on group check."""
    _reload_auth(monkeypatch, extra_env={
        "OIDC_ALLOWED_DOMAINS": "example.com",
        "OIDC_ALLOWED_GROUPS": "admins",
        "OIDC_REQUIRED_CLAIMS": "",
    })
    import auth
    allowed, reason = auth.verify_oidc_authorization(
        _user_info(email="alice@example.com", groups=["users"])
    )
    assert allowed is False
    assert "no matching group" in reason


def test_oidc_auth_malformed_json_warns(monkeypatch, tmp_path, caplog):
    """Malformed OIDC_REQUIRED_CLAIMS JSON logs a warning instead of crashing."""
    _reload_auth(monkeypatch, extra_env={
        "OIDC_ALLOWED_DOMAINS": "",
        "OIDC_ALLOWED_GROUPS": "",
        "OIDC_REQUIRED_CLAIMS": "{not valid json!!!",
    })
    # Re-import auth to trigger the parsing path
    sys.modules.pop("auth", None)
    import auth  # noqa: F401

    assert auth.OIDC_REQUIRED_CLAIMS == {}
    assert any("OIDC_REQUIRED_CLAIMS has invalid JSON" in r.message for r in caplog.records)


def test_oidc_auth_malformed_json_allows_all(monkeypatch, tmp_path):
    """When OIDC_REQUIRED_CLAIMS is malformed, it defaults to {} and all users pass."""
    _reload_auth(monkeypatch, extra_env={
        "OIDC_ALLOWED_DOMAINS": "",
        "OIDC_ALLOWED_GROUPS": "",
        "OIDC_REQUIRED_CLAIMS": "{{{{",
    })
    sys.modules.pop("auth", None)
    import auth  # noqa: F401

    allowed, reason = auth.verify_oidc_authorization(_user_info())
    assert allowed is True
    assert reason is None
