# Contributing

## Development setup

A clean checkout is ready for development with one noninteractive command:

```sh
mise run bootstrap
```

[mise](https://mise.jdx.dev) installs the Python pinned in `.mise/config.toml` (locked, with
checksums, in `.mise/mise.lock`), creates the project-local `.venv/`, and installs the project
with dev extras from `pyproject.toml` — the dependency authority for development installs. The
committed pip constraint lock in `.mise/requirements-constraints.txt` pins the reviewed resolution
(including the build backend) without adding a second dependency authority. No sudo, no global config changes, no Git hooks.
Re-running is idempotent: an existing `.venv/` built from the pinned Python is reused. The script
fails clearly if prerequisites are missing and never modifies tracked source or runs the test
suite implicitly.

Run tasks through mise so the pinned toolchain is used (activate `.venv/` manually if you
prefer a plain shell):

| Task | What it does |
| --- | --- |
| `mise run lint` | Ruff with the same flags as the CI lint workflow |
| `mise run check` | `lint` plus the version/dependency consistency scripts CI gates on |
| `mise run test` | Unit tests with the CI coverage gate (`--fail-under=80`) |

Targeted verification examples:

```sh
mise run check
mise run test
.venv/bin/pytest tests/test_upload.py -q
```

Native/media utilities: none are required beyond pip-installable wheels (Pillow) — upload
tests fabricate image and MP4 bytes in pure Python, so no ffmpeg/ffprobe or system media
packages are needed. `pip check` at the end of bootstrap verifies the resolved environment.

## Agent claim/lock workflow (prevents overlapping PRs)

Before starting implementation on an issue:

1. Add labels: `claimed`, `in-progress`
2. Assign yourself
3. Comment with:

```text
/claim
owner: <agent-name>
issue: #<number>
branch: chipper/<issue>-<slug>
started: <ISO-8601>
```

If an issue is already claimed or already has an open PR, do not start new work on it.

When blocked, switch label to `blocked` and leave a short status comment.
When complete, remove `claimed`/`in-progress` labels and close the claim comment with PR link.
