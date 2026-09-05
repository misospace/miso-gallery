import re
import sys
from pathlib import Path

from conftest import build_client

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_MINIMAL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"
)


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


def test_view_and_images_do_not_serve_non_media_files(monkeypatch, tmp_path):
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="local", extra_env={"ADMIN_PASSWORD": "pass123"})
    (data_dir / "secret.env").write_text("SECRET=1")
    (data_dir / "notes.txt").write_text("hello")
    assert client.get("/view/secret.env").status_code == 404
    assert client.get("/view/notes.txt").status_code == 404
    assert client.get("/images/secret.env").status_code == 404
    assert client.get("/images/notes.txt").status_code == 404


def test_view_and_images_reject_symlinks_outside_data_folder(monkeypatch, tmp_path):
    """#442: /view/ and /images/ must not follow symlinks that point outside DATA_FOLDER."""
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "passwd"
    secret.write_bytes(b"root:x:0:0:root:/root:/bin/bash")
    (data_dir / "foo.png").symlink_to(secret)

    for route in ("/view/foo.png", "/images/foo.png"):
        resp = client.get(route)
        assert resp.status_code == 404
        assert b"root:x:0:0" not in resp.data


def test_view_serves_symlink_inside_data_folder(monkeypatch, tmp_path):
    """#442: symlinks that resolve back inside DATA_FOLDER are still served."""
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")
    (data_dir / "other").mkdir()
    (data_dir / "other" / "real.png").write_bytes(_MINIMAL_PNG)
    (data_dir / "link.png").symlink_to(data_dir / "other" / "real.png")

    resp = client.get("/view/link.png")
    assert resp.status_code == 200
    assert resp.content_type.startswith("image/")
    assert resp.data == _MINIMAL_PNG


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


def test_index_excludes_hidden_files_and_symlinks(monkeypatch, tmp_path):
    """#444: index() must apply the same hidden-segment + symlink exclusions
    that iter_gallery_items() uses, so dotfiles and symlinks cannot surface in
    the gallery UI even when an authenticated user has shell-like write
    access to DATA_FOLDER.
    """
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")
    # Hidden / dot-prefixed artifacts that would currently leak.
    (data_dir / ".DS_Store").write_bytes(b"\x00")
    (data_dir / ".git").mkdir()
    (data_dir / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
    # Symlinks -- one a file, one a directory -- pointing outside DATA_FOLDER.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "real.png").write_bytes(_MINIMAL_PNG)
    (data_dir / "link.png").symlink_to(outside / "real.png")
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "leak.png").write_bytes(_MINIMAL_PNG)
    (data_dir / "linkdir").symlink_to(outside_dir)

    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    for forbidden in (".git", "HEAD", ".DS_Store", "link.png", "linkdir"):
        assert forbidden not in body, (
            f"index() must not surface {forbidden!r} in the gallery UI"
        )


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


# ---------------------------------------------------------------------------
# API key identification and key class tests (issue #201)
# ---------------------------------------------------------------------------

def _reload_api_keys(monkeypatch, *, read_keys: str = "", write_keys: str = ""):
    """Set API keys and force-reload the auth module."""
    monkeypatch.setenv("LLM_READ_API_KEYS", read_keys)
    monkeypatch.setenv("LLM_WRITE_API_KEYS", write_keys)
    # Also clear legacy to avoid interference
    monkeypatch.setenv("LLM_API_KEYS", "")
    sys.modules.pop("auth", None)
    import auth  # noqa: F401


def test_find_matching_key_returns_class_read(monkeypatch, tmp_path):
    """_find_matching_key returns 'read' as the scope class for read keys."""
    _reload_api_keys(monkeypatch, read_keys="read-key-1234", write_keys="")
    import auth
    matched, hint, key_class = auth._find_matching_key("read-key-1234", "read")
    assert matched is True
    assert hint is not None
    assert len(hint) < len("read-key-1234")  # hint is obfuscated
    assert key_class == "read"


def test_find_matching_key_returns_class_write(monkeypatch, tmp_path):
    """_find_matching_key returns 'write' as the scope class for write keys."""
    _reload_api_keys(monkeypatch, read_keys="read-key-1234", write_keys="write-key-5678")
    import auth
    matched, hint, key_class = auth._find_matching_key("write-key-5678", "write")
    assert matched is True
    assert hint is not None
    assert len(hint) < len("write-key-5678")  # hint is obfuscated
    assert key_class == "write"


def test_find_matching_key_fails_returns_none_class(monkeypatch, tmp_path):
    """_find_matching_key returns (False, None, None) for wrong token."""
    _reload_api_keys(monkeypatch, read_keys="read-key-1234", write_keys="")
    import auth
    matched, hint, key_class = auth._find_matching_key("wrong-token", "read")
    assert matched is False
    assert hint is None
    assert key_class is None


def test_find_matching_key_empty_token_returns_none(monkeypatch, tmp_path):
    """_find_matching_key returns (False, None, None) for empty token."""
    _reload_api_keys(monkeypatch, read_keys="read-key-1234", write_keys="")
    import auth
    matched, hint, key_class = auth._find_matching_key("", "read")
    assert matched is False
    assert hint is None
    assert key_class is None


def test_api_key_hint_short_cuts_for_keys_under_8_chars(monkeypatch, tmp_path):
    """Keys shorter than 8 chars show first 2 + '...'."""
    _reload_api_keys(monkeypatch, read_keys="short", write_keys="")
    import auth
    hint = auth._api_key_hint("short")
    assert hint == "sh..."


def test_api_key_hint_full_format(monkeypatch, tmp_path):
    """Keys 8+ chars show first 4 and last 4."""
    _reload_api_keys(monkeypatch, read_keys="read-key-1234", write_keys="")
    import auth
    hint = auth._api_key_hint("read-key-1234")
    assert hint == "read...1234"


def test_api_key_hint_empty_key(monkeypatch, tmp_path):
    """Empty key returns empty string."""
    _reload_api_keys(monkeypatch, read_keys="", write_keys="")
    import auth
    hint = auth._api_key_hint("")
    assert hint == ""


def test_api_key_hint_none_key(monkeypatch, tmp_path):
    """None key returns empty string."""
    _reload_api_keys(monkeypatch, read_keys="", write_keys="")
    import auth
    hint = auth._api_key_hint(None)  # type: ignore[arg-type]
    assert hint == ""


def test_find_matching_key_legacy_fallback_returns_class(monkeypatch, tmp_path):
    """Legacy LLM_API_KEYS are used when no read/write keys configured."""
    monkeypatch.setenv("LLM_READ_API_KEYS", "")
    monkeypatch.setenv("LLM_WRITE_API_KEYS", "")
    monkeypatch.setenv("LLM_API_KEYS", "legacy-key-99")
    sys.modules.pop("auth", None)
    import auth  # noqa: F401

    matched, hint, key_class = auth._find_matching_key("legacy-key-99", "read")
    assert matched is True
    assert key_class == "read"


def test_find_matching_key_write_falls_back_to_legacy(monkeypatch, tmp_path):
    """When no write keys set but legacy exists, write scope matches legacy."""
    monkeypatch.setenv("LLM_READ_API_KEYS", "")
    monkeypatch.setenv("LLM_WRITE_API_KEYS", "")
    monkeypatch.setenv("LLM_API_KEYS", "legacy-key-99")
    sys.modules.pop("auth", None)
    import auth  # noqa: F401

    matched, hint, key_class = auth._find_matching_key("legacy-key-99", "write")
    assert matched is True
    assert key_class == "write"


# ---------------------------------------------------------------------------
# OIDC route rate limiting (issue #446)
# ---------------------------------------------------------------------------

def setup_function():
    """Reset the in-memory rate limiter before each test to avoid cross-test pollution."""
    from security import FALLBACK_LIMITER
    FALLBACK_LIMITER.reset()


def test_oidc_login_rate_limited(monkeypatch, tmp_path):
    """/auth/oidc must return 429 after 20 requests in a 60s window.

    A single real login (one authorize_redirect) stays well under the limit,
    but an unauthenticated attacker spamming the route is cut off before it
    can force repeated state generation and session writes.
    """
    import flask

    client = _build_auth_client(monkeypatch, tmp_path, auth_type="oidc", oidc_enabled=True)
    import app as app_module

    calls = {"n": 0}

    def fake_authorize_redirect(*args, **kwargs):
        calls["n"] += 1
        return flask.Response("redirected")

    monkeypatch.setattr(app_module.oauth.oidc, "authorize_redirect", fake_authorize_redirect)

    # The first 20 requests (a real user logging in once is far below this) pass.
    for _i in range(20):
        resp = client.get("/auth/oidc", follow_redirects=False)
        assert resp.status_code == 200
    assert calls["n"] == 20

    # The 21st request is rate-limited and never reaches the provider.
    resp = client.get("/auth/oidc", follow_redirects=False)
    assert resp.status_code == 429
    payload = resp.get_json()
    assert payload["error"] == "Rate limit exceeded"
    assert calls["n"] == 20  # authorize_redirect was NOT called again


def test_oidc_callback_rate_limited(monkeypatch, tmp_path):
    """/auth/oidc/callback must return 429 after 20 requests in a 60s window.

    The callback is the route that forces a token-endpoint round trip to the
    external OIDC provider per call, so it must be bounded even for
    unauthenticated callers.
    """
    client = _build_auth_client(monkeypatch, tmp_path, auth_type="oidc", oidc_enabled=True)
    import app as app_module

    calls = {"n": 0}

    def fake_authorize_access_token(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("mock provider failure")

    monkeypatch.setattr(app_module.oauth.oidc, "authorize_access_token", fake_authorize_access_token)

    # The first 20 requests reach the handler (each fails the mock token
    # exchange and redirects to the login page with error=oidc_failed).
    for _i in range(20):
        resp = client.get("/auth/oidc/callback", query_string={"code": "x", "state": "y"}, follow_redirects=False)
        assert resp.status_code == 302
        assert "error=oidc_failed" in resp.headers["Location"]
    assert calls["n"] == 20

    # The 21st request is rate-limited and never reaches the provider.
    resp = client.get("/auth/oidc/callback", query_string={"code": "x", "state": "y"}, follow_redirects=False)
    assert resp.status_code == 429
    payload = resp.get_json()
    assert payload["error"] == "Rate limit exceeded"
    assert calls["n"] == 20  # authorize_access_token was NOT called again


def test_oidc_refresh_rate_limited(monkeypatch, tmp_path):
    """/auth/oidc/refresh must return 429 after 5 requests in a 300s window.

    Even an authenticated session with a stale refresh token cannot spam the
    OIDC provider's token endpoint without bound. The limit matches /auth
    (Issue #454).
    """
    client = _build_auth_client(monkeypatch, tmp_path, auth_type="oidc", oidc_enabled=True)

    # The route is POST-only and CSRF-protected; seed a valid token so the
    # requests reach the auth check.
    with client.session_transaction() as sess:
        sess["csrf_token"] = "test-csrf"

    # Unauthenticated: each request is rejected with 401, but still counts
    # against the rate limit.
    for _i in range(5):
        resp = client.post("/auth/oidc/refresh", data={"csrf_token": "test-csrf"})
        assert resp.status_code == 401

    # The 6th request is rate-limited.
    resp = client.post("/auth/oidc/refresh", data={"csrf_token": "test-csrf"})
    assert resp.status_code == 429
    payload = resp.get_json()
    assert payload["error"] == "Rate limit exceeded"


# ---------------------------------------------------------------------------
# resolved_auth_mode() must not claim "oidc" without OIDC env (issue #449)
# ---------------------------------------------------------------------------

def test_resolved_auth_mode_oidc_without_env_returns_none(monkeypatch, tmp_path):
    """AUTH_TYPE=oidc but OIDC env vars missing → resolved mode is "none".

    Previously the function short-circuited on AUTH_TYPE and returned "oidc",
    which made the about page lie about the auth mode while the only form
    actually rendered was the local password login. Without ADMIN_PASSWORD
    the app must not be mislabelled as OIDC, so the effective mode is
    "none" and the gallery is publicly accessible.
    """
    _reload_auth(monkeypatch, extra_env={
        "AUTH_TYPE": "oidc",
        "ADMIN_PASSWORD": "",
        "OIDC_ENABLED": "false",
        "OIDC_ISSUER": "",
        "OIDC_CLIENT_ID": "",
        "OIDC_CLIENT_SECRET": "",
    })
    import auth
    assert auth.resolved_auth_mode() == "none"
    assert auth.is_auth_enabled() is False


def test_resolved_auth_mode_oidc_without_env_falls_back_to_local(monkeypatch, tmp_path):
    """AUTH_TYPE=oidc with missing OIDC env but ADMIN_PASSWORD set → "local".

    Operators who misconfigure OIDC must not be locked out: if they have
    set ADMIN_PASSWORD, the local password form is what the login page
    actually offers, and resolved_auth_mode() must agree so the about page
    does not report auth_mode=oidc while only the local form is reachable.
    """
    _reload_auth(monkeypatch, extra_env={
        "AUTH_TYPE": "oidc",
        "ADMIN_PASSWORD": "pass123",
        "OIDC_ENABLED": "false",
        "OIDC_ISSUER": "",
        "OIDC_CLIENT_ID": "",
        "OIDC_CLIENT_SECRET": "",
    })
    import auth
    assert auth.resolved_auth_mode() == "local"
    # is_oidc_configured() must still be False — the about page contract
    # is that auth_mode agrees with oidc_configured.
    assert auth.is_oidc_configured() is False


def test_resolved_auth_mode_oidc_partial_env_falls_back(monkeypatch, tmp_path):
    """AUTH_TYPE=oidc with a partial OIDC env (missing one of the four) → not "oidc"."""
    # OIDC_ENABLED=true but the rest missing — is_oidc_configured() returns False.
    _reload_auth(monkeypatch, extra_env={
        "AUTH_TYPE": "oidc",
        "ADMIN_PASSWORD": "pass123",
        "OIDC_ENABLED": "true",
        "OIDC_ISSUER": "",
        "OIDC_CLIENT_ID": "",
        "OIDC_CLIENT_SECRET": "",
    })
    import auth
    assert auth.is_oidc_configured() is False
    assert auth.resolved_auth_mode() == "local"


def test_resolved_auth_mode_oidc_fully_configured_returns_oidc(monkeypatch, tmp_path):
    """AUTH_TYPE=oidc with the full OIDC env still resolves to "oidc"."""
    _reload_auth(monkeypatch, extra_env={
        "AUTH_TYPE": "oidc",
        "ADMIN_PASSWORD": "",
        "OIDC_ENABLED": "true",
        "OIDC_ISSUER": "https://issuer.example",
        "OIDC_CLIENT_ID": "client",
        "OIDC_CLIENT_SECRET": "secret",
    })
    import auth
    assert auth.resolved_auth_mode() == "oidc"
    assert auth.is_oidc_configured() is True


def test_resolved_auth_mode_local_with_oidc_env_returns_oidc(monkeypatch, tmp_path):
    """AUTH_TYPE=local with OIDC env vars fully set still resolves to "oidc".

    This preserves the pre-#449 behaviour: operators who set OIDC env vars
    but leave AUTH_TYPE at the default "local" get OIDC if it is configured.
    """
    _reload_auth(monkeypatch, extra_env={
        "AUTH_TYPE": "local",
        "ADMIN_PASSWORD": "",
        "OIDC_ENABLED": "true",
        "OIDC_ISSUER": "https://issuer.example",
        "OIDC_CLIENT_ID": "client",
        "OIDC_CLIENT_SECRET": "secret",
    })
    import auth
    assert auth.resolved_auth_mode() == "oidc"


def test_about_page_does_not_show_contradictory_auth_state_when_oidc_misconfigured(monkeypatch, tmp_path):
    """/about must not show auth_mode=oidc alongside oidc_configured=false.

    When AUTH_TYPE=oidc is set but the OIDC env is missing, the about page
    must show the resolved mode ("local" if ADMIN_PASSWORD is set,
    otherwise "none") rather than the misleading combination auth_mode=oidc
    + oidc_configured=false (Issue #449).
    """
    # NOTE: build_client deletes ADMIN_PASSWORD when auth_type != "local",
    # so we build with auth_type="local" and override AUTH_TYPE=oidc through
    # extra_env — the env vars read at module-import time win regardless.
    extra_env = {
        "AUTH_TYPE": "oidc",
        "OIDC_ENABLED": "false",
        "OIDC_ISSUER": "",
        "OIDC_CLIENT_ID": "",
        "OIDC_CLIENT_SECRET": "",
    }
    client, _ = build_client(monkeypatch, tmp_path, auth_type="local", extra_env=extra_env)

    # Authenticate via the local form so we can reach /about.
    login_page = client.get("/login")
    csrf = _extract_csrf(login_page.get_data(as_text=True))
    ok = client.post(
        "/auth",
        data={"password": "pass123", "next": "/about", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert ok.status_code == 302

    resp = client.get("/about")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # auth_mode must reflect the actual local fallback, not the misconfigured "oidc".
    assert "<div>local</div>" in html
    # oidc_configured is genuinely false (no OIDC env) — the two must agree.
    assert "<div>False</div>" in html


def test_auth_type_oidc_without_env_local_password_login_works(monkeypatch, tmp_path):
    """AUTH_TYPE=oidc with missing OIDC env still offers the local form.

    Operators who misconfigured OIDC but set ADMIN_PASSWORD must be able
    to log in with their local password — the previous behaviour did this
    in the UI but then session["auth_method"] disagreed with the resolved
    auth mode (Issue #449).
    """
    # See note above about why auth_type="local" + AUTH_TYPE=oidc in extra_env.
    extra_env = {
        "AUTH_TYPE": "oidc",
        "OIDC_ENABLED": "false",
        "OIDC_ISSUER": "",
        "OIDC_CLIENT_ID": "",
        "OIDC_CLIENT_SECRET": "",
    }
    client, _ = build_client(monkeypatch, tmp_path, auth_type="local", extra_env=extra_env)

    # Unauthenticated root → redirect to /login because auth mode is now "local"
    # (resolved_auth_mode fell back), not "none".
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]

    # The local password form is rendered (no OIDC button — OIDC isn't configured).
    login_page = client.get("/login")
    html = login_page.get_data(as_text=True)
    assert 'name="password"' in html
    assert "OIDC" not in html  # no OIDC login button when OIDC isn't configured

    # Logging in with the local password succeeds and lands on the gallery.
    csrf = _extract_csrf(html)
    ok = client.post(
        "/auth",
        data={"password": "pass123", "next": "/", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert ok.status_code == 302
    assert ok.headers["Location"].endswith("/")
    with client.session_transaction() as sess:
        # The session records the auth method it actually used.
        assert sess["auth_method"] == "local"
