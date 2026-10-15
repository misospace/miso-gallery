import os
import re
import sys

from PIL import Image

import thumbnails
from conftest import build_client
from thumbnails import _thumbnail_path_identity


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
    monkeypatch.setattr(thumbnails, "ensure_thumbnail_cache_dir", lambda: None)

    # Stale symlink simulating a bind-mount / NFS target. The cleanup contract
    # matches by the path-identity digest prefix (issue #515), so we mirror
    # that shape here.
    external = tmp_path / "nfs_target.jpg"
    external.write_bytes(b"external content")
    identity = _thumbnail_path_identity("cats/cat")
    link = cache / f"{identity}.111.jpg"
    link.symlink_to(external)

    # A legitimate cache file with the same prefix must still be removed
    legit = cache / f"{identity}.222.jpg"
    legit.write_bytes(b"legit")

    app_module.batch_remove_thumbnails(["cats/cat"])

    assert not legit.exists(), "legitimate cache file must be removed"
    assert link.is_symlink(), "symlink inside .thumb_cache/ must not be unlinked"
    assert external.exists(), "symlink target must not be deleted"


def test_thumb_route_distinguishes_collision_prone_paths(monkeypatch, tmp_path):
    """Issue #515: distinct gallery-relative paths whose names share a separator
    representation must each receive their own thumbnail, in either request
    order. Pre-fix this test reproduced a real gallery bug: ``a/b.png`` and
    ``a__b.png`` produced the same cached JPEG when their mtime and size
    matched, so the wrong image would be served depending on order of access.
    """
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")
    app_module = sys.modules["app"]

    # Build the exact scenario from issue #515: two 2x2 PNGs of equal size and
    # equal mtime_ns — red at ``a/b.png`` and blue at ``a__b.png``.
    nested_dir = data_dir / "a"
    nested_dir.mkdir()
    red_img = Image.new("RGB", (2, 2), color=(254, 0, 0))
    red_img.save(nested_dir / "b.png")
    blue_img = Image.new("RGB", (2, 2), color=(0, 0, 254))
    blue_img.save(data_dir / "a__b.png")

    # Force equal size (PNG encoding already produces equal bytes for 2x2)
    # and equal mtime_ns.
    target_mtime = 123456789
    os.utime(nested_dir / "b.png", (target_mtime, target_mtime))
    os.utime(data_dir / "a__b.png", (target_mtime, target_mtime))
    assert (nested_dir / "b.png").stat().st_size == (data_dir / "a__b.png").stat().st_size
    assert (nested_dir / "b.png").stat().st_mtime_ns == (data_dir / "a__b.png").stat().st_mtime_ns

    # Forward order.
    first = client.get("/thumb/a/b.png")
    assert first.status_code == 200, first.get_data(as_text=True)
    second = client.get("/thumb/a__b.png")
    assert second.status_code == 200, second.get_data(as_text=True)

    def _pixel_of(resp):
        with Image.open(__import__("io").BytesIO(resp.get_data())).convert("RGB") as im:
            return im.getpixel((0, 0))

    assert _pixel_of(first) == (254, 0, 0), (
        "/thumb/a/b.png must show red (its own image), got %r" % (_pixel_of(first),)
    )
    assert _pixel_of(second) == (0, 0, 254), (
        "/thumb/a__b.png must show blue (its own image), got %r" % (_pixel_of(second),)
    )

    # Reverse order — responses must be deterministic.
    again_first = client.get("/thumb/a/b.png")
    again_second = client.get("/thumb/a__b.png")
    assert _pixel_of(again_first) == (254, 0, 0)
    assert _pixel_of(again_second) == (0, 0, 254)

    # Both cache files now exist with distinct digest identities (the on-disk
    # shape proves the cache identity is not path-swapped).
    cache_dir = app_module.THUMBNAIL_CACHE_DIR
    cache_names = sorted(p.name for p in cache_dir.iterdir() if p.is_file())
    assert any(name.startswith(_thumbnail_path_identity("a/b.png") + ".") for name in cache_names), (
        f"missing digest-named cache entry for a/b.png; saw {cache_names!r}"
    )
    assert any(name.startswith(_thumbnail_path_identity("a__b.png") + ".") for name in cache_names), (
        f"missing digest-named cache entry for a__b.png; saw {cache_names!r}"
    )


def test_integrity_check_does_not_delete_legacy_cache_files(monkeypatch, tmp_path):
    """Issue #515: ``run_thumbnail_integrity_check`` must not delete or rename
    legacy cache files (``<path-swap>.<mtime>.<size>.jpg``) written by earlier
    builds. They are simply orphaned by the new identity contract; the
    integrity check regenerates only files under the new digest-based name.
    """
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")
    app_module = sys.modules["app"]
    cache = data_dir / ".thumb_cache"

    # Plant a legacy-shaped cache file alongside the bootstrap sample images.
    sample = data_dir / "sample.png"
    legacy_path = cache / f"sample.png.{os.stat(sample).st_mtime_ns}.{os.stat(sample).st_size}.jpg"
    legacy_path.write_bytes(b"legacy bytes")

    app_module.run_thumbnail_integrity_check()

    # The legacy entry must still be present and untouched on disk.
    assert legacy_path.exists(), "legacy cache file must not be removed by integrity check"
    assert legacy_path.read_bytes() == b"legacy bytes", (
        "legacy cache file must not be rewritten by integrity check"
    )

    # A fresh, modern entry for ``sample.png`` was created alongside it.
    modern_identity = _thumbnail_path_identity("sample.png")
    modern_entries = [
        p for p in cache.iterdir()
        if p.is_file() and p.name.startswith(f"{modern_identity}.")
    ]
    assert modern_entries, (
        "integrity check must produce a digest-named cache entry for sample.png"
    )


def test_batch_remove_thumbnails_leaves_legacy_cache_files_in_place(monkeypatch, tmp_path):
    """Issue #515: ``batch_remove_thumbnails`` matching by the new digest
    identity must NEVER delete a legacy-shaped cache entry. Stale legacy
    cache files are only orphaned — subsequent regeneration overwrites the
    modern entry, but the legacy file is never deleted automatically.
    """
    import app as app_module

    cache = tmp_path / "thumbs"
    cache.mkdir()
    monkeypatch.setattr(app_module, "THUMBNAIL_CACHE_DIR", cache)
    monkeypatch.setattr(thumbnails, "ensure_thumbnail_cache_dir", lambda: None)

    # Legacy shape — none of the prefix bits match a hex-digest identity.
    legacy = cache / "sample.png.111.222.jpg"
    legacy.write_bytes(b"legacy")
    # Modern shape — must be deleted by the call below.
    modern_identity = _thumbnail_path_identity("sample.png")
    modern = cache / f"{modern_identity}.111.222.jpg"
    modern.write_bytes(b"modern")

    app_module.batch_remove_thumbnails(["sample.png"])

    assert not modern.exists(), "modern cache file must be deleted"
    assert legacy.exists(), "legacy cache file must NOT be deleted"
