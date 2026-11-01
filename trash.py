from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import os
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

# Restore destinations are reserved with an exclusive ``flock`` on a
# per-destination *claim* file kept in the trash directory (see
# ``_reserve_restore_dest``). The files are restore bookkeeping, not trash
# items, so the trash listing and the maintenance sweeps skip them by name,
# and ``_reclaim_restore_claims`` removes the ones nobody holds once they are
# older than ``STAGE_ABANDON_GRACE_SECONDS``.
RESTORE_CLAIM_PREFIX = ".restore-claim-"
RESTORE_CLAIM_SUFFIX = ".lock"
# ``sha256().hexdigest()`` alphabet; ``_is_restore_claim_name`` requires a full
# digest so the sweep's name test cannot capture an ordinary file.
_CLAIM_DIGEST_ALPHABET = "0123456789abcdef"

# A trash metadata sidecar with no paired item is either a crashed claim or
# an in-flight one whose mover has not renamed its payload onto the name
# yet (``_claim_trash_dest`` creates the sidecar *before* the move). Deleting
# a live claim's sidecar re-opens the name to a second claimer and the two
# renames then clobber a payload (#606). The live claim is identified by the
# exclusive ``flock`` it holds on that sidecar for the whole move, which
# every sweep consults; this grace is the fallback for a crashed owner (whose
# lock the kernel released) and for filesystems without ``flock`` support.
# It must exceed any plausible move duration (cross-device copytree
# included) — mirroring the stage-abandon policy.
CLAIM_ABANDON_GRACE_SECONDS = 24 * 3600

# Attempts to take the claim lock. The claim creates its sidecar and locks
# it immediately; a sweep that opened the fresh sidecar in that instant can
# hold the lock for the length of one sweep step, so the claim retries
# briefly before giving up.
CLAIM_LOCK_ATTEMPTS = 20
CLAIM_LOCK_RETRY_SECONDS = 0.01

# Outcomes of taking the claim lock.
CLAIM_LOCK_HELD = "held"  # the claim owns the name exclusively
CLAIM_LOCK_UNSUPPORTED = "unsupported"  # no flock on this platform/filesystem
CLAIM_LOCK_CONTENDED = "contended"  # another worker holds the lock right now


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


def _lock_claim(fd: int) -> str:
    """Take the move's exclusive ``flock`` on a freshly created sidecar.

    The lock is taken on the very descriptor that created the sidecar, so
    no other worker can ever see the sidecar unlocked for longer than the
    single syscall between the create and the lock. A sweep that opened the
    brand-new sidecar in that instant can hold the lock for the length of
    one sweep step, so the claim retries briefly before giving up.

    Returns one of:

    - ``CLAIM_LOCK_HELD``: the claim owns the name exclusively, and every
      sweep and restore skips the sidecar until the move completes.
    - ``CLAIM_LOCK_UNSUPPORTED``: this platform or filesystem has no
      usable ``flock``. The claim is then protected by the sidecar's own
      existence (a second delete cannot claim the same name) and by the
      age-based reclaim policy, as before the lock protocol.
    - ``CLAIM_LOCK_CONTENDED``: another worker holds the lock on this
      sidecar right now. The claim cannot be made exclusive, so the caller
      must abandon it rather than proceed unprotected — proceeding would
      re-open exactly the race the lock closes.
    """
    if fcntl is None:
        return CLAIM_LOCK_UNSUPPORTED
    for _ in range(CLAIM_LOCK_ATTEMPTS):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return CLAIM_LOCK_HELD
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EACCES):
                # This filesystem does not support flock at all.
                return CLAIM_LOCK_UNSUPPORTED
            time.sleep(CLAIM_LOCK_RETRY_SECONDS)
    return CLAIM_LOCK_CONTENDED


def _claim_trash_dest(td: Path, name: str, ts: int) -> tuple[Path, int] | None:
    """Atomically claim a non-clashing destination path under the trash dir.

    Returns ``(dest, claim_fd)`` on success and ``None`` if no sidecar
    could be created (e.g. the trash directory became unwritable).
    *claim_fd* is the open descriptor of the sidecar; it carries the
    exclusive ``flock`` that marks the claim as live and must be closed
    once the move is done (closing releases the lock).

    The sidecar ``<dest>.meta.json`` is created with ``O_CREAT | O_EXCL``,
    which is atomic across threads *and* processes, and it doubles as the
    reservation token: every completed move writes exactly that sidecar for
    its item, so a candidate whose sidecar exists is already owned by
    another delete (or by a crashed one, until maintenance reclaims the
    stale meta). The ``dest.exists()`` probe additionally covers legacy
    items whose sidecar write failed. The exclusive create then closes the
    window between those probes and the caller's ``rename`` — a bare
    existence loop cannot do that, because POSIX ``rename`` overwrites and
    two deletes that both pass the loop would clobber each other (issue
    #606).

    The sidecar also serves as the move's lock file, following the same
    protocol ``_acquire_source_lock`` uses for restores and maintenance:
    the caller holds an exclusive ``flock`` on it from the claim until the
    move and its metadata write are complete. Without that lock a sweep
    could free the claimed name while the move is still in flight — the
    sweep would delete the claimed destination and its sidecar, a second
    delete could then claim the freed name, and the first delete's failure
    cleanup would remove the second delete's payload (issue #606 review).
    Where ``flock`` is unavailable the sidecar's own existence still
    reserves the name, and the age-based reclaim policy keeps a sweep away
    from it; a claim that cannot be made exclusive is abandoned instead of
    proceeding unprotected.
    A failed move must release the claim by unlinking the sidecar; a crash
    between claim and move leaves only a lone sidecar, which ``empty_trash``
    reclaims once it exceeds ``CLAIM_ABANDON_GRACE_SECONDS`` (never while
    it may still belong to an in-flight mover).
    """
    i = 0
    while True:
        dest = td / f"{ts}_{name}" if i == 0 else td / f"{ts}_{i}_{name}"
        try:
            if dest.exists():
                i += 1
                continue
            # The sidecar is created here rather than by the metadata write,
            # so its mode is set explicitly: ``os.open`` would otherwise
            # apply 0o777 masked by the umask, while the metadata file
            # this replaces was created by ``write_text`` with 0o666
            # masked by the umask.
            fd = os.open(_meta_path(dest), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            i += 1
            continue
        except OSError:
            return None
        lock_state = _lock_claim(fd)
        if lock_state == CLAIM_LOCK_CONTENDED:
            # Another worker holds this sidecar right now, so the claim
            # cannot be made exclusive. Proceeding would leave the move
            # unprotected against that worker — the very race the lock
            # closes — so give the name back and report the failure.
            _close_claim(fd)
            with contextlib.suppress(OSError):
                _meta_path(dest).unlink(missing_ok=True)
            return None
        return dest, fd


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


def _claim_owns_name(claim_fd: int, dest: Path) -> bool:
    """True while this call still owns the destination name.

    The sidecar is the reservation token for a trash name: a second delete
    can only claim a name by creating a sidecar of its own, so the
    sidecar's identity proves ownership. While this call owns the name the
    destination can only hold this call's own (possibly partial) payload
    and may be cleaned up on failure. Once the sidecar has been replaced
    the name belongs to another delete, and that delete's payload must not
    be touched.
    """
    try:
        claimed = os.fstat(claim_fd)
    except OSError:
        return False
    try:
        current = os.stat(_meta_path(dest))
    except OSError:
        return False
    return (claimed.st_dev, claimed.st_ino) == (current.st_dev, current.st_ino)


def _close_claim(claim_fd: int) -> None:
    """Close the claim descriptor, releasing the lock it holds."""
    with contextlib.suppress(OSError):
        os.close(claim_fd)


def _release_claim(dest: Path, claim_fd: int) -> None:
    """Release a failed claim by dropping the sidecar that reserved the name.

    The sidecar is unlinked only while this call still owns the name, so a
    failed move can never free a name that another delete has taken over
    in the meantime. The claim descriptor is closed by the caller's
    ``finally`` block.
    """
    if _claim_owns_name(claim_fd, dest):
        with contextlib.suppress(OSError):
            _meta_path(dest).unlink(missing_ok=True)


def _write_claim_meta(claim_fd: int, meta: dict) -> None:
    """Write the item's metadata into the claimed sidecar.

    The metadata goes through the claim descriptor itself, so it lands in
    the very inode the claim reserved and never in a sidecar that another
    delete may have taken over in the meantime.
    """
    payload = json.dumps(meta).encode("utf-8")
    os.ftruncate(claim_fd, 0)
    os.lseek(claim_fd, 0, os.SEEK_SET)
    written = 0
    while written < len(payload):
        written += os.write(claim_fd, payload[written:])


def move_to_trash(file_path: Path, data_folder: Path) -> bool:
    """Move a file or directory to the trash.

    Returns True on success, False if the path does not exist or is inaccessible.
    For directories, the entire tree is moved atomically via rename.

    The destination is claimed atomically before the move (see
    ``_claim_trash_dest``), so a concurrent delete in this process or any
    other worker can never select the same path and have its payload
    silently overwritten by the loser's ``rename`` (issue #606).

    The claim also holds an exclusive lock on its sidecar for the whole
    move — the rename, the cross-device copy fallback, the source removal
    and the metadata write. Trash maintenance consults that same lock, so
    a sweep cannot free the claimed name while the move is in flight: the
    sweep would otherwise delete the claimed destination and its sidecar, a
    second delete could then claim the freed name, and this call's failure
    cleanup would remove the second delete's payload (issue #606 review).
    A failed move releases the claim so the trash dir keeps no residue for
    the retry beyond what maintenance already reclaims.
    """
    if not file_path.exists():
        return False

    td = trash_dir(data_folder)
    rel = file_path.relative_to(data_folder).as_posix()
    ts = int(time.time())

    if file_path.is_file():
        claim = _claim_trash_dest(td, file_path.name, ts)
        if claim is None:
            return False
        dest, claim_fd = claim
        try:
            try:
                file_path.rename(dest)
            except OSError:
                _release_claim(dest, claim_fd)
                return False
            return _finish_trash_move(dest, claim_fd, rel)
        finally:
            _close_claim(claim_fd)
    elif file_path.is_dir():
        claim = _claim_trash_dest(td, file_path.name, ts)
        if claim is None:
            return False
        dest, claim_fd = claim
        try:
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
                # Clean up the partial copy on failure and release the claim.
                # The destination can only hold our own (partial) copy while
                # this call still owns the name: the sidecar reserves it, and
                # the claim's lock keeps every other delete and every sweep
                # away from it for the whole move.
                if _claim_owns_name(claim_fd, dest) and dest.exists():
                    shutil.rmtree(dest, ignore_errors=True)
                _release_claim(dest, claim_fd)
                return False
            return _finish_trash_move(dest, claim_fd, rel)
        finally:
            _close_claim(claim_fd)
    else:
        return False


def _finish_trash_move(dest: Path, claim_fd: int, rel: str) -> bool:
    """Record the metadata of a moved item in its claimed sidecar."""
    try:
        meta = {
            "original": rel,
            "deleted_at": _utcnow().isoformat(),
            "size": dir_size(dest) if dest.is_dir() else dest.stat().st_size,
        }
        _write_claim_meta(claim_fd, meta)
        return True
    except OSError:
        return False


def list_trash(data_folder: Path) -> list[dict]:
    td = trash_dir(data_folder)
    out = []
    for item in td.iterdir():
        if item.name.endswith(META_SUFFIX):
            continue
        if _is_restore_claim_name(item.name):
            # Reservation bookkeeping written by a restore, not a trashed item.
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


SOURCE_LOCK_HELD = "held"
SOURCE_LOCK_ACQUIRED = "acquired"
SOURCE_LOCK_UNAVAILABLE = "unavailable"


def _acquire_source_lock(meta_file: Path):
    """Acquire the exclusive source lock on a trash item's meta file.

    Every restorable trash item has a sibling ``<item>.meta.json``; an
    exclusive ``flock`` on that file is the "source in use" signal shared by
    ``restore_from_trash`` and the maintenance sweeps (``empty_trash``,
    ``purge_old_trash``). The meta file doubles as the lock file, so no
    extra lock file is created.

    Returns ``(fh, state)`` where *state* is:

    - ``SOURCE_LOCK_ACQUIRED``: *fh* is an open file description holding the
      exclusive flock. The caller must keep it open while it reads, copies,
      renames, or deletes the item, and release it by closing *fh* — never
      by unlinking the meta file (it is real metadata, unlike a stage lock
      file).
    - ``SOURCE_LOCK_HELD``: another live process holds the lock (EAGAIN or
      EACCES). The item is in use by a restore or by concurrent
      maintenance; the caller must leave item and metadata alone.
    - ``SOURCE_LOCK_UNAVAILABLE``: the state could not be determined (no
      meta file, ``fcntl`` is None, the open failed, or flock raised
      something other than EAGAIN/EACCES). Callers fall back to the
      historical lockless behavior.

    Like the stage-lock protocol, ``flock`` is honored across processes and
    the kernel releases it on process death, so a crashed restore leaves
    the item with a free lock and no grace machinery is needed for sources.
    """
    if fcntl is None:
        return None, SOURCE_LOCK_UNAVAILABLE
    try:
        # Read-only open: never create or truncate real metadata. A missing
        # meta file is a legitimate "unavailable" outcome.
        fh = open(meta_file, "r")  # noqa: SIM115
    except OSError:
        return None, SOURCE_LOCK_UNAVAILABLE
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        fh.close()
        if exc.errno in (errno.EAGAIN, errno.EACCES):
            return None, SOURCE_LOCK_HELD
        return None, SOURCE_LOCK_UNAVAILABLE
    return fh, SOURCE_LOCK_ACQUIRED


RESTORE_CLAIM_ACQUIRED = "acquired"
RESTORE_CLAIM_HELD = "held"
RESTORE_CLAIM_UNAVAILABLE = "unavailable"


class RestoreDestinationUnavailable(ValueError):
    """The restore destination could not be reserved, so the restore fails.

    Raised when the exclusive ``flock`` on the destination's claim file cannot
    be taken, or when the claim file cannot even be created. Without that lock
    a restore cannot tell an owned destination from a free one, and POSIX
    ``rename`` overwrites: renaming anyway would let one restore discard
    another restore's payload (issue #608).

    The restore fails closed instead. The trash item and its metadata stay in
    place, so the restore can be retried once the filesystem can lock the
    claim again.
    """


def _is_restore_claim_name(name: str) -> bool:
    """True for the trash-dir reservation files of ``_reserve_restore_dest``.

    The sweep skip is a name test, so the shape is deliberately narrow: the
    prefix, a 64-character lowercase hex digest, and the ``.lock`` suffix. A
    trashed item can never have that name anyway (``_unique_trash_dest`` writes
    every item as ``{timestamp}_{name}``), so no restorable payload is ever
    mistaken for bookkeeping, and a user file that merely *starts* with the
    prefix (``.restore-claim-notes.lock``) is still listed and deleted as the
    item it is.
    """
    if not (name.startswith(RESTORE_CLAIM_PREFIX) and name.endswith(RESTORE_CLAIM_SUFFIX)):
        return False
    digest = name[len(RESTORE_CLAIM_PREFIX):-len(RESTORE_CLAIM_SUFFIX)]
    return len(digest) == 64 and all(ch in _CLAIM_DIGEST_ALPHABET for ch in digest)


def _restore_claim_path(td: Path, dest: Path) -> Path:
    """Trash-dir claim file that guards ``dest`` as a restore destination.

    The name is a digest of the destination spelled under the *resolved* data
    folder, so workers configured with different spellings of ``DATA_FOLDER``
    (a symlinked path, a trailing slash) still contend for one claim file, and
    so no gallery path lands in a filename. ``sha256().hexdigest()`` is exactly
    the shape ``_is_restore_claim_name`` matches. Two distinct destinations
    digesting to the same claim only costs a spurious suffix on one restore; it
    can never cause an overwrite.
    """
    digest = hashlib.sha256(os.fsencode(dest.as_posix())).hexdigest()
    return td / f"{RESTORE_CLAIM_PREFIX}{digest}{RESTORE_CLAIM_SUFFIX}"


def _acquire_restore_claim(claim_path: Path, create: bool = True):
    """Reserve a restore destination with an exclusive ``flock`` on its claim.

    Returns ``(claim_fd, state)`` where *state* is ``RESTORE_CLAIM_ACQUIRED``
    (the caller holds the reservation through that raw descriptor and closes it
    once it is done with the destination), ``RESTORE_CLAIM_HELD`` (a live
    restore owns it) or ``RESTORE_CLAIM_UNAVAILABLE`` (no verdict possible). A
    descriptor rather than a file object so the claim is created with a private
    mode and without truncation.

    *create* selects who may bring the file into existence: a restore always
    does, while ``_reclaim_restore_claims`` passes ``False`` so a claim another
    sweep just deleted is not resurrected as a fresh inode behind that sweep's
    back.

    The file is opened without ``O_EXCL`` and is *never unlinked by a restore*,
    so exactly one inode ever backs a destination: a competing restore either
    loses the lock on that inode (held) or reserves it, which is what a
    create-if-missing-then-unlink protocol loses when a release races a reopen.
    ``O_NOFOLLOW`` keeps a claim path planted as a symlink (the trash directory
    is the one place gallery media lands) from turning the reservation into an
    ``flock`` on somebody else's inode; the open then fails and the restore
    fails closed (see ``RestoreDestinationUnavailable``).
    """
    if fcntl is None:
        return None, RESTORE_CLAIM_UNAVAILABLE
    flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0)
    try:
        # No truncation: the claim is inert bookkeeping and rewriting it would
        # reset the abandon age maintenance relies on.
        claim_fd = os.open(claim_path, flags, 0o600)  # noqa: SIM115
    except OSError:
        return None, RESTORE_CLAIM_UNAVAILABLE
    try:
        fcntl.flock(claim_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(claim_fd)
        if exc.errno in (errno.EAGAIN, errno.EACCES):
            return None, RESTORE_CLAIM_HELD
        return None, RESTORE_CLAIM_UNAVAILABLE
    return claim_fd, RESTORE_CLAIM_ACQUIRED


def _release_restore_claim(claim_fd) -> None:
    """Release a reservation by closing its descriptor; the file is kept."""
    if claim_fd is not None:
        with contextlib.suppress(OSError):
            os.close(claim_fd)


def _restore_dest_is_occupied(candidate: Path, payload_is_dir: bool) -> bool:
    """True when a restore must not rename its payload onto *candidate*.

    The restore renames onto the *unresolved* destination path, and
    ``rename(2)`` does not follow a symlink in the final component: the link
    itself is swapped out, so the file it pointed at keeps its own bytes and
    is never written through. A file payload may therefore take a name a
    symlink holds. (This is about the final component only — the kernel still
    resolves the intermediate ones, so a symlinked *parent* directory is a
    separate concern; see the escape guard in ``restore_from_trash`` and
    issue #615.)

    A directory payload may not: ``rename(2)`` refuses a directory onto a
    symlink (``ENOTDIR``), whatever the symlink points at. For a directory
    payload a symlink therefore counts as occupied and the restore moves on
    to the next candidate name, which keeps the restore working instead of
    failing on a rename the kernel would reject.
    """
    if candidate.is_symlink():
        return payload_is_dir
    return candidate.exists()


def _reserve_restore_dest(
    td: Path,
    dest: Path,
    canonical_parent: Path,
    *,
    payload_is_dir: bool,
) -> tuple[Path, int | None]:
    """Pick a restore destination for *dest* and reserve it before it is used.

    Returns ``(dest, claim_fd)``: the caller owns *claim_fd* and keeps it open
    across the ``rename`` into *dest*, closing it afterwards (never unlinking
    the claim file). *claim_fd* is never ``None``: a destination is only
    returned when the caller holds its claim.

    *canonical_parent* is the resolved destination directory: claim names are
    derived from it, so every worker contends for the same file however it
    spells ``DATA_FOLDER`` and two ``original`` paths that reach one directory
    through a symlink share one claim. The returned path stays unresolved — the
    caller's ``rename`` must keep replacing a symlink at the destination rather
    writing through it to whatever it points at.

    *payload_is_dir* tells the occupancy probe what the rename will carry: a
    file payload may replace a symlink at the destination, a directory payload
    cannot (see ``_restore_dest_is_occupied``).

    Candidates are the original name first, then ``{stem}_{ts}{suffix}`` with an
    incrementing timestamp. A candidate is only accepted while its claim is
    held, and the occupancy probe runs *inside* that claim, so a
    concurrent restore of a different trash item that shares the same
    ``original`` path (the same gallery path deleted twice over time) either
    sees the claim held and moves to the next candidate, or acquires it later,
    sees the winner's payload already at ``candidate`` and suffixes too. The
    old probe-then-rename order was a check-then-use race: POSIX ``rename``
    overwrites, so both restores reported success and one payload was discarded
    (issue #608) — the same class of race issue #606 addresses for trash
    entries. The per-item source flock does not help here: it serializes
    restores of one trash item, not two items pointing at one destination.

    When no lock verdict is possible (``fcntl`` is None, the trash directory
    cannot hold the claim, or the filesystem refuses ``flock``) the restore
    fails closed with ``RestoreDestinationUnavailable``. Renaming without the
    claim would reintroduce the very check-then-rename race the claim closes:
    two restores could pass the same existence probe and the later ``rename``
    would overwrite the earlier payload. Failing keeps both payloads — in the
    trash — instead of losing one of them.
    """
    name = dest.name
    ts = int(time.time())
    while True:
        candidate = dest.with_name(name)
        claim_fd, state = _acquire_restore_claim(_restore_claim_path(td, canonical_parent / name))
        if state == RESTORE_CLAIM_ACQUIRED:
            if not _restore_dest_is_occupied(candidate, payload_is_dir):
                return candidate, claim_fd
            # The candidate is taken: either a finished restore released this
            # claim and left its payload at the candidate, or a symlink sits
            # there (a directory payload cannot replace one). Hand the claim
            # back and try the next name.
            _release_restore_claim(claim_fd)
        elif state == RESTORE_CLAIM_UNAVAILABLE:
            # Fail closed: without the claim's exclusive lock this restore
            # cannot tell an owned destination from a free one, so renaming
            # now could discard another restore's payload (issue #608). The
            # item stays in the trash for a retry.
            logger.warning(
                "Cannot restore to %s: the destination reservation is unavailable",
                candidate,
            )
            raise RestoreDestinationUnavailable(
                f"Cannot restore {dest.name!r}: the destination could not be reserved. "
                "The trash directory must be writable and support file locking. "
                "The item stays in the trash; try again later."
            )
        name = f"{dest.stem}_{ts}{dest.suffix}"
        ts += 1


def restore_from_trash(item_name: str, data_folder: Path) -> bool:
    """Restore an item from trash to its original location.

    Returns True if restored, False if not found or invalid.
    Raises ValueError if *item_name* contains path traversal sequences.

    The destination is reserved before the item is renamed onto it (see
    ``_reserve_restore_dest``), so two concurrent restores whose metadata
    share one ``original`` path cannot overwrite each other's payload: the
    loser takes a timestamped name instead. Both the reservation and the
    collision probe are needed — POSIX ``rename`` overwrites, so a bare
    ``dest.exists()`` check leaves a window in this process and across
    Gunicorn workers (issue #608).

    When the destination cannot be reserved at all (no lock verdict: the
    platform has no ``flock``, the trash directory cannot hold the claim, or
    the filesystem refuses the lock), the restore fails closed with
    ``RestoreDestinationUnavailable`` — a ``ValueError`` the caller reports to
    the user. Renaming without the reservation would restore the
    check-then-rename race the claim closes, so the item and its metadata stay
    in the trash for a retry.

    For the whole duration of the restore the item's meta file is held
    under an exclusive flock (see ``_acquire_source_lock``): trash
    maintenance in any worker cannot delete the trash *source* while a
    cross-device ``copytree`` is still reading it (issue #557), and if
    maintenance holds the lock first, the restore fails fast before any
    copy starts. A ``ValueError`` (invalid item, unsafe meta, or an
    unreservable destination) propagates to the caller; any other failure
    returns False with item and metadata preserved so a retry stays possible.
    """
    # Reject path traversal in item_name (Flask's <path:> converter allows / and ..)
    # and embedded null bytes, which would make every path syscall below
    # raise ValueError from deep inside os.stat instead of this explicit guard.
    if "/" in item_name or "\\" in item_name or ".." in item_name or "\x00" in item_name:
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
    # Set once the destination is reserved; released in the finally below so a
    # ValueError (unsafe meta) cannot leak the reservation either.
    claim_fd = None

    # Source-side lock: hold an exclusive flock on the meta file across BOTH
    # restore paths (fast rename and the cross-device stage/copytree
    # fallback), the post-rename source cleanup, and the meta unlink, so the
    # trash source cannot vanish under an in-flight copytree. Maintenance
    # must *acquire* this lock (not merely probe it) before deleting, which
    # closes the probe-vs-acquire race in both directions: if maintenance
    # wins the lock the restore fails fast below; if the restore wins,
    # maintenance skips the item.
    lock_fh, lock_state = _acquire_source_lock(meta_file)
    if lock_state == SOURCE_LOCK_HELD:
        # Another worker is emptying, purging, or restoring this item.
        # Failing fast before any copy starts loses nothing (trash
        # semantics for a concurrently-emptied item) and keeps item+meta
        # untouched for the holder to finish with.
        return False
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
            resolved_dest_parent = dest.parent.resolve()
            resolved_data = data_folder.resolve()
        except OSError:
            return False
        if not (str(resolved_dest).startswith(str(resolved_data) + "/") or resolved_dest == resolved_data):
            # A symlink at the destination resolves to its target here, so a
            # link whose target escapes the data folder refuses the whole
            # restore. The rename would replace the link instead of writing
            # through it, so this is stricter than the rename needs; it is
            # kept deliberately. The check resolves the final component as
            # well, so a symlinked *parent* directory can still hide an
            # escape — tracked in #615.
            raise ValueError(f"Destination path escapes data folder: {rel!r}")
        # The final component may cancel out an escape in an intermediate
        # component (a symlinked parent pointing outside DATA_FOLDER whose
        # target points back inside), so the resolved destination alone can
        # look safe while rename(2) still follows the parent symlink and
        # writes outside DATA_FOLDER. The parent is checked separately, and
        # before any mkdir, so no directory can be created through it either
        # (issue #615).
        if not (
            str(resolved_dest_parent).startswith(str(resolved_data) + "/") or resolved_dest_parent == resolved_data
        ):
            raise ValueError(f"Destination parent path escapes data folder: {rel!r}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Reserve the destination before anything is renamed onto it, so a
        # concurrent restore of another trash item with the same ``original``
        # cannot land on the same name (see ``_reserve_restore_dest``). The
        # claim is keyed on the resolved parent (it exists by now); *dest*
        # stays unresolved so the rename below replaces a symlink at the
        # destination instead of writing through it.
        dest, claim_fd = _reserve_restore_dest(
            td, dest, dest.parent.resolve(), payload_is_dir=item.is_dir()
        )
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
                    # staged copy completed. Under the source-lock protocol
                    # this cannot come from empty/purge (they must acquire
                    # the same meta flock we hold); the branch remains as a
                    # safety net for lockless filesystems. The destination
                    # holds the full restored tree, so the restore itself
                    # succeeded; the missing source is exactly what a
                    # completed restore looks like.
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
    finally:
        # Release the destination reservation first: from here another restore
        # may claim the name, and it will see the payload this call renamed
        # there (or nothing at all, on a failed restore).
        _release_restore_claim(claim_fd)
        # Release the source lock by closing the descriptor only — never by
        # unlinking the meta file: the success path unlinked it explicitly
        # above, and a failed restore must keep item+meta for a retry.
        if lock_fh is not None:
            with contextlib.suppress(OSError):
                lock_fh.close()


def empty_trash(data_folder: Path) -> int:
    """Delete every trash item, skipping items whose source lock is held.

    Each deleted directory entry (an item or a meta file) counts 1. A meta
    file whose paired item still exists is left for the item's own
    iteration (deleting the meta first would strip the item of its lock
    file); a meta file with no item is stale *and older than
    ``CLAIM_ABANDON_GRACE_SECONDS``* and is unlinked directly. A lone meta
    newer than that grace may belong to an in-flight delete (the sidecar is
    created before the payload is renamed), so it is skipped, not counted
    (#606).

    For a real item, maintenance ACQUIRES (not merely probes) the source
    lock on the meta file and holds it across the delete of both the item
    and its meta (see ``_acquire_source_lock``). If the lock is held by
    another worker (an active restore or concurrent maintenance), the item
    and its meta are skipped entirely and not counted. If the lock cannot
    be acquired for any other reason (no meta file, ``fcntl`` is None, or
    flock unsupported/unexpectedly failing), the historical unconditional
    delete is kept for lockless filesystems. A raced non-ENOENT
    ``OSError`` from a delete is skipped with a debug log instead of
    vanishing silently (#597); only entries the sweep actually deleted
    are counted, preserving the sweep's do-not-propagate guarantee for
    the remaining entries.
    """
    td = trash_dir(data_folder)
    deleted = 0
    for item in td.iterdir():
        if _is_restore_claim_name(item.name):
            # Restore reservation bookkeeping, not a trash item: it is
            # reclaimed by ``_reclaim_restore_claims`` below, and deleting it
            # here would free a destination another worker is renaming into.
            continue
        try:
            if item.name.endswith(META_SUFFIX) and len(item.name) > len(META_SUFFIX):
                # The paired item's iteration deletes item + meta together
                # under the source lock; a lone meta means the item is
                # already gone, so the meta is stale. An entry literally
                # named ".meta.json" is a real trashed item, not metadata
                # (stripping the suffix would leave an empty paired name),
                # so it falls through to the ordinary item path below.
                if item.with_name(item.name[:-len(META_SUFFIX)]).exists():
                    continue
                if item.exists():
                    # A lone sidecar is either a crashed claim or an
                    # in-flight one whose mover has not renamed its payload
                    # onto the name yet. The mover holds an exclusive flock
                    # on the sidecar for the whole move, so a held lock
                    # identifies the in-flight case exactly; a free lock
                    # means the owner is gone and the sidecar is reclaimed
                    # once it also exceeds the claim grace.
                    lock_fh, lock_state = _acquire_source_lock(item)
                    if lock_state == SOURCE_LOCK_HELD:
                        continue
                    try:
                        if not _exceeds_abandon_grace(item, CLAIM_ABANDON_GRACE_SECONDS):
                            # A lone sidecar fresher than the claim grace is
                            # indistinguishable from an in-flight delete whose
                            # mover created the sidecar moments ago but has not
                            # renamed its payload yet; unlinking it would re-open
                            # the name to a second claimer and let the two
                            # renames clobber (#606). Leave it for a later sweep.
                            continue
                        try:
                            item.unlink(missing_ok=True)
                        except OSError as exc:
                            # Same skip-and-debug policy as the delete block
                            # below (#597): the entry stays retryable by later
                            # sweeps, and only a successful unlink counts.
                            logger.debug("empty_trash: skipping stale meta %s: %s", item, exc)
                        else:
                            deleted += 1
                    finally:
                        # Release by closing only; the meta file itself must
                        # never be unlinked from here.
                        if lock_fh is not None:
                            with contextlib.suppress(OSError):
                                lock_fh.close()
                continue
            lock_fh, lock_state = _acquire_source_lock(_meta_path(item))
            if lock_state == SOURCE_LOCK_HELD:
                # An active restore (or concurrent maintenance) owns this
                # item; deleting it mid-copy would report a successful
                # restore of media that is only partially copied (#557).
                continue
            try:
                # Symlink-aware delete, mirrored from purge_old_trash (#595):
                # is_symlink() is lstat-based and never follows the link,
                # and a symlink to a live directory is True for both
                # is_symlink() and is_dir() — so rmtree() would fail on it
                # (OSError swallowed), the meta would still be deleted, and
                # the link would survive every empty_trash call. Unlink the
                # link itself; is_symlink() catches a loop first, and
                # is_dir() would also return False on a loop (ELOOP is
                # swallowed by Path.is_dir()) so the unlinked loop under
                # the old order already worked.
                if item.is_symlink():
                    item.unlink(missing_ok=True)
                elif item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
                deleted += 1
                meta_file = _meta_path(item)
                if meta_file.exists():
                    meta_file.unlink(missing_ok=True)
                    deleted += 1
            except OSError as exc:
                # Same skip-and-continue policy as ``purge_old_trash`` (#597):
                # a raced non-ENOENT OSError from this single entry's delete
                # is debug-logged instead of vanishing silently into the
                # broad catch below; the sweep continues and only entries it
                # actually deleted are counted.
                logger.debug("empty_trash: skipping trash entry %s: %s", item, exc)
            finally:
                # Release by closing only; the meta was just deleted (or is
                # gone with it) and must never be re-unlinked from here.
                if lock_fh is not None:
                    with contextlib.suppress(OSError):
                        lock_fh.close()
        except Exception:
            pass
    _reclaim_restore_claims(td)
    _reclaim_orphan_restore_stages(data_folder)
    return deleted


def purge_old_trash(data_folder: Path, retention_days: int = 30) -> int:
    """Delete trash items older than the retention window.

    Counting is unchanged: one per expired item (its meta goes with it).
    Like ``empty_trash``, maintenance acquires the item's source lock on
    its meta file and holds it across the deletes; a lock held by another
    worker (active restore or concurrent maintenance) skips the item AND
    its meta without counting, while an unverifiable lock state falls back
    to the historical unconditional delete. See ``_acquire_source_lock``.
    An entry whose pre-lock age probes raise ``OSError`` (it vanished
    mid-sweep, e.g. a concurrent restore renamed it away, or it is
    otherwise unprobeable) is skipped and logged at debug level. The
    post-lock delete applies the same policy to a raced non-ENOENT
    ``OSError`` from the entry's own ``unlink()``/meta delete (#597): the
    sweep skips the failed entry with a debug log instead of escaping the
    sweep and 500ing the route, so one bad entry never denies service to
    the remaining trash, and only entries actually deleted are counted. An entry
    that is itself a dangling symlink raises ``FileNotFoundError`` from
    ``stat()`` (which follows the link) while ``lstat()`` succeeds; it is
    aged from the link inode's mtime, or the meta's ``deleted_at`` when
    present, so retention eventually collects it instead of skipping it
    on every sweep forever (#590). The same pre-lock probe covers a
    symlink-loop entry: ``stat()`` follows the link, hits ELOOP and raises
    a bare ``OSError`` (errno ELOOP, never FileNotFoundError), so the loop
    also falls back to the link inode — a symlink loop will not vanish on
    its own, so retention must eventually collect it (#595). If lstat()
    also fails the entry really vanished mid-sweep and the skip below
    still applies.
    """
    td = trash_dir(data_folder)
    cutoff = _utcnow() - timedelta(days=retention_days)
    deleted = 0
    for item in td.iterdir():
        if item.name.endswith(META_SUFFIX):
            continue
        if _is_restore_claim_name(item.name):
            # Reservation bookkeeping, not a trash item (see empty_trash). It
            # carries no ``deleted_at`` age, so a retention sweep would happily
            # delete a claim whose restore is holding the flock right now.
            continue
        meta_file = _meta_path(item)
        try:
            try:
                st = item.stat()
            except FileNotFoundError:
                st = item.lstat()
            except OSError as exc:
                if exc.errno != errno.ELOOP:
                    raise
                # A symlink-loop entry raises ELOOP from stat(): ELOOP is
                # not FileNotFoundError, so it would never reach the lstat
                # fallback and the entry would be skipped by every sweep
                # forever — a loop will not vanish on its own, so age the
                # link inode exactly like the dangling case (#590/#595). If
                # lstat() fails too the entry really vanished and the skip
                # below still applies.
                st = item.lstat()
            deleted_at = _from_timestamp(st.st_mtime)
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text())
                    deleted_at = _parse_deleted_at(meta.get("deleted_at", deleted_at.isoformat()))
                except Exception:
                    pass
        except OSError as exc:
            # A concurrent restore's rename can move the entry away between
            # iterdir() and these pre-lock probes: the source lock only
            # exists once the expiry decision is made, so it cannot cover
            # this window. A vanished entry is a benign sweep outcome, not a
            # purge error: skip it and continue (#565). Other OSErrors (an
            # inaccessible entry) get the same skip, logged so a permanently
            # unprobeable entry is at least visible in the logs instead of
            # silently surviving every sweep.
            logger.debug("purge_old_trash: skipping trash entry %s: %s", item, exc)
            continue
        if deleted_at < cutoff:
            lock_fh, lock_state = _acquire_source_lock(meta_file)
            if lock_state == SOURCE_LOCK_HELD:
                # Owned by a live restore (or concurrent maintenance): the
                # item and its meta both stay, uncounted.
                continue
            try:
                # is_symlink() is checked FIRST: it is lstat-based and never
                # follows the link, while a symlink to a live directory is
                # True for both is_symlink() and is_dir(). rmtree() cannot
                # remove a symlink (its OSError is swallowed by
                # ignore_errors) and the link must never be followed to its
                # still-live target — the old order deleted the meta,
                # counted the entry as purged, and let the link survive
                # every sweep forever (#595). Checking is_symlink() first
                # also closes the reviewed race where a dangling link's
                # target reappears as a directory between the age decision
                # and the is_dir() check.
                if item.is_symlink():
                    item.unlink(missing_ok=True)
                elif item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
                meta_file.unlink(missing_ok=True)
                deleted += 1
            except OSError as exc:
                # Same skip-and-continue policy as the pre-lock probes above:
                # a raced non-ENOENT OSError from this single entry's
                # unlink() or meta delete (a restore replacing the entry
                # mid-delete, an EACCES-like fault) must not escape the
                # sweep and 500 the route while the remaining trash goes
                # unpurged (#597). Only entries the sweep actually deleted
                # are counted; an item deleted with a failed meta delete
                # leaves a stale meta that the next empty_trash sweep
                # reclaims.
                logger.debug("purge_old_trash: skipping trash entry %s: %s", item, exc)
            finally:
                if lock_fh is not None:
                    with contextlib.suppress(OSError):
                        lock_fh.close()
    _reclaim_restore_claims(td)
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


def _exceeds_abandon_grace(path: Path, grace_seconds: float | None = None) -> bool:
    """True if *path*'s inode is older than the given abandon grace.

    *grace_seconds* defaults to ``STAGE_ABANDON_GRACE_SECONDS``, read at
    call time so tests (and future policy changes) can retune the module
    constant. Uses ``st_ctime`` rather than ``st_mtime`` because
    ``copytree`` applies the *source's* mtimes to the copy, which would
    make a freshly copied stage of old media look old.
    """
    if grace_seconds is None:
        grace_seconds = STAGE_ABANDON_GRACE_SECONDS
    try:
        st = path.stat()
    except OSError:
        return False
    return (time.time() - st.st_ctime) > grace_seconds


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


def _reclaim_restore_claims(td: Path) -> None:
    """Retire restore-destination claim files that no live process holds.

    A restore never unlinks its claim (see ``_acquire_restore_claim``), so the
    sweeps that already clean orphaned stages and stale sidecars retire them.
    A claim is retired only when this sweep can hold its ``flock`` itself
    (nobody else reserves it) *and* it is older than
    ``STAGE_ABANDON_GRACE_SECONDS``; anything held, or whose lock state cannot
    be verified (``RESTORE_CLAIM_UNAVAILABLE``), is kept, so a lock-deprived or
    contested filesystem never loses a reservation.

    Retiring takes the lock *before* the ``unlink`` and drops it only
    afterwards, which is what closes the gap a probe-then-unlink would leave:
    a restore that opened this inode just before the unlink is refused the flock
    by this sweep and takes the next candidate name, instead of ending up on an
    unlinked inode while the next restore creates a fresh one for the same
    destination. Two inodes for one destination means no mutual exclusion, so
    the overwrite ``_reserve_restore_dest`` exists to prevent (issue #608) would
    be back. Claims are never rewritten, so their age is their creation time
    and the grace bounds how long an abandoned one survives.

    ``create=False`` matters for the same reason: a claim another sweep deleted
    a moment ago must not be resurrected here, or both sweeps end up on
    different inodes of a name nobody is restoring.
    """
    try:
        entries = list(td.iterdir())
    except OSError:
        return
    for entry in entries:
        if not _is_restore_claim_name(entry.name):
            continue
        claim_fd, state = _acquire_restore_claim(entry, create=False)
        if state != RESTORE_CLAIM_ACQUIRED:
            continue
        try:
            if _exceeds_abandon_grace(entry):
                with contextlib.suppress(OSError):
                    entry.unlink(missing_ok=True)
        finally:
            _release_restore_claim(claim_fd)
