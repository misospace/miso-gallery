from pathlib import Path


def test_dockerfile_packages_tag_store():
    dockerfile = Path("Dockerfile").read_text()
    copy_commands = [
        line.split()
        for line in dockerfile.splitlines()
        if line.strip().startswith("COPY ")
    ]

    assert any("tag_store.py" in command[1:-1] for command in copy_commands)
