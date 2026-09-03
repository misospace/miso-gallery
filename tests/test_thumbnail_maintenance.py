import re
import sys

from PIL import Image

from conftest import build_client


def _build_client(monkeypatch, tmp_path):
    """Build client using shared bootstrap, then add per-test data."""
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")

    img = Image.new("RGB", (64, 64), color="blue")
    (data_dir / "cats").mkdir(parents=True, exist_ok=True)
    img.save(data_dir / "cats" / "cat.png")

    return client


def test_thumbnail_integrity_maintenance_regenerates_and_reports_counts(monkeypatch, tmp_path):
    client = _build_client(monkeypatch, tmp_path)

    settings = client.get("/settings")
    assert settings.status_code == 200
    html = settings.get_data(as_text=True)

    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "CSRF token not found in settings page"
    csrf = match.group(1)

    resp = client.post(
        "/maintenance/thumbnails/regenerate",
        data={"csrf_token": csrf},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    output = resp.get_data(as_text=True)

    # Maintenance runs successfully and reports counts
    assert "Checked:" in output
    assert "Regenerated:" in output
    assert "Failed: 0" in output


def test_integrity_check_skips_symlinked_images(monkeypatch, tmp_path):
    """#445: a symlink in DATA_FOLDER must not be thumbnailed into .thumb_cache/."""
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")
    app_module = sys.modules["app"]

    img = Image.new("RGB", (64, 64), color="blue")
    (data_dir / "cats").mkdir(parents=True, exist_ok=True)
    img.save(data_dir / "cats" / "cat.png")

    # Real (valid) image outside DATA_FOLDER
    external = tmp_path / "external.png"
    img.save(external)

    # Symlink inside DATA_FOLDER pointing at the external image
    link = data_dir / "foo.png"
    link.symlink_to(external)

    stats = app_module.run_thumbnail_integrity_check()

    # The symlink must not be counted as checked (it is skipped entirely).
    # Bootstrap creates sample.png, copy.png, cats/cat.jpg; we add cats/cat.png.
    assert stats["checked"] == 4
    # No thumbnail derived from the symlink target may exist in the cache
    cache = data_dir / ".thumb_cache"
    assert not any("foo" in f.name for f in cache.iterdir()), (
        "symlink target must not be thumbnailed into .thumb_cache/"
    )
    # The symlink itself and its target are untouched
    assert link.is_symlink()
    assert external.exists()


def test_batch_remove_thumbnails_skips_symlinks(monkeypatch, tmp_path):
    """#445: a symlink inside .thumb_cache/ must not be unlinked by batch_remove_thumbnails()."""
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")
    app_module = sys.modules["app"]

    cache = data_dir / ".thumb_cache"
    monkeypatch.setattr(app_module, "ensure_thumbnail_cache_dir", lambda: None)

    # Stale symlink simulating a bind-mount / NFS target
    external = tmp_path / "nfs_target.jpg"
    external.write_bytes(b"external content")
    link = cache / "cats__cat.111.jpg"
    link.symlink_to(external)

    # A legitimate cache file with the same prefix must still be removed
    legit = cache / "cats__cat.222.jpg"
    legit.write_bytes(b"legit")

    app_module.batch_remove_thumbnails(["cats/cat"])

    assert not legit.exists(), "legitimate cache file must be removed"
    assert link.is_symlink(), "symlink inside .thumb_cache/ must not be unlinked"
    assert external.exists(), "symlink target must not be deleted"
