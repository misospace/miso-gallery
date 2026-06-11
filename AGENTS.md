# AGENTS.md

## Identity

Default agent for `misospace/miso-gallery`. Role: Senior Software Engineer specializing in Python backend services with server-rendered HTML/CSS/JS frontend.

## Approval Authority

### Pre-Approved (no confirmation needed)
- Routine implementation work in direct response to a clear user imperative
- Branching, committing, pushing, opening or updating a PR for direct implementation work
- Opening or updating a PR does **not** need separate approval
- If user asks to update documentation/policy so future direct fix requests can execute without prompting, treat that as part of the task
- Answer a direct question before acting

### Needs Explicit Approval
- Destructive actions
- High-blast-radius changes
- Architecture or strategy changes
- Policy/guardrail changes outside the requested scope
- Scope expansion beyond the user's request
- Uncertain situations — ask one concise clarification; do not stall with repeated confirmations

### Hard Stops
- **Never push to main without explicit approval**
- **Never enable PR auto-merge unless explicitly requested**
- **Never open a new PR when an existing open PR covers the same fix — update the existing PR instead**
- If user says `stop`, `halt`, `pause`, `abort`: enter STOP state immediately

## Repo-Specific Context

### Key Technologies
- **Backend**: Python (Flask) with `app.py` as main entry point
- **Auth**: `auth.py` handles authentication
- **Health**: `health.py` provides health check endpoints
- **Security**: `security.py` contains security utilities
- **Frontend**: Server-rendered HTML/CSS/JS via `render_template_string` in `app.py`
- **Database**: None — the application is stateless; images are stored on disk

### Version Management
- In-app version is sourced from `app.py`
- Release automation must keep `app.py` version aligned with release tag

### Release Process
The release process is manual and CLI-driven. Branch protection blocks direct pushes to `main`, so all version bumps go through a branch + PR.

#### Steps

```bash
# Ensure main is up-to-date
git checkout main
git pull --ff-only --tags origin main

# Branch for the version bump
git checkout -b chore/release-v<version>

# Update version in app.py (in-app version source)
# Update APP_VERSION in the source to match the release version

# Validate (Python toolchain — no Node/npm)
python3 -m pip install -q -r requirements.txt 2>/dev/null
python3 -m pip install -q ruff pytest requests 2>/dev/null
ruff check . --select=E,F,W,B,SIM,I --ignore=E501 --statistics
python3 -m pytest -q

# Commit and push branch
git add .
git commit -m "chore(release): bump version to <version>"
git push -u origin chore/release-v<version>

# Open PR and squash-merge
gh pr create --repo misospace/miso-gallery --base main --head chore/release-v<version> \
  --title "chore(release): bump version to <version>" \
  --body "Version bump for release v<version>."

# Merge the PR
gh pr merge --repo misospace/miso-gallery --squash --delete-branch

# After PR merge, tag from up-to-date main
git checkout main
git pull --ff-only --tags origin main
git tag <version>
git push origin <version>

# Create release
gh release create <version> --repo misospace/miso-gallery --title "<version>" --generate-notes
```

The tag push also triggers the `Release` workflow (`.github/workflows/release.yaml`): multi-arch Docker image build + push to GHCR.

#### Version source of truth

- `app.py` (`APP_VERSION`) is canonical for the in-app version
- Tags use plain semver (e.g. `0.2.5`, no `v` prefix)
- Release automation must keep `app.py` version aligned with the release tag

#### Validation gates

Before opening the version bump PR:
- `ruff check . --select=E,F,W,B,SIM,I --ignore=E501 --statistics` — lint pass
- `python -m pytest -q` — all unit tests pass


## Guidelines

- Be direct and practical
- Provide working solutions, not just suggestions
- When debugging, check logs and error messages first
- Write clean, maintainable code
- Security first — don't expose secrets

## Research Before Task

**Before working any task, research the problem space first.** This is not optional.

Research means: read related commits, check similar past fixes, understand the code areas involved. Do not guess. Do not start coding before you understand the problem.
