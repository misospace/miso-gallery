"""Tests for the service worker (Issue #453).

The SW must never cache authenticated navigation responses, and it must
purge every cache when it receives { type: "auth_changed" } from the page
after login/logout.
"""

import re
from pathlib import Path

import pytest

from conftest import build_client

SW_PATH = Path(__file__).resolve().parent.parent / "templates" / "service-worker.js"


@pytest.fixture
def client(monkeypatch, tmp_path):
    c, _ = build_client(monkeypatch, tmp_path)
    return c


@pytest.fixture(scope="module")
def sw_source():
    return SW_PATH.read_text(encoding="utf-8")


def test_service_worker_served(client):
    resp = client.get("/service-worker.js")
    assert resp.status_code == 200
    assert b"miso-gallery" in resp.data


def test_navigation_responses_are_not_cached(sw_source):
    # The navigate branch must be network-first with no cache write.
    navigate_block = re.search(
        r'request\.mode === "navigate".*?return;', sw_source, re.DOTALL
    )
    assert navigate_block, "navigate branch missing from service worker"
    assert "cache.put" not in navigate_block.group(0)
    assert "caches.open" not in navigate_block.group(0)


def test_authenticated_pages_not_precached(sw_source):
    # CORE_ASSETS must only contain unauthenticated static assets.
    core = re.search(r"const CORE_ASSETS = \[(.*?)\];", sw_source, re.DOTALL)
    assert core
    for page in ('"/"', '"/recent"', '"/trash"', '"/settings"', '"/about"'):
        assert page not in core.group(1)


def test_core_assets_resolve_from_running_app(client, sw_source):
    """Every URL in CORE_ASSETS must return 200 from the running app.

    cache.addAll() is all-or-nothing: a single 404 in the list rejects the
    whole install step, so the SW never installs, never activates, and never
    claims clients (Issue #479).
    """
    core = re.search(r"const CORE_ASSETS = \[(.*?)\];", sw_source, re.DOTALL)
    assert core, "CORE_ASSETS missing from service worker"
    urls = re.findall(r'"([^"]+)"', core.group(1))
    assert urls, "CORE_ASSETS is empty"
    for url in urls:
        resp = client.get(url)
        assert resp.status_code == 200, f"CORE_ASSETS entry {url} returned {resp.status_code}"


def test_images_are_not_cached(sw_source):
    image_block = re.search(
        r'request\.destination === "image".*?return;', sw_source, re.DOTALL
    )
    assert image_block
    assert "cache.put" not in image_block.group(0)


def test_auth_changed_message_purges_all_caches(sw_source):
    msg_block = re.search(
        r'addEventListener\("message".*?\}\);', sw_source, re.DOTALL
    )
    assert msg_block
    assert 'event.data.type === "auth_changed"' in msg_block.group(0)
    assert "caches.keys()" in msg_block.group(0)
    assert "caches.delete" in msg_block.group(0)


def test_pages_post_auth_changed_on_state_change():
    """Every page that registers the SW must post auth_changed when the
    auth state changes (login/logout/OIDC callback landing)."""
    for name in ("index.html", "recent.html", "trash.html", "login.html", "settings.html"):
        src = (Path(__file__).resolve().parent.parent / "templates" / name).read_text(
            encoding="utf-8"
        )
        assert "serviceWorker" in src, f"{name} does not register the SW"
        assert "auth_changed" in src, f"{name} does not post auth_changed"
        assert "miso-auth-state" in src, f"{name} does not track auth state"


def test_login_page_purges_sw_cache_on_state_change(client):
    """The /login landing page (target of /logout) renders the inline
    script that posts auth_changed to the active SW (Issue #453)."""
    resp = client.get("/login")
    assert resp.status_code == 200
    assert b"auth_changed" in resp.data
    assert b"miso-auth-state" in resp.data


def test_index_page_purges_sw_cache_on_state_change(client):
    """The / landing page (target of a successful POST /auth or the OIDC
    callback) renders the inline script that posts auth_changed to the
    active SW (Issue #453)."""
    resp = client.get("/", follow_redirects=True)
    assert resp.status_code == 200
    assert b"auth_changed" in resp.data
    assert b"miso-auth-state" in resp.data


def test_logout_lands_on_purging_page(client):
    """After /logout the browser lands on /login, which posts auth_changed
    to the active SW so the next user cannot receive cached content."""
    resp = client.get("/logout", follow_redirects=True)
    assert resp.status_code == 200
    assert b"auth_changed" in resp.data
