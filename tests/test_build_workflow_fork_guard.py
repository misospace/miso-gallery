"""Regression tests for .github/workflows/build.yaml fork-PR guard.

Issue #509: the Build workflow's ``docker/build-push-action`` step always
pushed ``push=true`` to GHCR on every pull_request event, so fork PRs (where
``GITHUB_TOKEN`` is read-only) failed the Build check with a 403 from the
registry. The workflow must build the image for every PR so the Dockerfile
is exercised, but only push when the PR is from the same repository.

These tests assert the structural invariants that keep that contract true:
fork PRs must not push, must not log in to GHCR, and must not upload the
digest artifact the merge job depends on.

The workflow is parsed with a tiny line-based scanner rather than PyYAML
so the test stays free of new dependencies — matching how
``test_dockerfile_packaging`` inspects the Dockerfile.
"""

import re
from pathlib import Path

REPO = Path(__file__).parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "build.yaml"

# The shared fork-guard expression used throughout the workflow. Use of
# ``head.repo.full_name`` matches the acceptance criterion in issue #509.
FORK_GUARD = (
    "github.event_name != 'pull_request' || "
    "github.event.pull_request.head.repo.full_name == github.repository"
)

# Indent (in spaces) of ``- name:`` step markers inside ``jobs.<job>.steps``.
STEP_INDENT = 6
# Indent (in spaces) of ``jobname:`` markers under ``jobs:``.
JOB_INDENT = 2
# Indent (in spaces) of scalar fields inside a job (e.g. ``if:``, ``runs-on:``).
JOB_FIELD_INDENT = JOB_INDENT + 2


def _split_blocks(lines, start_pattern):
    """Split ``lines`` into blocks.

    Each block starts at the first line that matches ``start_pattern`` (a
    precompiled regex) and runs until the next such line. Returns a list of
    raw line lists, one per block. Lines before the first match are dropped.
    """
    blocks = []
    current = None
    for line in lines:
        if start_pattern.match(line):
            if current is not None:
                blocks.append(current)
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        blocks.append(current)
    return blocks


def _load_blocks():
    assert WORKFLOW.exists(), f"{WORKFLOW} is missing"
    text = WORKFLOW.read_text()
    lines = text.splitlines()

    job_marker = re.compile(rf"^ {{{JOB_INDENT}}}[A-Za-z_][A-Za-z0-9_-]*:\s*$")
    step_marker = re.compile(rf"^ {{{STEP_INDENT}}}-\s")

    jobs = _split_blocks(lines, job_marker)
    by_name = {}
    for job in jobs:
        # Job name is the first line's leading text before the colon.
        head = job[0].lstrip()
        name = head.split(":", 1)[0].strip()
        by_name[name] = _split_blocks(job[1:], step_marker)
    return text, by_name


def _step_field(block_lines, key):
    """Return the value of a step-level scalar field.

    Handles both ``- name: <value>`` (the step marker) and ``<key>: <value>``
    (subsequent indented fields). Returns None if the field is absent.
    """
    name_marker = re.compile(rf"^ {{{STEP_INDENT}}}-\s+{re.escape(key)}:\s*(.+?)\s*$")
    field_marker = re.compile(rf"^\s+{re.escape(key)}:\s*(.+?)\s*$")
    for line in block_lines:
        for marker in (name_marker, field_marker):
            match = marker.match(line)
            if match:
                return match.group(1).strip()
    return None


def _has_step_field(block_lines, key):
    name_marker = re.compile(rf"^ {{{STEP_INDENT}}}-\s+{re.escape(key)}:")
    field_marker = re.compile(rf"^\s+{re.escape(key)}:")
    return any(marker.search(line) for line in block_lines for marker in (name_marker, field_marker))


def _step_outputs(block_lines):
    """Return the joined value of the ``with.outputs:`` field (folded scalar)."""
    text = "\n".join(block_lines)
    match = re.search(
        r"^\s+outputs:\s*(.+?)(?=\n\s+\S|$)",
        text,
        re.MULTILINE,
    )
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def _job_field(lines, key):
    """Return the value of a job-level scalar field at indent JOB_FIELD_INDENT."""
    pattern = re.compile(rf"^ {{{JOB_FIELD_INDENT}}}{re.escape(key)}:\s*(.+?)\s*$")
    for line in lines:
        match = pattern.match(line)
        if match:
            return match.group(1).strip()
    return None


def _has_job_field(lines, key):
    pattern = re.compile(rf"^ {{{JOB_FIELD_INDENT}}}{re.escape(key)}:")
    return any(pattern.search(line) for line in lines)


def _merge_block_text(text):
    """Return the lines that span the ``merge:`` job (from ``  merge:`` to
    the next top-level key or end of file)."""
    lines = text.splitlines()
    start = None
    end = len(lines)
    for i, line in enumerate(lines):
        if re.match(rf"^ {{{JOB_INDENT}}}merge:\s*$", line):
            start = i
        elif start is not None and re.match(rf"^ {{{JOB_INDENT}}}[A-Za-z_][A-Za-z0-9_-]*:\s*$", line):
            end = i
            break
    assert start is not None, "build.yaml must define a 'merge' job"
    return lines[start:end]


def test_workflow_triggers_on_pull_request_and_push():
    """Build must still run on PRs and main pushes; otherwise fork PRs would
    never get a green Build status to prove the image builds."""
    text, _ = _load_blocks()
    assert "  pull_request:" in text
    assert "- main" in text
    assert "  push:" in text


def _build_steps():
    _, by_name = _load_blocks()
    assert "build" in by_name, "build.yaml must define a 'build' job"
    return by_name["build"]


def _merge_steps():
    _, by_name = _load_blocks()
    assert "merge" in by_name, "build.yaml must define a 'merge' job"
    return by_name["merge"]


def test_build_push_action_pushes_only_when_same_repo():
    """The build step must not unconditionally push; fork PRs need the image
    to be built but not pushed. The ``outputs: push=...`` expression must use
    the fork guard so that push=false for fork PRs and push=true for main
    pushes and same-repo PRs."""
    matches = [s for s in _build_steps() if _step_field(s, "id") == "build"]
    assert len(matches) == 1, "Build workflow must have exactly one build-push step"
    outputs = _step_outputs(matches[0])
    assert "push=${{ " + FORK_GUARD + " }}" in outputs, (
        f"build step must gate push on fork detection; got outputs={outputs!r}"
    )


def test_login_step_is_gated_on_fork_detection():
    """Skipping the registry login for fork PRs avoids a 403 from
    docker/login-action when the read-only GITHUB_TOKEN is rejected."""
    login_steps = [
        s
        for s in _build_steps()
        if (_step_field(s, "uses") or "").startswith("docker/login-action")
    ]
    assert login_steps, "Build workflow must log in to the container registry"
    for step in login_steps:
        assert _has_step_field(step, "if"), "docker/login-action must be guarded by an if: condition"
        assert FORK_GUARD in (_step_field(step, "if") or ""), (
            f"docker/login-action must use the fork guard; got if={_step_field(step, 'if')!r}"
        )


def test_digest_export_is_gated_on_fork_detection():
    """The digest export references ``steps.build.outputs.digest`` which is
    empty when the image was not pushed; running it for fork PRs would
    create an empty digest file and break the upload-artifact step."""
    export_steps = [s for s in _build_steps() if _step_field(s, "name") == "Export digest"]
    assert len(export_steps) == 1, "Build workflow must have exactly one Export digest step"
    assert _has_step_field(export_steps[0], "if"), "Export digest step must be guarded by an if: condition"
    assert FORK_GUARD in (_step_field(export_steps[0], "if") or ""), (
        f"Export digest must use the fork guard; got if={_step_field(export_steps[0], 'if')!r}"
    )


def test_digest_upload_is_gated_on_fork_detection():
    """The upload-artifact step depends on the digest export; it must be
    skipped for fork PRs to avoid an ``if-no-files-found: error`` failure."""
    upload_steps = [
        s
        for s in _build_steps()
        if (_step_field(s, "uses") or "").startswith("actions/upload-artifact")
    ]
    assert len(upload_steps) == 1, "Build workflow must have exactly one upload-artifact step"
    assert _has_step_field(upload_steps[0], "if"), "Upload digest step must be guarded by an if: condition"
    assert FORK_GUARD in (_step_field(upload_steps[0], "if") or ""), (
        f"Upload digest must use the fork guard; got if={_step_field(upload_steps[0], 'if')!r}"
    )


def test_merge_job_is_gated_on_fork_detection():
    """The merge job downloads the digest artifact; that artifact does not
    exist for fork PRs, so the merge job must skip them entirely."""
    text, _ = _load_blocks()
    merge_lines = _merge_block_text(text)
    assert _has_job_field(merge_lines, "if"), "merge job must be guarded by an if: condition"
    assert FORK_GUARD in (_job_field(merge_lines, "if") or ""), (
        f"merge job must use the fork guard; got if={_job_field(merge_lines, 'if')!r}"
    )


def test_merge_job_push_steps_remain_gated_on_event_type():
    """Sanity check: the existing ``github.event_name != 'pull_request'``
    guards on the manifest-list and inspect steps must stay in place so
    same-repo PRs do not push the manifest list during PR CI."""
    merge_steps = _merge_steps()
    manifest_step = next(s for s in merge_steps if "manifest list" in (_step_field(s, "name") or ""))
    inspect_step = next(s for s in merge_steps if _step_field(s, "name") == "Inspect image")
    assert _has_step_field(manifest_step, "if"), "Create manifest list and push must stay guarded"
    assert _step_field(manifest_step, "if") == "github.event_name != 'pull_request'", (
        f"manifest list push must stay guarded on event type; "
        f"got if={_step_field(manifest_step, 'if')!r}"
    )
    assert _has_step_field(inspect_step, "if"), "Inspect image must stay guarded"
    assert _step_field(inspect_step, "if") == "github.event_name != 'pull_request'", (
        f"Inspect image must stay guarded on event type; "
        f"got if={_step_field(inspect_step, 'if')!r}"
    )


def test_build_step_still_loads_image_for_fork_prs():
    """Even when push=false, the build step must still produce an image so
    PR CI proves the Dockerfile compiles. The ``outputs: type=image,...``
    prefix must remain — only the ``push=`` suffix is conditional."""
    build_step = next(s for s in _build_steps() if _step_field(s, "id") == "build")
    outputs = _step_outputs(build_step)
    assert outputs.startswith("type=image,"), (
        f"build step must keep image output type for local loading; got outputs={outputs!r}"
    )
    assert "push-by-digest=true" in outputs, (
        f"build step must keep push-by-digest semantics; got outputs={outputs!r}"
    )