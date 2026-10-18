---
name: media-security-review
description: Review miso-gallery changes involving user-uploaded media, filesystem paths, authentication, OAuth/OIDC callbacks, rate limiting, subprocesses, and image parsing.
---

# AI PR Review: miso-gallery

## Security review conventions

miso-gallery serves user-uploaded media. Security-sensitive areas:

- **File serving routes** (`app.py`: `send_from_directory`): sanitize requested paths, block hidden/excluded directories, reject non-media files on public-media-only routes
- **Path traversal**: verify symlink and resolved-path behavior; resolved file targets must not escape the configured data root
- **Uploads** (`app.py`): validate content type, file size, and filename before storage; reject executable content
- **Auth** (`auth.py`): auth bypass routes must be intentionally public and narrowly scoped; OAuth/OIDC callback validation
- **Rate limiting** (`security.py`): endpoint-specific rate limits, Redis-backed state
- **Subprocess execution** (`app.py`: `subprocess` usage): validate any user-influenced arguments, avoid shell injection
- **PIL/Image processing**: validate image dimensions and format before processing; handle `UnidentifiedImageError`

For PRs that touch these areas, call out:
- Is input sanitized before use in filesystem, subprocess, or network operations?
- Are auth bypass routes documented as intentionally public?
- Does the change address all threat cases from the linked issue, or are edge cases documented as out of scope?
- Missing test coverage for file-path or auth edge cases.

## Review tone

- Be direct and practical.
- Flag only real defects, regressions, or meaningful risks as blocking.
- Do not nitpick formatting, naming, or style unless it affects readability or correctness.
- Prefer `approve` or non-blocking comments for PRs that look reasonable overall.

## Focused review procedure

- Trace user-controlled filenames and paths from request validation through storage and file serving. Verify resolved targets remain under the configured data root, including symlink behavior.
- For upload and image-processing changes, check size, content type, actual file format, dimensions, and malformed input handling. Do not trust filename extensions or client-supplied MIME types alone.
- For auth changes, verify route exposure is intentional, OAuth/OIDC callback state and identity validation remain intact, and public exceptions are narrowly scoped.
- For subprocess changes, trace all user-influenced arguments and confirm shell interpretation is avoided or strictly controlled.
- Check regression tests for traversal, hidden/excluded directories, non-media files, malformed images, auth bypasses, and rate-limit behavior where relevant.
- Keep findings evidence-based: cite the changed path and concrete exploit or regression path. Do not turn speculative hardening preferences into blockers.
