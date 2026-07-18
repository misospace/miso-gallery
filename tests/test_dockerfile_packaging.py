from pathlib import Path


def test_dockerfile_packages_tag_store():
    dockerfile = Path("Dockerfile").read_text()
    copy_commands = [
        line.split()
        for line in dockerfile.splitlines()
        if line.strip().startswith("COPY ")
    ]

    assert any("tag_store.py" in command[1:-1] for command in copy_commands)


def test_dockerfile_uses_entrypoint():
    """Dockerfile should use entrypoint.sh instead of hardcoded CMD."""
    dockerfile = Path("Dockerfile").read_text()
    assert "entrypoint.sh" in dockerfile
    assert "ENTRYPOINT" in dockerfile
    # Should NOT have hardcoded workers in CMD
    lines = [line.strip() for line in dockerfile.splitlines() if line.strip().startswith("CMD")]
    assert not any("--workers" in line for line in lines), (
        "Worker count should not be hardcoded in CMD"
    )


def test_dockerfile_documents_rate_limiter_limitation():
    """Dockerfile should document the in-memory rate limiter limitation."""
    dockerfile = Path("Dockerfile").read_text()
    assert "rate limiter" in dockerfile.lower() or "rate limiting" in dockerfile.lower()


def test_entrypoint_script_exists():
    """entrypoint.sh must exist and be executable."""
    entrypoint = Path("entrypoint.sh")
    assert entrypoint.exists(), "entrypoint.sh is missing"
    assert entrypoint.stat().st_mode & 0o111, "entrypoint.sh is not executable"


def test_entrypoint_uses_web_concurrency():
    """entrypoint.sh should read WEB_CONCURRENCY env var."""
    entrypoint = Path("entrypoint.sh").read_text()
    assert "WEB_CONCURRENCY" in entrypoint
    assert "gunicorn" in entrypoint
    # Should have a default value
    assert ":=" in entrypoint or "${WEB_CONCURRENCY:-" in entrypoint


def test_entrypoint_documents_rate_limiter_warning():
    """entrypoint.sh should warn about in-memory rate limiter with multiple workers."""
    entrypoint = Path("entrypoint.sh").read_text()
    assert "rate limiter" in entrypoint.lower() or "rate limiting" in entrypoint.lower()
