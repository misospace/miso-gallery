import re
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


def test_entrypoint_gunicorn_timeout_at_least_max_task_timeout():
    """Gunicorn --timeout must be >= max WEBHOOK_TASK_TIMEOUT (120s).

    Regression test for issue #385: webhook tasks can run up to 120s, but
    gunicorn's default 30s worker timeout would kill the worker mid-task.
    """
    entrypoint = Path("entrypoint.sh").read_text()
    match = re.search(r"--timeout\s+(\d+)", entrypoint)
    assert match, "entrypoint.sh should set --timeout for gunicorn"
    timeout = int(match.group(1))
    # Max webhook task timeout is 120s (hard-coded cap in app.py)
    max_task_timeout = 120
    assert timeout >= max_task_timeout, (
        f"gunicorn --timeout ({timeout}s) must be >= max WEBHOOK_TASK_TIMEOUT ({max_task_timeout}s)"
    )


def test_dockerfile_creates_appuser_group():
    """Dockerfile should create a dedicated appuser group."""
    dockerfile = Path("Dockerfile").read_text()
    assert "groupadd" in dockerfile
    assert "appuser" in dockerfile


def test_dockerfile_creates_appuser_user():
    """Dockerfile should create a dedicated appuser user with numeric UID."""
    dockerfile = Path("Dockerfile").read_text()
    assert "useradd" in dockerfile
    # Must use a numeric UID (DL3066 compliance)
    assert "-u 10001" in dockerfile


def test_dockerfile_uses_appuser():
    """Dockerfile should switch to appuser before ENTRYPOINT."""
    dockerfile = Path("Dockerfile").read_text()
    lines = dockerfile.splitlines()
    user_line = None
    entrypoint_line = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("USER"):
            user_line = i
        if stripped.startswith("ENTRYPOINT"):
            entrypoint_line = i
    assert user_line is not None, "No USER directive found"
    assert entrypoint_line is not None, "No ENTRYPOINT directive found"
    assert user_line < entrypoint_line, "USER must come before ENTRYPOINT"
    # Must use numeric UID (DL3066 compliance)
    assert "USER 10001" in dockerfile


def test_dockerfile_chowns_data():
    """Dockerfile should chown /data to appuser."""
    dockerfile = Path("Dockerfile").read_text()
    assert "chown" in dockerfile
    assert "/data" in dockerfile
