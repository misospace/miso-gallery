from __future__ import annotations

import contextlib
import errno
import json
import logging
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # non-POSIX platform: fall back to the age-based policy
    fcntl = None

logger = logging.getLogger(__name__)

TRASH_DIR_NAME = ".trash"
META_SUFFIX = ".meta.json"
STAGE_PREFIX = ".restore-stage-"
STAGE_LOCK_SUFFIX = ".lock"

# Stages that carry no verifiable live lock (legacy leftovers written before
# the lock protocol, or filesystems where flock is unsupported) are only
# reclaimed once they are older than this grace period. The flock on
# ``<stage>.lock`` is the primary active/abandoned signal; the grace period
# is the conservative fallback and must exceed any plausible restore
# duration so a slow or lock-deprived copy is never clobbered.
STAGE_ABANDON_GRACE_SECONDS = 24 * 3600


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _from_timestamp(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _parse_deleted_at(raw: str) -> datetime:
    """Parse a stored deleted_at timestamp, treating naive values as UTC.

    Legacy meta files (written before timezone-aware timestamps) lack an
    offset; normalize them so comparisons stay consistent.
    """
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _unique_trash_dest(td: Path, name: str, ts: int) -> Path:
    """Pick a non-clashing destination path under the trash dir."""
    dest = td / f"{ts}_{name}"
    i = 1
    while dest.exists():
        dest = td / f"{ts}_{i}_{name}"
        i += 1
    return dest


def dir_size(path: Path) -> int:
    """Estimate total size of a directory tree in bytes.

    Skips symlinks to prevent information disclosure and DoS via symlink
    cycles or external filesystem traversal.
    """
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_symlink():
                continue
            if entry.is_file():
                with contextlib.suppress(OSError):
                    total += entry.stat().st_size
                # Intentionally suppress OSError (e.g., permission denied) for
                # individual files during size estimation; we only need an estimate.
    except OSError:
        pass
    return total


def trash_dir(data_folder: Path) -> Path:
    path = data_folder / TRASH_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _meta_path(item_path: Path) -> Path:
    return item_path.with_name(item_path.name + META_SUFFIX)


def move_to_trash(file_path: Path, data_folder: Path) -> bool:
    """Move a file or directory to the trash.

    Returns True on success, False if the path does not exist or is inaccessible.
    For directories, the entire tree is moved atomically via rename.
    """
    if not file_path.exists():
        return False

    td = trash_dir(data_folder)
    rel = file_path.relative_to(data_folder).as_posix()
    ts = int(time.time())

    if file_path.is_file():
        dest = _unique_trash_dest(td, file_path.name, ts)
        try:
            file_path.rename(dest)
        except OSError:
            return False
    elif file_path.is_dir():
        dest = _unique_trash_dest(td, file_path.name, ts)
        try:
            # Prefer atomic rename (same filesystem) for immediate move.
            # Fall back to copytree+rmtree only when cross-device rename fails.
            try:
                file_path.rename(dest)
            except OSError:
                # symlinks=True keeps links as links (matching rename) so the
                # copy cannot inline symlink-target contents into the trash tree.
                shutil.copytree(file_path, dest, symlinks=True)
                shutil.rmtree(file_path)
        except OSError:
            # Clean up partial copy on failure
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            return False
    else:
        return False

    try:
        meta = {
            "original": rel,
            "deleted_at": _utcnow().isoformat(),
            "size": dir_size(dest) if dest.is_dir() else dest.stat().st_size,
        }
        _meta_path(dest).write_text(json.dumps(meta))
        return True
    except OSError:
        return False


def list_trash(data_folder: Path) -> list[dict]:
    td = trash_dir(data_folder)
    out = []
    for item in td.iterdir():
        if item.name.endswith(META_SUFFIX):
            continue
        meta_file = _meta_path(item)
        meta = {}
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
            except Exception:
                meta = {}
        size = dir_size(item) if item.is_dir() else item.stat().st_size
        out.append(
            {
                "name": item.name,
                "original": meta.get("original", "unknown"),
                "deleted_at": meta.get("deleted_at", _from_timestamp(item.stat().st_mtime).isoformat()),
                "size": size,
            }
        )
    out.sort(key=lambda x: x["deleted_at"], reverse=True)
    return out


def _is_safe_relative_path(path_str: str) -> bool:
    """Return True if *path_str* contains no path-traversal segments."""
    return all(part != ".." for part in path_str.replace("\\", "/").split("/"))


def _find_symlinks(root: Path) -> list[Path]:
    """Return all symlinks inside *root* (including *root* itself if it is one).

    Walks without following symlinks so external targets are never read.
    """
    found: list[Path] = []
    if root.is_symlink():
        found.append(root)
        return found
    try:
        for entry in root.rglob("*"):
            if entry.is_symlink():
                found.append(entry)
    except OSError:
        pass
    return found


def restore_from_trash(item_name: str, data_folder: Path) -> bool:
    """Restore an item from trash to its original location.

    Returns True if restored, False if not found or invalid.
    Raises ValueError if *item_name* contains path traversal sequences.
    """
    # Reject path traversal in item_name (Flask's <path:> converter allows / and ..)
    if "/" in item_name or "\\" in item_name or ".." in item_name:
        raise ValueError(f"Invalid trash item name: {item_name!r}")

    td = trash_dir(data_folder)
    item = td / item_name
    if not item.exists():
        return False

    # Refuse items that are symlinks or contain symlinks: shutil.copytree
    # defaults to symlinks=False and would inline symlink-target contents
    # into DATA_FOLDER, making external files reachable via /images/ etc.
    if item.is_symlink() or (item.is_dir() and _find_symlinks(item)):
        symlinks = [item] if item.is_symlink() else _find_symlinks(item)
        for link in symlinks:
            logger.warning(
                "Refusing to restore trash item %r: contains symlink %s",
                item_name,
                link,
            )
        raise ValueError(
            f"Cannot restore {item_name!r}: the trashed item contains "
            f"symlinks (e.g. {symlinks[0]}). "
            "Remove the item from trash to discard it."
        )

    # Verify resolved path stays within the trash directory
    try:
        resolved_item = item.resolve()
        resolved_td = td.resolve()
    except OSError:
        return False
    if not (str(resolved_item).startswith(str(resolved_td) + "/") or resolved_item == resolved_td):
        raise ValueError(f"Item path escapes trash directory: {item_name!r}")

    meta_file = _meta_path(item)
    try:
        meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
        rel = meta.get("original")
        if not rel:
            return False
        # Validate original path doesn't escape data_folder
        if not _is_safe_relative_path(rel):
            raise ValueError(f"Invalid original path in meta: {rel!r}")
        dest = data_folder / rel
        try:
            resolved_dest = dest.resolve()
            resolved_data = data_folder.resolve()
        except OSError:
            return False
        if not (str(resolved_dest).startswith(str(resolved_data) + "/") or resolved_dest == resolved_data):
            raise ValueError(f"Destination path escapes data folder: {rel!r}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            ts = int(time.time())
            if item.is_dir():
                while dest.exists():
                    dest = dest.with_name(f"{dest.stem}_{ts}{dest.suffix}")
                    ts += 1
            else:
                dest = dest.with_name(f"{dest.stem}_{ts}{dest.suffix}")
        if item.is_dir():
            try:
                # Move consumes the trash source so a successful restore
                # leaves no orphaned copy behind (matches file semantics).
                item.rename(dest)
            except OSError:
                # Cross-device fallback: stage the copy on the *destination*
                # filesystem (under dest.parent), then atomically rename it
                # into place. The stage MUST live on dest's filesystem: if
                # it lived next to the source, the staged rename would
                # itself cross the same cross-device boundary that just
                # failed the original rename (typically EXDEV), and the
                # fallback could never complete. Putting it under
                # dest.parent keeps the final rename on a single filesystem.
                #
                # The stage is exclusively owned by this call (UUID-named),
                # so failure cleanup only ever rmtree's our own stage —
                # never dest. Without staging, a concurrent duplicate
                # restore whose copytree fails because the winner already
                # populated dest would rmtree the winner's restored media.
                stage = dest.parent / f"{STAGE_PREFIX}{uuid.uuid4().hex}"
                # Mark the stage as actively owned *before* copying into it.
                # Trash maintenance (empty/purge in any worker) consults this
                # lock and must skip the stage while a live process holds it;
                # see _reclaim_orphan_restore_stages.
                stage_lock = _acquire_stage_lock(stage)
                stage_failed = True
                try:
                    shutil.copytree(item, stage, symlinks=True)
                    try:
                        stage.rename(dest)
                    except OSError:
                        # Another restore populated dest concurrently, or
                        # rename crossed devices again. Clean up only our
                        # stage; leave dest alone (it may belong to the
                        # winner or to a later retry).
                        shutil.rmtree(stage, ignore_errors=True)
                        raise
                    stage_failed = False
                finally:
                    if stage_failed and stage.exists():
                        shutil.rmtree(stage, ignore_errors=True)
                    _release_stage_lock(stage, stage_lock)
                try:
                    shutil.rmtree(item)
                except OSError:
                    if item.exists():
                        # Keep the trash entry and metadata so a retry stays
                        # possible; both copies exist but nothing is lost.
                        logger.warning(
                            "Restored %r to %s but could not remove trash source %s",
                            item_name,
                            dest,
                            item,
                        )
                        raise
                    # The trash source vanished while we were restoring it:
                    # concurrent trash maintenance reclaimed it after our
                    # staged copy completed. The destination holds the full
                    # restored tree, so the restore itself succeeded; the
                    # missing source is exactly what a completed restore
                    # looks like.
                    logger.info(
                        "Restored %r to %s; trash source %s was already "
                        "reclaimed by concurrent maintenance",
                        item_name,
                        dest,
                        item,
                    )
        else:
            item.rename(dest)
        if meta_file.exists():
            meta_file.unlink(missing_ok=True)
        return True
    except ValueError:
        raise
    except Exception:
        return False


def empty_trash(data_folder: Path) -> int:
    td = trash_dir(data_folder)
    deleted = 0
    for item in td.iterdir():
        try:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
            deleted += 1
        except Exception:
            pass
    _reclaim_orphan_restore_stages(data_folder)
    return deleted


def purge_old_trash(data_folder: Path, retention_days: int = 30) -> int:
    td = trash_dir(data_folder)
    cutoff = _utcnow() - timedelta(days=retention_days)
    deleted = 0
    for item in td.iterdir():
        if item.name.endswith(META_SUFFIX):
            continue
        meta_file = _meta_path(item)
        deleted_at = _from_timestamp(item.stat().st_mtime)
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
                deleted_at = _parse_deleted_at(meta.get("deleted_at", deleted_at.isoformat()))
            except Exception:
                pass
        if deleted_at < cutoff:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
            if meta_file.exists():
                meta_file.unlink(missing_ok=True)
            deleted += 1
    _reclaim_orphan_restore_stages(data_folder)
    return deleted


def _stage_lock_path(stage: Path) -> Path:
    """Path of the advisory lock file that marks *stage* as live."""
    return stage.with_name(stage.name + STAGE_LOCK_SUFFIX)


def _acquire_stage_lock(stage: Path):
    """Best-effort advisory lock marking *stage* as actively owned.

    The lock is an exclusive ``flock`` on ``<stage>.lock`` held for the
    lifetime of the copy. ``flock`` is honored across processes: trash
    maintenance running in any other worker sees the lock held and must not
    reclaim the stage. When a restore process crashes, the kernel drops the
    lock, so its stage becomes distinguishable as abandoned immediately —
    no timing heuristic needed for the normal case.

    Returns the open lock file on success. On *any* failure (flock
    unsupported, read-only filesystem, permissions) the lock file is removed
    before returning ``None``, so maintenance can never mistake an
    in-flight restore's lock for a crashed owner's released one; such
    stages fall back to the ``STAGE_ABANDON_GRACE_SECONDS`` age policy
    instead of immediate reclaim.
    """
    if fcntl is None:
        return None
    lock_path = _stage_lock_path(stage)
    try:
        # The descriptor must outlive this function: closing it releases the
        # flock, which is precisely what marks the stage as abandoned.
        fh = open(lock_path, "w")  # noqa: SIM115
    except OSError:
        return None
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        with contextlib.suppress(OSError):
            lock_path.unlink(missing_ok=True)
        return None
    return fh


def _release_stage_lock(stage: Path, fh) -> None:
    """Close the lock file (releasing the flock) and remove the lock file."""
    if fh is not None:
        with contextlib.suppress(OSError):
            fh.close()
    with contextlib.suppress(OSError):
        _stage_lock_path(stage).unlink(missing_ok=True)


def _stage_lock_state(lock_path: Path):
    """Classify a stage lock file for maintenance.

    Returns ``True`` when a live process holds the lock (the stage is an
    active restore's, never to be reclaimed), ``False`` when the lock file
    exists but no process holds it (its owner crashed: the kernel drops
    flocks on process death, so the stage is abandoned and reclaimable), and
    ``None`` when the state cannot be determined (flock unsupported or the
    lock file is inaccessible).
    """
    if fcntl is None:
        return None
    try:
        with open(lock_path, "r") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EACCES):
                    return True
                return None
            # Acquiring the lock proves nobody held it; exiting the with
            # closes the descriptor and releases it again.
    except OSError:
        return None
    return False


def _exceeds_abandon_grace(path: Path) -> bool:
    """True if *path*'s inode is older than the explicit abandon grace.

    Uses ``st_ctime`` rather than ``st_mtime`` because ``copytree`` applies
    the *source's* mtimes to the copy, which would make a freshly copied
    stage of old media look old.
    """
    try:
        st = path.stat()
    except OSError:
        return False
    return (time.time() - st.st_ctime) > STAGE_ABANDON_GRACE_SECONDS


def _remove_stage(stage: Path) -> None:
    shutil.rmtree(stage, ignore_errors=True)
    with contextlib.suppress(OSError):
        _stage_lock_path(stage).unlink(missing_ok=True)


def _reclaim_stage_dir(stage: Path) -> None:
    """Reclaim one ``.restore-stage-*`` directory, if abandoned.

    Active stages (lock held) are skipped entirely. Stages whose lock file
    exists but is held by nobody are reclaimed immediately (the owner
    crashed). Stages with no verifiable lock are reclaimed only once they
    exceed ``STAGE_ABANDON_GRACE_SECONDS``.
    """
    lock_path = _stage_lock_path(stage)
    if lock_path.exists():
        held = _stage_lock_state(lock_path)
        if held:
            return
        if held is False:
            _remove_stage(stage)
            return
    if _exceeds_abandon_grace(stage):
        _remove_stage(stage)


def _reclaim_stage_lock_file(lock_path: Path) -> None:
    """Reclaim a leaked lock file whose stage directory is already gone.

    Unlike a stage *directory* paired with a lock (whose existence proves
    the owner at least reached ``copytree``, hence had taken the flock), a
    lock file with no directory proves nothing: the owner may simply not
    have created the stage yet (the restore takes the flock a moment before
    ``copytree`` creates the directory). Unlinking such a fresh lock would
    strip a live restore of its protection mid-copy, so these orphans are
    reclaimed only past the abandon grace, never immediately.
    """
    stage = lock_path.with_name(lock_path.name[: -len(STAGE_LOCK_SUFFIX)])
    if stage.exists():
        # The paired directory entry owns reclaim of this lock.
        return
    if _stage_lock_state(lock_path):
        return
    if _exceeds_abandon_grace(lock_path):
        with contextlib.suppress(OSError):
            lock_path.unlink(missing_ok=True)


def _reclaim_orphan_restore_stages(data_folder: Path) -> None:
    """Reclaim ``.restore-stage-*`` directories left behind by a crashed restore.

    The cross-device fallback in ``restore_from_trash`` stages the copy on
    the destination filesystem (under ``dest.parent``). If the process
    crashes between ``copytree`` and the final ``rename``, the staged
    directory can leak. Because it lives under the data folder (not under
    ``td``), the normal ``empty_trash``/``purge_old_trash`` iteration over
    the trash directory would miss it; this helper sweeps the data folder
    for ``.restore-stage-*`` directories and removes abandoned ones.

    A stage is only abandoned if its owner is dead. A live restore holds an
    exclusive ``flock`` on ``<stage>.lock`` for the whole copy, so sweeping
    unconditionally would delete an in-progress restore (issue #557: a
    second worker's ordinary maintenance — or even a purge with the default
    30-day retention — could delete a stage created milliseconds earlier).
    The reclaim policy is:

    - lock held by a live process  -> skip (active restore)
    - stage directory present with a lock file nobody holds -> reclaim now
      (owner crashed; the kernel releases flocks on process death, and the
      directory's existence proves the owner had reached the flock'd copy)
    - no lock file / lock state unverifiable -> reclaim only after
      ``STAGE_ABANDON_GRACE_SECONDS``, so legacy orphans stay collectable
      while a slow or lock-deprived restore is never clobbered
    - lock file without a stage directory -> reclaim only after the same
      grace: the owner may not have created the stage yet, so a free lock
      alone is not crash evidence.

    ``flock`` works across processes (and is treated independently per open
    file description even within one process), which is what makes the
    signal valid between the app's concurrent workers; a per-process set
    could not protect stages owned by other workers. The immediate
    crashed-owner reclaim additionally assumes lock-coherent storage: the
    supported deployment is a single host's Docker volume. On a hypothetical
    multi-host mount where flock is per-client only, a free lock could be a
    false crash signal; deployments sharing DATA_FOLDER across hosts would
    need coherent locks (or should raise the grace and rely on it).
    """
    for entry in data_folder.rglob(f"{STAGE_PREFIX}*"):
        try:
            if entry.name.endswith(STAGE_LOCK_SUFFIX):
                _reclaim_stage_lock_file(entry)
            elif entry.is_dir():
                _reclaim_stage_dir(entry)
        except OSError:
            pass
