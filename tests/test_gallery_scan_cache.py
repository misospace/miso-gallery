"""Tests for the shared filesystem scan cache (iter_gallery_items).

Validates that repeated scans within the TTL window reuse a cached snapshot,
that cache invalidation fires after write operations, and that small-limit
queries (folder covers) bypass the cache to maintain immediate consistency.
"""

from unittest.mock import patch

import pytest

import app


@pytest.fixture(autouse=True)
def _clear_scan_cache():
    """Clear the scan cache before and after each test."""
    app._gallery_scan_cache.clear()
    yield
    app._gallery_scan_cache.clear()


@pytest.fixture
def gallery_root(tmp_path):
    """Create a temporary gallery root and patch DATA_FOLDER to use it."""
    gallery = tmp_path / "gallery"
    gallery.mkdir()
    with patch.object(app, "DATA_FOLDER", gallery):
        yield gallery


class TestScanCacheBasic:
    """Verify the scan cache stores and returns cached results within TTL."""

    def test_repeated_calls_return_cached_results(self, gallery_root):
        """Multiple calls to iter_gallery_items reuse the cached snapshot."""
        (gallery_root / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0")
        (gallery_root / "b.png").write_bytes(b"\x89PNG\r\n\x1a\n")

        # First call populates the cache
        results1 = app.iter_gallery_items(kind="media", root=gallery_root)
        assert len(results1) == 2

        # Second call should return cached data (within TTL)
        results2 = app.iter_gallery_items(kind="media", root=gallery_root)
        assert results1 == results2

    def test_cache_key_includes_kind(self, gallery_root):
        """Cache entries are keyed by kind so media and folder scans don't collide."""
        subdir = gallery_root / "subdir"
        subdir.mkdir()
        (subdir / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0")

        media_results = app.iter_gallery_items(kind="media", root=subdir)
        folder_results = app.iter_gallery_items(kind="folders", root=subdir)

        assert len(media_results) == 1
        assert len(folder_results) == 0  # no subdirs inside subdir

    def test_cache_key_includes_root(self, gallery_root):
        """Cache entries are keyed by root so subfolder scans don't collide."""
        subdir = gallery_root / "subdir"
        subdir.mkdir()
        (gallery_root / "root_a.jpg").write_bytes(b"\xff\xd8\xff\xe0")
        (subdir / "sub_b.jpg").write_bytes(b"\xff\xd8\xff\xe0")

        root_results = app.iter_gallery_items(kind="media", root=gallery_root)
        subdir_results = app.iter_gallery_items(kind="media", root=subdir)

        assert len(root_results) == 2
        assert len(subdir_results) == 1


class TestScanCacheTTL:
    """Verify the cache expires after TTL."""

    def test_cache_expires_after_ttl(self, gallery_root):
        """Cached results are evicted after GALLERY_SCAN_CACHE_TTL seconds."""
        (gallery_root / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0")

        # Populate cache
        app.iter_gallery_items(kind="media", root=gallery_root)

        # Manually expire the cache entry
        for key in list(app._gallery_scan_cache):
            ts, results = app._gallery_scan_cache[key]
            app._gallery_scan_cache[key] = (ts - app.GALLERY_SCAN_CACHE_TTL - 1, results)

        # Add a new file after "expiration"
        (gallery_root / "b.jpg").write_bytes(b"\xff\xd8\xff\xe0")

        # Should re-scan and include the new file
        results = app.iter_gallery_items(kind="media", root=gallery_root)
        assert len(results) == 2


class TestScanCacheSmallLimit:
    """Verify small-limit queries (limit=1) bypass the cache."""

    def test_limit_one_bypasses_cache(self, gallery_root):
        """Queries with limit=1 skip the cache for immediate consistency."""
        (gallery_root / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0")

        # First call
        results1 = app.iter_gallery_items(kind="media", limit=1, root=gallery_root)
        assert len(results1) == 1

        # No cache entry should exist for limit=1
        cache_keys = [k for k in app._gallery_scan_cache if k[1] == 1]
        assert len(cache_keys) == 0, "limit=1 queries should not populate the cache"

    def test_limit_one_reflects_file_addition(self, gallery_root):
        """Adding a file directly on disk is reflected in limit=1 queries."""
        (gallery_root / "z.jpg").write_bytes(b"\xff\xd8\xff\xe0")
        app.iter_gallery_items(kind="media", limit=1, root=gallery_root)

        # Add a new file that sorts before the existing one
        (gallery_root / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0")
        results = app.iter_gallery_items(kind="media", limit=1, root=gallery_root)
        assert len(results) == 1
        assert results[0].name == "a.jpg"

    def test_limit_one_reflects_file_deletion(self, gallery_root):
        """Deleting a file directly on disk is reflected in limit=1 queries."""
        (gallery_root / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0")
        (gallery_root / "b.jpg").write_bytes(b"\xff\xd8\xff\xe0")

        results = app.iter_gallery_items(kind="media", limit=1, root=gallery_root)
        assert len(results) == 1
        assert results[0].name == "a.jpg"

        # Delete the first file
        (gallery_root / "a.jpg").unlink()

        # Should now return b.jpg
        results = app.iter_gallery_items(kind="media", limit=1, root=gallery_root)
        assert len(results) == 1
        assert results[0].name == "b.jpg"


class TestScanCacheInvalidation:
    """Verify cache invalidation after write operations."""

    def test_invalidate_clears_all_entries(self, gallery_root):
        """_invalidate_gallery_scan_cache clears all cached entries."""
        (gallery_root / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0")
        (gallery_root / "b.jpg").write_bytes(b"\xff\xd8\xff\xe0")

        # Populate cache with multiple entries
        app.iter_gallery_items(kind="media", root=gallery_root)
        app.iter_gallery_items(kind="folders", root=gallery_root)

        assert len(app._gallery_scan_cache) >= 1

        app._invalidate_gallery_scan_cache()
        assert len(app._gallery_scan_cache) == 0


class TestScanCacheDefaultLimit:
    """Verify default limit behavior with caching."""

    def test_default_limit_uses_cache(self, gallery_root):
        """Queries using the default limit (GALLERY_SCAN_LIMIT) use the cache."""
        (gallery_root / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0")

        results1 = app.iter_gallery_items(kind="media", root=gallery_root)
        assert len(results1) == 1

        # Cache should have an entry with default limit
        cache_keys = [k for k in app._gallery_scan_cache if k[1] == app.GALLERY_SCAN_LIMIT]
        assert len(cache_keys) == 1
