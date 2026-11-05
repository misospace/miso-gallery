"""Tests for HTTP Range-request support on video files served via /thumb/.

Fixes #363: Video files served via /thumb/ lack Range-request support, breaking seek/scrub.
"""
from conftest import build_client


def _build_client_with_video(monkeypatch, tmp_path):
    """Build client with a small test video file."""
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")

    # Create a small "video" file (just bytes, not a real mp4)
    video_dir = data_dir / "videos"
    video_dir.mkdir(parents=True, exist_ok=True)
    video_path = video_dir / "test.mp4"
    # Write 10240 bytes of predictable data for range testing
    video_path.write_bytes(bytes(range(256)) * 40)

    return client, video_path


def test_video_without_range_returns_200(monkeypatch, tmp_path):
    """Video request without Range header should return full file with 200."""
    client, _ = _build_client_with_video(monkeypatch, tmp_path)

    resp = client.get("/thumb/videos/test.mp4")
    assert resp.status_code == 200
    assert len(resp.data) == 10240
    # send_file advertises Accept-Ranges so clients know seeking is available
    assert resp.headers.get("Accept-Ranges") == "bytes"


def test_video_with_range_returns_206(monkeypatch, tmp_path):
    """Video request with Range header should return 206 Partial Content."""
    client, _ = _build_client_with_video(monkeypatch, tmp_path)

    resp = client.get("/thumb/videos/test.mp4", headers={"Range": "bytes=0-1023"})
    assert resp.status_code == 206
    assert len(resp.data) == 1024
    assert "Content-Range" in resp.headers
    assert resp.headers["Content-Range"] == "bytes 0-1023/10240"
    assert resp.headers["Content-Length"] == "1024"
    assert resp.headers["Accept-Ranges"] == "bytes"


def test_video_range_open_ended(monkeypatch, tmp_path):
    """Range header with open-ended end (bytes=500-) should return to EOF."""
    client, _ = _build_client_with_video(monkeypatch, tmp_path)

    resp = client.get("/thumb/videos/test.mp4", headers={"Range": "bytes=500-"})
    assert resp.status_code == 206
    expected_length = 10240 - 500
    assert len(resp.data) == expected_length
    assert resp.headers["Content-Range"] == f"bytes 500-{expected_length + 499}/10240"


def test_video_range_seeking_positions(monkeypatch, tmp_path):
    """Test seeking to multiple positions (simulates scrubbing)."""
    client, video_path = _build_client_with_video(monkeypatch, tmp_path)
    full_data = video_path.read_bytes()

    # Seek to three different positions
    ranges = [
        ("bytes=0-255", 0, 255),
        ("bytes=5000-5100", 5000, 5100),
        ("bytes=9000-", 9000, 10239),
    ]

    for range_header, expected_start, expected_end in ranges:
        resp = client.get("/thumb/videos/test.mp4", headers={"Range": range_header})
        assert resp.status_code == 206, f"Failed for Range: {range_header}"
        chunk_data = resp.data
        # Verify the returned bytes match the expected slice of the file
        assert chunk_data == full_data[expected_start : expected_end + 1], (
            f"Data mismatch for Range: {range_header}"
        )


def test_video_range_invalid_returns_200(monkeypatch, tmp_path):
    """Invalid Range header should fall back to serving full file."""
    client, _ = _build_client_with_video(monkeypatch, tmp_path)

    resp = client.get("/thumb/videos/test.mp4", headers={"Range": "invalid"})
    assert resp.status_code == 200
    assert len(resp.data) == 10240


def test_video_range_clamped_to_file_size(monkeypatch, tmp_path):
    """Range extending past file size should be clamped at the last byte."""
    client, _ = _build_client_with_video(monkeypatch, tmp_path)

    resp = client.get("/thumb/videos/test.mp4", headers={"Range": "bytes=10200-20000"})
    assert resp.status_code == 206
    # Should be clamped to bytes 10200-10239 (40 bytes)
    assert len(resp.data) == 40
    assert "bytes 10200-10239/10240" in resp.headers["Content-Range"]


def test_video_range_start_at_file_size_returns_416(monkeypatch, tmp_path):
    """bytes=N- with N == file size is unsatisfiable: 416 with bytes */<size> (RFC 9110 §14.2)."""
    client, _ = _build_client_with_video(monkeypatch, tmp_path)

    resp = client.get("/thumb/videos/test.mp4", headers={"Range": "bytes=10240-"})
    assert resp.status_code == 416
    assert resp.headers["Content-Range"] == "bytes */10240"
    # The 416 must not promise bytes it does not send
    assert len(resp.data) == int(resp.headers["Content-Length"])


def test_video_range_start_beyond_file_size_returns_416(monkeypatch, tmp_path):
    """bytes=N- with N > file size is unsatisfiable: 416 with bytes */<size> (RFC 9110 §14.2)."""
    client, _ = _build_client_with_video(monkeypatch, tmp_path)

    resp = client.get("/thumb/videos/test.mp4", headers={"Range": "bytes=20000-"})
    assert resp.status_code == 416
    assert resp.headers["Content-Range"] == "bytes */10240"
    assert len(resp.data) == int(resp.headers["Content-Length"])


def test_video_range_zero_byte_file_with_range_is_consistent(monkeypatch, tmp_path):
    """Zero-byte video with a Range header must be a consistent response, never
    a Content-Length exceeding the body bytes (RFC 9110 §14.2: every byte range
    on an empty representation is unsatisfiable).

    Previously this returned 206 with "Content-Range: bytes 0-0/0",
    Content-Length 1, and an empty body.
    """
    client, data_dir = build_client(monkeypatch, tmp_path, auth_type="none")
    (data_dir / "empty.mp4").write_bytes(b"")

    resp = client.get("/thumb/empty.mp4", headers={"Range": "bytes=0-"})
    assert resp.status_code == 416
    assert resp.headers["Content-Range"] == "bytes */0"
    assert len(resp.data) == int(resp.headers["Content-Length"])
