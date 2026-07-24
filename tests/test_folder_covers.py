import time

from PIL import Image

from conftest import build_client


def _build_folder_client(monkeypatch, tmp_path, auto_covers: bool):
    """Build client with folder cover settings using shared bootstrap."""
    extra_env = {
        "GALLERY_AUTO_FOLDER_COVERS": str(auto_covers),
        "GALLERY_COVER_CACHE_TTL": "3600",
    }
    # Use auth_type="none" to match original behavior
    client, data_dir = build_client(
        monkeypatch, tmp_path, auth_type="none", extra_env=extra_env
    )

    folder = data_dir / "albums" / "trip"
    folder.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (128, 128), color="purple")
    img.save(folder / "001.jpg")

    return client


def test_folder_card_uses_nested_image_preview_when_enabled(monkeypatch, tmp_path):
    client = _build_folder_client(monkeypatch, tmp_path, auto_covers=True)

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert "folder-preview" in html
    assert "/thumb/albums/trip/001.jpg" in html


def test_folder_card_uses_icon_when_auto_cover_disabled(monkeypatch, tmp_path):
    client = _build_folder_client(monkeypatch, tmp_path, auto_covers=False)

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert '<img class="folder-preview"' not in html
    assert "📁" in html


def test_folder_card_recovers_when_folder_gains_image_after_empty_cache(
    monkeypatch, tmp_path
):
    extra_env = {
        "GALLERY_AUTO_FOLDER_COVERS": "true",
    }
    client, data_dir = build_client(
        monkeypatch, tmp_path, auth_type="none", extra_env=extra_env
    )

    folder = data_dir / "albums" / "trip"
    folder.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (128, 128), color="purple")
    img.save(folder / "001.jpg")

    first = client.get("/")
    assert first.status_code == 200
    first_html = first.get_data(as_text=True)
    assert "/thumb/albums/trip/001.jpg" in first_html

    empty_folder = data_dir / "albums" / "fresh"
    empty_folder.mkdir(parents=True, exist_ok=True)

    second = client.get("/albums")
    assert second.status_code == 200
    second_html = second.get_data(as_text=True)
    assert "/thumb/albums/fresh/" not in second_html

    img2 = Image.new("RGB", (128, 128), color="orange")
    img2.save(empty_folder / "cover.jpg")

    third = client.get("/albums")
    assert third.status_code == 200
    third_html = third.get_data(as_text=True)
    assert "/thumb/albums/fresh/cover.jpg" in third_html


def test_folder_card_recovers_when_cached_preview_image_is_deleted(
    monkeypatch, tmp_path
):
    extra_env = {
        "GALLERY_AUTO_FOLDER_COVERS": "true",
    }
    client, data_dir = build_client(
        monkeypatch, tmp_path, auth_type="none", extra_env=extra_env
    )

    folder = data_dir / "albums" / "trip"
    folder.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (128, 128), color="purple")
    img.save(folder / "001.jpg")
    backup = Image.new("RGB", (128, 128), color="green")
    backup.save(folder / "002.jpg")

    first = client.get("/")
    assert first.status_code == 200
    first_html = first.get_data(as_text=True)
    assert "/thumb/albums/trip/001.jpg" in first_html

    (folder / "001.jpg").unlink()

    second = client.get("/")
    assert second.status_code == 200
    second_html = second.get_data(as_text=True)
    assert "/thumb/albums/trip/001.jpg" not in second_html
    assert "/thumb/albums/trip/002.jpg" in second_html


def _make_image(path, color="red"):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), color=color).save(path)
    return path


def test_folder_cover_cache_evicts_oldest_entries_when_full(monkeypatch, tmp_path):
    """The folder cover cache must be bounded — oldest entries are evicted at capacity (issue #348)."""
    extra_env = {
        "GALLERY_AUTO_FOLDER_COVERS": "true",
        "GALLERY_COVER_CACHE_TTL": "3600",
        "GALLERY_FOLDER_COVER_CACHE_MAX_SIZE": "2",
    }
    client, data_dir = build_client(
        monkeypatch, tmp_path, auth_type="none", extra_env=extra_env
    )

    palette = ["red", "green", "blue", "yellow"]
    for name in ("a", "b", "c", "d"):
        _make_image(data_dir / "albums" / name / "001.jpg", color=palette.pop(0))

    import app as app_module

    app_module._FOLDER_COVER_CACHE.clear()
    try:
        assert app_module.folder_cover_rel_path("albums/a") == "albums/a/001.jpg"
        assert app_module.folder_cover_rel_path("albums/b") == "albums/b/001.jpg"
        assert app_module.folder_cover_rel_path("albums/c") == "albums/c/001.jpg"

        # Cache holds at most 2 entries; "albums/a" is the oldest and must be evicted.
        assert len(app_module._FOLDER_COVER_CACHE) == 2
        assert "albums/a" not in app_module._FOLDER_COVER_CACHE
        assert "albums/b" in app_module._FOLDER_COVER_CACHE
        assert "albums/c" in app_module._FOLDER_COVER_CACHE

        # Accessing "a" again re-inserts it and evicts the oldest ("albums/b").
        assert app_module.folder_cover_rel_path("albums/a") == "albums/a/001.jpg"
        assert len(app_module._FOLDER_COVER_CACHE) == 2
        assert "albums/a" in app_module._FOLDER_COVER_CACHE
        assert "albums/b" not in app_module._FOLDER_COVER_CACHE
        assert "albums/c" in app_module._FOLDER_COVER_CACHE
    finally:
        app_module._FOLDER_COVER_CACHE.clear()


def test_folder_cover_cache_short_circuits_none_entry_within_ttl(monkeypatch, tmp_path):
    """A cached (ts, None) entry must short-circuit a re-scan while the TTL is still valid (issue #348)."""
    extra_env = {
        "GALLERY_AUTO_FOLDER_COVERS": "true",
        "GALLERY_COVER_CACHE_TTL": "3600",
    }
    client, data_dir = build_client(
        monkeypatch, tmp_path, auth_type="none", extra_env=extra_env
    )

    import app as app_module

    # No folder is created; first call records (now, None) because the
    # folder does not exist.
    assert app_module.folder_cover_rel_path("albums/missing") is None
    cached = app_module._FOLDER_COVER_CACHE.get("albums/missing")
    assert cached is not None and cached[1] is None

    # Replace iter_gallery_items so a re-scan would explode; if the function
    # short-circuits on the cached None, this callable is never invoked.
    def _explode(*_args, **_kwargs):
        raise AssertionError("iter_gallery_items must not be re-called within TTL")

    monkeypatch.setattr("app.iter_gallery_items", _explode)

    # Second call within TTL must return None without invoking iter_gallery_items.
    assert app_module.folder_cover_rel_path("albums/missing") is None


def test_folder_cover_cache_revalidates_after_ttl_expiry(monkeypatch, tmp_path):
    """After TTL expiry the cache must re-validate the backing file before re-caching (issue #348)."""
    extra_env = {
        "GALLERY_AUTO_FOLDER_COVERS": "true",
        "GALLERY_COVER_CACHE_TTL": "1",
    }
    client, data_dir = build_client(
        monkeypatch, tmp_path, auth_type="none", extra_env=extra_env
    )

    import app as app_module

    folder = data_dir / "albums" / "trip"
    _make_image(folder / "001.jpg", color="red")
    _make_image(folder / "002.jpg", color="green")

    # Prime the cache and capture the original timestamp.
    assert app_module.folder_cover_rel_path("albums/trip") == "albums/trip/001.jpg"
    original = app_module._FOLDER_COVER_CACHE["albums/trip"]
    original_ts, original_rel = original
    assert original_rel == "albums/trip/001.jpg"

    # Simulate iter_gallery_items returning a bogus (nonexistent) candidate to
    # confirm the post-scan re-validation rejects it.
    ghost = data_dir / "albums" / "trip" / "ghost.jpg"
    scan_calls = {"n": 0}

    def _scanning(*_args, **_kwargs):
        scan_calls["n"] += 1
        return [ghost]

    monkeypatch.setattr("app.iter_gallery_items", _scanning)

    # Wait for the TTL to expire so we take the post-TTL branch.
    time.sleep(1.2)

    # The cached file still exists, so we must NOT re-scan; the entry is
    # refreshed in place and returned.
    assert app_module.folder_cover_rel_path("albums/trip") == "albums/trip/001.jpg"
    assert scan_calls["n"] == 0, (
        "TTL-expiry re-validation should refresh and skip the scan"
    )
    refreshed = app_module._FOLDER_COVER_CACHE["albums/trip"]
    assert refreshed[1] == "albums/trip/001.jpg"
    assert refreshed[0] > original_ts

    # Now delete the backing file and confirm a TTL-expiry pass takes the
    # scan branch, but the post-scan re-validation rejects the nonexistent
    # ghost path and does not re-populate the cache for the full TTL.
    (folder / "001.jpg").unlink()
    time.sleep(1.2)
    assert app_module.folder_cover_rel_path("albums/trip") is None
    cached = app_module._FOLDER_COVER_CACHE.get("albums/trip")
    assert cached is None or cached[1] != "albums/trip/ghost.jpg"


def test_folder_cover_cache_does_not_repopulate_nonexistent_path_after_scan(
    monkeypatch, tmp_path
):
    """A nonexistent candidate from iter_gallery_items must never be re-cached (issue #348)."""
    extra_env = {
        "GALLERY_AUTO_FOLDER_COVERS": "true",
        "GALLERY_COVER_CACHE_TTL": "3600",
    }
    client, data_dir = build_client(
        monkeypatch, tmp_path, auth_type="none", extra_env=extra_env
    )

    import app as app_module

    folder = data_dir / "albums" / "trip"
    folder.mkdir(parents=True, exist_ok=True)

    ghost = data_dir / "albums" / "trip" / "ghost.jpg"  # never created

    monkeypatch.setattr("app.iter_gallery_items", lambda *a, **kw: [ghost])

    # First call scans and gets the ghost path; post-scan validation rejects it.
    assert app_module.folder_cover_rel_path("albums/trip") is None
    cached = app_module._FOLDER_COVER_CACHE.get("albums/trip")
    assert cached is None, "Nonexistent path must not be cached: %r" % (cached,)
