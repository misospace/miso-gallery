"""Behavioral tests for folder selection, deletion, and trash behavior.

Covers:
- Single file move to trash (existing path)
- Single directory move to trash (fixed path)
- Bulk delete with mixed files and folders
- Restore from trash for both files and directories
- Trash listing includes directories
- Nested folder deletion
- Empty folder deletion
- Form-based bulk delete (web UI path)
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

import trash
from conftest import build_client
from trash import dir_size, list_trash, move_to_trash, purge_old_trash, restore_from_trash


def test_dir_size_ignores_stat_errors_and_symlinks(tmp_path, monkeypatch):
    root = tmp_path / "folder"
    root.mkdir()
    readable = root / "readable.txt"
    readable.write_text("ok")
    (root / "outside-link").symlink_to(tmp_path / "outside.txt")
    original_stat = trash.Path.stat

    def failing_stat(path, *args, **kwargs):
        if path == readable:
            raise OSError("unreadable")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(trash.Path, "stat", failing_stat)
    assert dir_size(root) == 0


def test_move_to_trash_falls_back_to_copy_and_cleans_partial_copy(tmp_path, monkeypatch):
    data = tmp_path / "data"
    source = data / "album"
    source.mkdir(parents=True)
    (source / "photo.txt").write_text("photo")
    real_rename = trash.Path.rename
    calls = []

    def cross_device_rename(path, destination):
        calls.append((path, destination))
        if path == source:
            raise OSError("cross-device")
        return real_rename(path, destination)

    monkeypatch.setattr(trash.Path, "rename", cross_device_rename)
    assert move_to_trash(source, data) is True
    assert not source.exists()
    assert calls

    source = data / "broken"
    source.mkdir()
    (source / "photo.txt").write_text("photo")
    original_copytree = trash.shutil.copytree

    def partial_copy(*args, **kwargs):
        destination = args[1]
        destination.mkdir()
        (destination / "partial.txt").write_text("partial")
        raise OSError("copy failed")

    monkeypatch.setattr(trash.Path, "rename", cross_device_rename)
    monkeypatch.setattr(trash.shutil, "copytree", partial_copy)
    assert move_to_trash(source, data) is False
    assert source.exists()
    assert not any(path.name.startswith("broken") for path in trash.trash_dir(data).iterdir())
    monkeypatch.setattr(trash.shutil, "copytree", original_copytree)


def _concurrent_delete_with_claim_barrier(monkeypatch, paths, data):
    """Run ``move_to_trash`` for each path in its own thread, synchronizing
    every thread right after its destination claim returns and pinning the
    clock so all deletes target the same candidate names.

    This pins the issue #606 interleaving: under the old check-then-rename
    scheme every thread would pass the existence loop before any rename,
    so all of them picked the same destination and the later ``rename``
    clobbered the earlier payload.
    """
    real_claim = trash._claim_trash_dest
    barrier = threading.Barrier(len(paths), timeout=10)

    def claim_then_sync(td, name, ts):
        claim = real_claim(td, name, ts)
        barrier.wait()
        return claim

    monkeypatch.setattr(trash, "_claim_trash_dest", claim_then_sync)
    monkeypatch.setattr(trash.time, "time", lambda: 1234)
    results = {}

    def run(path):
        results[str(path)] = move_to_trash(path, data)

    threads = [threading.Thread(target=run, args=(path,)) for path in paths]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results


def test_concurrent_file_deletes_keep_both_payloads(tmp_path, monkeypatch):
    """Two same-second deletes of same-basename files must both succeed with
    their payload and original-path metadata intact (issue #606)."""
    data = tmp_path / "data"
    (data / "a").mkdir(parents=True)
    (data / "b").mkdir()
    (data / "a/photo.png").write_text("first")
    (data / "b/photo.png").write_text("second")

    results = _concurrent_delete_with_claim_barrier(
        monkeypatch, [data / "a/photo.png", data / "b/photo.png"], data
    )

    assert results == {str(data / "a/photo.png"): True, str(data / "b/photo.png"): True}
    assert not (data / "a/photo.png").exists()
    assert not (data / "b/photo.png").exists()
    td = trash.trash_dir(data)
    items = [p for p in td.iterdir() if not p.name.endswith(trash.META_SUFFIX)]
    assert len(items) == 2
    payload_original_pairs = set()
    for item in items:
        meta = json.loads(trash._meta_path(item).read_text())
        payload_original_pairs.add((item.read_text(), meta["original"]))
    assert payload_original_pairs == {("first", "a/photo.png"), ("second", "b/photo.png")}


def test_concurrent_folder_deletes_keep_both_trees(tmp_path, monkeypatch):
    """The claim also guards same-second deletes of same-name folders."""
    data = tmp_path / "data"
    (data / "a/album").mkdir(parents=True)
    (data / "b/album").mkdir(parents=True)
    (data / "a/album/marker.txt").write_text("first")
    (data / "b/album/marker.txt").write_text("second")

    results = _concurrent_delete_with_claim_barrier(
        monkeypatch, [data / "a/album", data / "b/album"], data
    )

    assert results == {str(data / "a/album"): True, str(data / "b/album"): True}
    assert not (data / "a/album").exists()
    assert not (data / "b/album").exists()
    td = trash.trash_dir(data)
    items = [p for p in td.iterdir() if not p.name.endswith(trash.META_SUFFIX)]
    assert len(items) == 2
    payload_original_pairs = set()
    for item in items:
        meta = json.loads(trash._meta_path(item).read_text())
        payload_original_pairs.add(((item / "marker.txt").read_text(), meta["original"]))
    assert payload_original_pairs == {("first", "a/album"), ("second", "b/album")}


def test_trash_claim_skips_inflight_claim_and_metaless_item(tmp_path, monkeypatch):
    """A sidecar with no item (an in-flight or crashed concurrent delete) and
    a legacy item with no sidecar both force the next candidate name, so the
    delete never renames onto an occupied destination."""
    data = tmp_path / "data"
    data.mkdir()
    td = trash.trash_dir(data)
    (td / "1234_photo.png.meta.json").write_text("")
    legacy = td / "1234_1_photo.png"
    legacy.write_text("legacy")
    src = data / "photo.png"
    src.write_text("new")

    monkeypatch.setattr(trash.time, "time", lambda: 1234)
    assert move_to_trash(src, data) is True

    assert (td / "1234_2_photo.png").read_text() == "new"
    meta = json.loads((td / "1234_2_photo.png.meta.json").read_text())
    assert meta["original"] == "photo.png"
    assert legacy.read_text() == "legacy"
    assert not (td / "1234_photo.png").exists()


def test_empty_trash_keeps_live_claim_sidecar(tmp_path, monkeypatch):
    """A claimed sidecar whose mover has not renamed its payload onto the
    name yet must survive an ``empty_trash`` sweep: reclaiming it would
    re-open the name to a second claimer and let the two renames clobber a
    payload (#606).

    The mover holds an exclusive lock on its sidecar for the whole move, so
    the sweep skips it even once it is older than the claim grace: the
    lock, not the age, is what identifies a live claim. An unlocked
    sidecar (a crashed owner, whose lock the kernel released) is still
    collected only after the grace."""
    data = tmp_path / "data"
    data.mkdir()
    td = trash.trash_dir(data)
    dest, claim_fd = trash._claim_trash_dest(td, "photo.png", 1234)
    assert dest == td / "1234_photo.png"

    # The name stays pinned while the claim is live, so the next claimant
    # is forced to a different destination.
    next_dest, next_fd = trash._claim_trash_dest(td, "photo.png", 1234)
    assert next_dest == td / "1234_1_photo.png"
    os.close(next_fd)

    # Age the sidecars past the claim grace: only the live lock keeps the
    # claimed one, while the released one becomes reclaimable.
    now = time.time()
    monkeypatch.setattr(trash.time, "time", lambda: now + 25 * 86400)

    assert trash.empty_trash(data) == 1
    assert trash._meta_path(dest).exists(), "the sweep reclaimed a live claim"
    assert not trash._meta_path(next_dest).exists()

    # Once the owner is gone the lock is released, and the aged sidecar
    # becomes reclaimable too.
    os.close(claim_fd)
    assert trash.empty_trash(data) == 1
    assert not trash._meta_path(dest).exists()


def test_empty_trash_reclaims_unlocked_stale_sidecar_after_grace(tmp_path, monkeypatch):
    """A sidecar whose owner is gone (no lock held) is reclaimed once it
    exceeds the claim grace.

    The lock is the primary signal that a claim is live; the age is the
    fallback that collects the sidecar of a crashed owner, whose lock the
    kernel already released."""
    data = tmp_path / "data"
    data.mkdir()
    td = trash.trash_dir(data)
    stale = td / "ghost.txt.meta.json"
    stale.write_text(json.dumps({"original": "ghost.txt"}))

    now = time.time()
    monkeypatch.setattr(trash.time, "time", lambda: now + 25 * 86400)

    assert trash.empty_trash(data) == 1
    assert not stale.exists()


def test_concurrent_deletes_across_processes_keep_both_payloads(tmp_path):
    """Two OS processes deleting same-basename files must both keep their
    payload (issue #606).

    The claim is an ``O_CREAT | O_EXCL`` create of the destination's
    metadata sidecar, which the kernel makes atomic across processes, so
    the two workers cannot pick the same destination name and the loser's
    ``rename`` cannot overwrite the winner's payload.
    """
    data = tmp_path / "data"
    (data / "a").mkdir(parents=True)
    (data / "b").mkdir()
    (data / "a/photo.png").write_text("first")
    (data / "b/photo.png").write_text("second")

    read_fd, write_fd = os.pipe()
    pids = []
    for src in (data / "a/photo.png", data / "b/photo.png"):
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            # Pin the clock in the child so both workers target the same
            # candidate name, then delete concurrently.
            trash.time.time = lambda: 1234
            ok = trash.move_to_trash(src, data)
            os.write(write_fd, b"1" if ok else b"0")
            os._exit(0)
        pids.append(pid)
    os.close(write_fd)

    results = b""
    while len(results) < 2:
        chunk = os.read(read_fd, 2 - len(results))
        if not chunk:
            break
        results += chunk
    os.close(read_fd)
    for pid in pids:
        os.waitpid(pid, 0)

    assert results == b"11", "both processes must report a successful delete"

    td = trash.trash_dir(data)
    items = [p for p in td.iterdir() if not p.name.endswith(trash.META_SUFFIX)]
    assert len(items) == 2, "one payload was overwritten"
    payload_original = set()
    for item in items:
        meta = json.loads(trash._meta_path(item).read_text())
        payload_original.add((item.read_text(), meta["original"]))
    assert payload_original == {("first", "a/photo.png"), ("second", "b/photo.png")}


def test_move_to_trash_rejects_paths_with_null_bytes(tmp_path):
    """A source path with an embedded null byte is rejected, not crashed on.

    The trash names come from the source basename, so a name carrying a
    null byte would otherwise reach the filesystem calls and raise
    ``ValueError`` deep inside ``os.stat``."""
    data = tmp_path / "data"
    data.mkdir()
    src = data / "bad\x00name"
    assert src.exists() is False
    assert move_to_trash(src, data) is False
    assert list(trash.trash_dir(data).iterdir()) == []


def test_move_to_trash_keeps_symlinks_as_links(tmp_path):
    """A trashed symlink stays a symlink; its target is left alone.

    The rename moves the link itself, and the sidecar records the link's
    own path, so restoring can refuse it (as it does for any symlinked
    trash item) instead of leaking the target's contents."""
    data = tmp_path / "data"
    data.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET")
    link = data / "link.png"
    link.symlink_to(outside)

    assert move_to_trash(link, data) is True

    td = trash.trash_dir(data)
    trashed = [p for p in td.iterdir() if not p.name.endswith(trash.META_SUFFIX)]
    assert len(trashed) == 1
    assert trashed[0].is_symlink(), "the trashed entry must stay a link"
    assert trashed[0].resolve() == outside.resolve()
    # The link still points at the same target, which was never moved.
    assert outside.read_text() == "SECRET"
    assert json.loads(trash._meta_path(trashed[0]).read_text())["original"] == "link.png"


def test_sweep_during_folder_move_cannot_free_the_claimed_name(tmp_path, monkeypatch):
    """A sweep that lands mid-move must not free the claimed name (#606 review).

    The folder path copies the tree into the claimed destination before the
    source is removed, so a concurrent ``empty_trash`` used to delete the
    half-copied destination and its sidecar. The name was then free for a
    second delete to claim, and the first mover's metadata write and cleanup
    ran against the second mover's payload.

    The mover now holds an exclusive lock on its claim sidecar for the whole
    move, so the sweep skips both the destination and the sidecar, and the
    second delete is forced onto a different name."""
    data = tmp_path / "data"
    (data / "a").mkdir(parents=True)
    (data / "b").mkdir()
    (data / "a/album").mkdir()
    (data / "a/album/first.txt").write_text("first")
    (data / "b/album").mkdir()
    (data / "b/album/second.txt").write_text("second")

    real_rename = trash.Path.rename

    def cross_device_rename(path, destination):
        if path == data / "a/album":
            raise OSError(errno.EXDEV, "cross-device link")
        return real_rename(path, destination)

    monkeypatch.setattr(trash.Path, "rename", cross_device_rename)

    real_copytree = trash.shutil.copytree
    events = []

    def copytree_with_sweep(src, dest, *args, **kwargs):
        result = real_copytree(src, dest, *args, **kwargs)
        # shutil.copytree recurses through this same attribute; only the
        # top-level call from move_to_trash passes Path objects.
        if isinstance(dest, Path):
            # The payload is in place and the mover still holds its claim:
            # a sweep must not delete either.
            events.append(("sweep", trash.empty_trash(data)))
            # And a second delete of a same-named folder must not be able to
            # take the claimed name.
            events.append(("second", trash.move_to_trash(data / "b/album", data)))
        return result

    monkeypatch.setattr(trash.shutil, "copytree", copytree_with_sweep)

    assert move_to_trash(data / "a/album", data) is True

    assert events[0] == ("sweep", 0), "the sweep deleted the in-flight claim"
    assert events[1][0] == "second"
    assert events[1][1] is True

    td = trash.trash_dir(data)
    items = [p for p in td.iterdir() if not p.name.endswith(trash.META_SUFFIX)]
    assert len(items) == 2, "the in-flight payload was lost"
    payload_original = set()
    for item in items:
        meta = json.loads(trash._meta_path(item).read_text())
        payload_original.add(((item / "first.txt").read_text()
                              if (item / "first.txt").exists()
                              else (item / "second.txt").read_text(), meta["original"]))
    assert payload_original == {("first", "a/album"), ("second", "b/album")}


def test_failed_move_does_not_remove_a_payload_the_move_no_longer_owns(tmp_path, monkeypatch):
    """A failed move may only clean up while it still owns the name.

    The sidecar is the reservation token, so a replaced sidecar means
    another delete took the name over. The failing mover must then leave
    that payload — and the new owner's sidecar — alone."""
    data = tmp_path / "data"
    data.mkdir()
    td = trash.trash_dir(data)
    source = data / "album"
    source.mkdir()
    (source / "marker.txt").write_text("mine")
    monkeypatch.setattr(trash.time, "time", lambda: 1234)

    real_rename = trash.Path.rename

    def cross_device_rename(path, destination):
        if path == source:
            raise OSError(errno.EXDEV, "cross-device link")
        return real_rename(path, destination)

    monkeypatch.setattr(trash.Path, "rename", cross_device_rename)

    def copytree_that_loses_the_name(src, dest, *args, **kwargs):
        # Simulate the claim being taken over: the sidecar is replaced by
        # another delete's sidecar, and that delete's payload occupies the
        # destination.
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "theirs.txt").write_text("theirs")
        trash._meta_path(dest).unlink(missing_ok=True)
        trash._meta_path(dest).write_text(json.dumps({"original": "theirs"}))
        raise OSError("copy failed")

    monkeypatch.setattr(trash.shutil, "copytree", copytree_that_loses_the_name)

    assert move_to_trash(source, data) is False

    # The foreign payload and the foreign sidecar both survive.
    assert (td / "1234_album/theirs.txt").read_text() == "theirs"
    assert json.loads((td / "1234_album.meta.json").read_text())["original"] == "theirs"
    # The source is untouched: the move failed.
    assert (source / "marker.txt").read_text() == "mine"


def test_list_trash_ignores_corrupt_metadata_and_unreadable_entries(tmp_path, monkeypatch):
    data = tmp_path / "data"
    td = trash.trash_dir(data)
    item = td / "item"
    item.write_text("contents")
    item.with_name(item.name + trash.META_SUFFIX).write_text("not-json")
    original_stat = trash.Path.stat

    def failing_stat(path, *args, **kwargs):
        if path == item:
            raise OSError("unreadable")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(trash.Path, "stat", failing_stat)
    with pytest.raises(OSError):
        list_trash(data)


def test_restore_rejects_traversal_and_renames_collisions(tmp_path):
    data = tmp_path / "data"
    td = trash.trash_dir(data)
    item = td / "deleted.txt"
    item.write_text("deleted")
    item.with_name(item.name + trash.META_SUFFIX).write_text(
        json.dumps({"original": "albums/photo.txt"})
    )
    (data / "albums").mkdir()
    (data / "albums/photo.txt").write_text("existing")
    assert restore_from_trash(item.name, data) is True
    assert (data / "albums/photo.txt").read_text() == "existing"
    restored = list((data / "albums").glob("photo_*.txt"))
    assert len(restored) == 1
    assert restored[0].read_text() == "deleted"

    for name in ("../item", "nested/item", "\\\\item"):
        with pytest.raises(ValueError):
            restore_from_trash(name, data)


def test_restore_rejects_escaping_original_and_purge_falls_back_to_mtime(tmp_path):
    data = tmp_path / "data"
    td = trash.trash_dir(data)
    item = td / "unsafe"
    item.write_text("unsafe")
    item.with_name(item.name + trash.META_SUFFIX).write_text(
        json.dumps({"original": "../outside.txt"})
    )
    with pytest.raises(ValueError):
        restore_from_trash(item.name, data)

    old = td / "old.txt"
    old.write_text("old")
    old_meta = old.with_name(old.name + trash.META_SUFFIX)
    old_meta.write_text(json.dumps({"deleted_at": "legacy-invalid"}))
    old_time = time.time() - 3 * 86400
    import os
    os.utime(old, (old_time, old_time))
    assert purge_old_trash(data, retention_days=1) == 1
    assert not old.exists()


def test_restore_refuses_destination_whose_parent_escapes_data_folder(tmp_path):
    """A symlinked parent directory must not redirect a restore outside DATA_FOLDER (#615).

    ``data/albums`` is a symlink to a directory outside the data folder and
    ``outside/photo.txt`` points back inside, so the fully resolved
    destination lands inside DATA_FOLDER and the final-component guard
    passes. The kernel still resolves the intermediate symlink on rename(2),
    so the payload would be written outside DATA_FOLDER. The resolved
    *parent* is therefore checked separately, before any mkdir.
    """
    data = tmp_path / "data"
    data.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (data / "inside.txt").write_bytes(b"INSIDE")

    td = trash.trash_dir(data)
    item = td / "deleted.txt"
    item.write_bytes(b"SECRET-PAYLOAD")
    meta = item.with_name(item.name + trash.META_SUFFIX)
    meta.write_text(json.dumps({"original": "albums/photo.txt"}))

    # The attack: an intermediate symlink leaving DATA_FOLDER whose target
    # points back inside, so the final component cancels the escape.
    (data / "albums").symlink_to(outside)
    (outside / "photo.txt").symlink_to(data / "inside.txt")

    # Precondition: the fully resolved destination is inside DATA_FOLDER,
    # i.e. the old final-component guard would have let this restore through.
    resolved_dest = (data / "albums" / "photo.txt").resolve()
    assert resolved_dest == (data / "inside.txt").resolve()
    assert str(resolved_dest).startswith(str(data.resolve()) + "/")

    with pytest.raises(ValueError, match="Destination parent path escapes data folder"):
        restore_from_trash(item.name, data)

    # Nothing landed outside DATA_FOLDER: the planted symlink is untouched
    # and the payload exists only as the still-trashed item.
    assert (outside / "photo.txt").is_symlink()
    assert sorted(p.name for p in outside.iterdir()) == ["photo.txt"]
    payload_hits = [
        p
        for p in tmp_path.rglob("*")
        if not p.is_symlink() and p.is_file() and p.read_bytes() == b"SECRET-PAYLOAD"
    ]
    assert payload_hits == [item]

    # The refused restore did not consume the trash item.
    assert item.exists()
    assert meta.exists()


def test_restore_refuses_directory_destination_whose_parent_escapes_data_folder(tmp_path):
    """A directory payload must not escape DATA_FOLDER through a symlinked parent (#615).

    Same attack shape as the file case, but the trashed payload is a
    directory, so the restore takes the directory branch (the fast rename,
    or the cross-device copytree/stage fallback). The resolved *parent* check
    must refuse the restore before any payload is moved out of the data
    folder, whatever the final component is.
    """
    data = tmp_path / "data"
    data.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (data / "inside.txt").write_bytes(b"INSIDE")

    td = trash.trash_dir(data)
    item = td / "deleted_dir"
    item.mkdir()
    payload = item / "payload.txt"
    payload.write_bytes(b"SECRET-DIR-PAYLOAD")
    meta = item.with_name(item.name + trash.META_SUFFIX)
    meta.write_text(json.dumps({"original": "albums/photo.txt"}))

    # The attack: an intermediate symlink leaving DATA_FOLDER whose target
    # points back inside, so the final component cancels the escape.
    (data / "albums").symlink_to(outside)
    (outside / "photo.txt").symlink_to(data / "inside.txt")

    # Precondition: the fully resolved destination is inside DATA_FOLDER,
    # i.e. the old final-component guard would have let this restore through.
    resolved_dest = (data / "albums" / "photo.txt").resolve()
    assert resolved_dest == (data / "inside.txt").resolve()
    assert str(resolved_dest).startswith(str(data.resolve()) + "/")

    with pytest.raises(ValueError, match="Destination parent path escapes data folder"):
        restore_from_trash(item.name, data)

    # Nothing landed outside DATA_FOLDER: the planted symlink is untouched
    # and the payload still only exists inside the trash payload directory.
    assert (outside / "photo.txt").is_symlink()
    assert sorted(p.name for p in outside.iterdir()) == ["photo.txt"]
    payload_hits = [
        p
        for p in tmp_path.rglob("*")
        if not p.is_symlink() and p.is_file() and p.read_bytes() == b"SECRET-DIR-PAYLOAD"
    ]
    assert payload_hits == [payload]
    assert all(hit.parent == item for hit in payload_hits)

    # No cross-device stage was created for the refused restore.
    assert list(data.rglob(trash.STAGE_PREFIX + "*")) == []

    # The refused restore did not consume the trash item.
    assert item.is_dir()
    assert meta.exists()


def test_restore_survives_parent_swapped_to_symlink_after_check(tmp_path, monkeypatch):
    """The restore rename must use the already-verified destination directory (#617).

    The parent check resolves ``dest.parent`` and compares it with the
    resolved data folder, but the rename resolves the path again: a
    directory inside DATA_FOLDER replaced by a symlink in the
    check-to-rename window used to carry the payload outside DATA_FOLDER —
    a swap the resolved-path checks cannot see because they ran before it.
    The restore now pins the verified parent in an open descriptor and
    renames through it, so the swap cannot redirect the payload.
    """
    data = tmp_path / "data"
    data.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    albums = data / "albums"
    albums.mkdir()

    td = trash.trash_dir(data)
    item = td / "deleted.txt"
    item.write_bytes(b"SECRET-PAYLOAD")
    meta = item.with_name(item.name + trash.META_SUFFIX)
    meta.write_text(json.dumps({"original": "albums/photo.txt"}))

    real_rename = trash._rename_onto_dest

    def swap_then_rename(src, dest, dest_fd):
        # The swap lands between the parent check and the rename: the
        # verified directory is replaced by a symlink out of the data
        # folder (possible because the directory is empty). The pinned
        # rename cannot follow the planted symlink — with the pinned
        # directory removed by the swap the rename fails, and the restore
        # fails closed instead of writing through the symlink.
        albums.rmdir()
        albums.symlink_to(outside)
        return real_rename(src, dest, dest_fd)

    monkeypatch.setattr(trash, "_rename_onto_dest", swap_then_rename)

    assert restore_from_trash(item.name, data) is False

    # The payload never left DATA_FOLDER: nothing landed behind the
    # planted symlink, and the trashed item and its metadata are intact
    # for a retry.
    assert sorted(p.name for p in outside.iterdir()) == []
    payload_hits = [
        p
        for p in tmp_path.rglob("*")
        if not p.is_symlink() and p.is_file() and p.read_bytes() == b"SECRET-PAYLOAD"
    ]
    assert payload_hits == [item]
    assert item.exists()
    assert meta.exists()


def test_restore_refuses_parent_swapped_to_symlink_before_open(tmp_path, monkeypatch):
    """A parent swapped between the check and the open must not be pinned (#617).

    The open of the verified destination parent can itself be raced: the
    swap lands after the resolved-path checks but before the open. The
    opened descriptor is therefore verified after the fact: the swap here
    replaces an *intermediate* component, so the raced open succeeds
    (the final component the attacker planted is a real directory that
    slips past ``O_NOFOLLOW``) and it is the descriptor's true path
    re-check against the data folder (``/proc/self/fd/<fd>`` on Linux)
    that rejects it. The restore fails closed either way.
    """
    data = tmp_path / "data"
    data.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a" / "b").mkdir(parents=True)
    swapped = data / "a"
    swapped.mkdir()
    (swapped / "b").mkdir()

    td = trash.trash_dir(data)
    item = td / "deleted.txt"
    item.write_bytes(b"SECRET-PAYLOAD")
    meta = item.with_name(item.name + trash.META_SUFFIX)
    meta.write_text(json.dumps({"original": "a/b/photo.txt"}))

    real_mkdir = trash.Path.mkdir

    def swap_then_mkdir(path, *args, **kwargs):
        result = real_mkdir(path, *args, **kwargs)
        if path == data / "a" / "b":
            # The swap lands after the resolved-path checks but before the
            # destination parent is opened: an intermediate directory is
            # replaced by a symlink out of the data folder, redirecting
            # the raced open to the attacker's directory.
            (swapped / "b").rmdir()
            swapped.rmdir()
            swapped.symlink_to(outside / "a")
        return result

    monkeypatch.setattr(trash.Path, "mkdir", swap_then_mkdir)

    assert restore_from_trash(item.name, data) is False

    # The payload never left DATA_FOLDER: the attacker's directory stays
    # empty, and the trashed item and its metadata are intact for a retry.
    assert sorted(p.name for p in (outside / "a" / "b").iterdir()) == []
    payload_hits = [
        p
        for p in tmp_path.rglob("*")
        if not p.is_symlink() and p.is_file() and p.read_bytes() == b"SECRET-PAYLOAD"
    ]
    assert payload_hits == [item]
    assert item.exists()
    assert meta.exists()


def test_restore_allows_data_folder_reached_through_symlinked_config_path(tmp_path):
    """A symlinked configured DATA_FOLDER path must not fail every restore (#617).

    Deployments commonly reach DATA_FOLDER through a symlinked configured
    path. ``O_NOFOLLOW`` would refuse that root at open time and fail
    every root-level restore closed, so the flag is skipped when the
    parent *is* the (resolved) data folder — the post-open re-check still
    pins the opened directory to the resolved data folder. The restore
    must succeed and the payload must land inside the real data folder.
    """
    real_data = tmp_path / "real_data"
    real_data.mkdir()
    data = tmp_path / "data"
    data.symlink_to(real_data)
    outside = tmp_path / "outside"
    outside.mkdir()

    td = trash.trash_dir(data)
    item = td / "deleted.txt"
    item.write_bytes(b"SECRET-PAYLOAD")
    meta = item.with_name(item.name + trash.META_SUFFIX)
    meta.write_text(json.dumps({"original": "photo.txt"}))

    # No O_NOFOLLOW at open for the data folder root itself: the raced-open
    # re-check (``/proc/self/fd`` resolving to the resolved data folder)
    # pins the opened directory instead, so a symlinked configured
    # DATA_FOLDER path restores normally.
    assert restore_from_trash(item.name, data) is True
    assert (real_data / "photo.txt").read_bytes() == b"SECRET-PAYLOAD"
    assert sorted(p.name for p in outside.iterdir()) == []
    assert not item.exists()
    assert not meta.exists()


def test_purge_old_trash_survives_item_vanishing_mid_sweep(tmp_path, monkeypatch):
    """A restore renaming an expired entry away mid-sweep skips it (#565).

    ``purge_old_trash`` stats each entry *before* acquiring the source
    lock, so a concurrent ``restore_from_trash`` fast-path rename landing
    between ``iterdir()`` and ``stat()`` used to raise FileNotFoundError
    out of the sweep. The interleaving is made deterministic: the victim's
    stat unlinks the entry first (exactly what the racing restore does to
    it), so the pre-lock probe sees the same FileNotFoundError the race
    produces. A second expired entry proves the sweep continues past the
    vanished one, and the victim's orphaned meta stays behind for
    ``empty_trash`` (the purge loop skips meta entries entirely).
    """
    data = tmp_path / "data"
    td = trash.trash_dir(data)
    old_iso = (trash._utcnow() - timedelta(days=40)).isoformat()

    victim = td / "victim.txt"
    victim.write_text("victim")
    victim_meta = victim.with_name(victim.name + trash.META_SUFFIX)
    victim_meta.write_text(json.dumps({"original": "victim.txt", "deleted_at": old_iso}))
    expired = td / "expired.txt"
    expired.write_text("expired")
    expired.with_name(expired.name + trash.META_SUFFIX).write_text(
        json.dumps({"original": "expired.txt", "deleted_at": old_iso})
    )

    original_stat = trash.Path.stat

    def racy_stat(path, *args, **kwargs):
        if path == victim:
            # The concurrent restore's rename wins the race against this
            # entry's stat: by the time the sweep examines it, it is gone.
            victim.unlink(missing_ok=True)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(trash.Path, "stat", racy_stat)
    assert purge_old_trash(data, retention_days=1) == 1

    assert not expired.exists()
    assert not victim.exists()
    assert victim_meta.exists()


def test_purge_old_trash_tolerates_failed_entry_mid_sweep(tmp_path, monkeypatch, caplog):
    """One raced non-ENOENT OSError no longer aborts the purge sweep (#597).

    The post-lock delete block only suppresses ENOENT, so a raced
    ``EACCES``-style OSError escaping the entry's ``unlink()`` used to
    bubble out of the sweep, 500 the route, and starve the remaining
    expired entries. The delete is now the same skip-and-continue policy
    as the pre-lock probes: the victim is debug-logged, left uncounted
    with its meta intact, and the sibling expired entry is still purged.
    """
    data = tmp_path / "data"
    td = trash.trash_dir(data)
    old_iso = (trash._utcnow() - timedelta(days=40)).isoformat()

    victim = td / "victim.txt"
    victim.write_text("victim")
    victim.with_name(victim.name + trash.META_SUFFIX).write_text(
        json.dumps({"original": "victim.txt", "deleted_at": old_iso})
    )
    expired = td / "expired.txt"
    expired.write_text("expired")
    expired.with_name(expired.name + trash.META_SUFFIX).write_text(
        json.dumps({"original": "expired.txt", "deleted_at": old_iso})
    )

    original_unlink = trash.Path.unlink

    def racy_unlink(path, *args, **kwargs):
        if path == victim:
            raise OSError(errno.EACCES, "permission denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(trash.Path, "unlink", racy_unlink)
    with caplog.at_level("DEBUG", logger="trash"):
        assert purge_old_trash(data, retention_days=1) == 1

    assert not expired.exists()
    assert victim.exists()
    assert victim.with_name(victim.name + trash.META_SUFFIX).exists()
    victim_skips = [
        r for r in caplog.records
        if "purge_old_trash: skipping trash entry" in r.message and "victim.txt" in r.getMessage()
    ]
    assert victim_skips, "the failed entry must be debug-logged, not raised"


def test_empty_trash_tolerates_failed_entry_mid_sweep(tmp_path, monkeypatch, caplog):
    """Same skip-and-continue delete policy as the purge sweep (#597).

    A raced non-ENOENT OSError from an entry's ``unlink()`` used to vanish
    silently into the loop's broad catch, skipping the rest of that
    entry's deletes (its meta) with no trace in the logs. The failed
    entry is now debug-logged, left uncounted with its meta intact, and
    the sweep still empties the sibling entries.
    """
    data = tmp_path / "data"
    td = trash.trash_dir(data)

    victim = td / "victim.txt"
    victim.write_text("victim")
    victim_meta = victim.with_name(victim.name + trash.META_SUFFIX)
    victim_meta.write_text(json.dumps({"original": "victim.txt"}))
    expired = td / "expired.txt"
    expired.write_text("expired")
    expired_meta = expired.with_name(expired.name + trash.META_SUFFIX)
    expired_meta.write_text(json.dumps({"original": "expired.txt"}))

    original_unlink = trash.Path.unlink

    def racy_unlink(path, *args, **kwargs):
        if path == victim:
            raise OSError(errno.EACCES, "permission denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(trash.Path, "unlink", racy_unlink)
    with caplog.at_level("DEBUG", logger="trash"):
        assert trash.empty_trash(data) == 2

    assert not expired.exists()
    assert not expired_meta.exists()
    assert victim.exists()
    assert victim_meta.exists()
    victim_skips = [
        r for r in caplog.records
        if "empty_trash: skipping trash entry" in r.message and "victim.txt" in r.getMessage()
    ]
    assert victim_skips, "the failed entry must be debug-logged, not raised"


def test_purge_old_trash_expires_dangling_symlink_entries(tmp_path):
    """A dangling-symlink trash entry ages via lstat and expires (#590).

    ``move_to_trash`` preserves symlinks, so a trash entry can itself be a
    link whose target is deleted later. The pre-lock age probe's ``stat()``
    follows the link and raises ENOENT, which used to mean the entry was
    skipped by every sweep forever. The probe must fall back to the link
    inode's own mtime (or the meta's ``deleted_at``) so retention
    eventually collects it, and the delete must unlink the link without
    following it or touching anything near its (missing) target.
    """
    import os

    data = tmp_path / "data"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sibling.txt").write_text("sibling")
    td = trash.trash_dir(data)
    old_iso = (trash._utcnow() - timedelta(days=40)).isoformat()
    fresh_iso = (trash._utcnow() - timedelta(hours=1)).isoformat()

    # Expired through the link inode's own mtime (no meta to consult).
    by_inode = td / "by-inode.txt"
    target = outside / "gone.txt"
    target.write_text("gone")
    by_inode.symlink_to(target)
    target.unlink()
    old_ts = time.time() - 40 * 86400
    os.utime(by_inode, (old_ts, old_ts), follow_symlinks=False)

    # Expired through the meta's deleted_at even though the link inode is fresh.
    by_meta = td / "by-meta.txt"
    by_meta.symlink_to(outside / "never-existed.txt")
    by_meta.with_name(by_meta.name + trash.META_SUFFIX).write_text(
        json.dumps({"deleted_at": old_iso})
    )

    # Meta present but unusable: the link inode's mtime still ages it.
    broken_meta = td / "broken-meta.txt"
    broken_meta.symlink_to(outside / "was-here.txt")
    broken_meta.with_name(broken_meta.name + trash.META_SUFFIX).write_text("not-json")
    os.utime(broken_meta, (old_ts, old_ts), follow_symlinks=False)

    # Fresh dangling link: the retention window must still protect it.
    kept = td / "kept.txt"
    kept.symlink_to(outside / "also-missing.txt")
    kept.with_name(kept.name + trash.META_SUFFIX).write_text(
        json.dumps({"deleted_at": fresh_iso})
    )

    for entry in (by_inode, by_meta, broken_meta, kept):
        assert os.path.islink(entry)

    assert purge_old_trash(data, retention_days=30) == 3
    assert not by_inode.exists() and not os.path.islink(by_inode)
    assert not by_meta.exists() and not os.path.islink(by_meta)
    assert not by_meta.with_name(by_meta.name + trash.META_SUFFIX).exists()
    assert not broken_meta.exists() and not os.path.islink(broken_meta)
    assert kept.is_symlink()
    assert (outside / "sibling.txt").read_text() == "sibling"
    assert not (outside / "gone.txt").exists()


def test_purge_old_trash_unlinks_symlink_to_live_dir_entry(tmp_path):
    """An expired symlink-to-live-directory entry is unlinked, target kept (#595).

    ``move_to_trash`` preserves symlinks, so a trash entry can be a link
    to a directory that still exists. The delete used to test
    ``is_dir()`` first: that follows the link, reports True, and
    ``rmtree(ignore_errors=True)`` swallows the OSError a symlink causes,
    so the sweep deleted the meta, counted the entry as purged, and left
    the link — and thus the entry — behind forever. The delete must
    unlink the link itself and never touch the live target.
    """
    import os

    data = tmp_path / "data"
    outside = tmp_path / "outside"
    target = outside / "live-dir"
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("keep")
    td = trash.trash_dir(data)
    old_iso = (trash._utcnow() - timedelta(days=40)).isoformat()

    link = td / "linked-dir"
    link.symlink_to(target)
    link_meta = link.with_name(link.name + trash.META_SUFFIX)
    link_meta.write_text(json.dumps({"deleted_at": old_iso}))

    assert purge_old_trash(data, retention_days=30) == 1
    assert not link.exists() and not os.path.islink(link)
    assert not link_meta.exists(), "no leftover meta after the purge"
    assert (target / "keep.txt").read_text() == "keep"
    assert target.is_dir()


def test_purge_old_trash_expires_symlink_loop_entries(tmp_path):
    """A symlink-loop trash entry ages via lstat and expires (#595).

    ``stat()`` on a symlink loop raises ELOOP, a bare ``OSError`` that is
    not FileNotFoundError, so the probe's dangling-link fallback never
    fired and the outer skip (#565) kept the loop alive on every sweep. A
    loop will not vanish on its own, so the probe must fall back to the
    link inode for ELOOP too, letting the meta's ``deleted_at`` (or the
    link's own mtime) age it into the delete path — where only the loop
    link itself may be unlinked.
    """
    import os

    data = tmp_path / "data"
    td = trash.trash_dir(data)
    old_iso = (trash._utcnow() - timedelta(days=40)).isoformat()

    loop = td / "loop"
    loop.symlink_to(loop)
    loop_meta = loop.with_name(loop.name + trash.META_SUFFIX)
    loop_meta.write_text(json.dumps({"deleted_at": old_iso}))

    assert purge_old_trash(data, retention_days=30) == 1
    assert not loop.exists() and not os.path.islink(loop)
    assert not loop_meta.exists()
    assert not list(td.iterdir()), "no leftover entries after the purge"


def test_purge_old_trash_ages_fresh_symlink_loop_until_retention(tmp_path):
    """A fresh symlink-loop entry survives a sweep and is collected when old (#595).

    Before the ELOOP fallback the loop entry was never aged — it hit the
    #565 skip on every sweep regardless of age, so freshness was
    indistinguishable from expiry. Now the probe reads the link inode via
    lstat(): with a fresh ``deleted_at`` in its meta the entry is inside
    the retention window and must survive, and once the meta's
    ``deleted_at`` ages past it, the very next sweep deletes the loop.
    """
    import os

    data = tmp_path / "data"
    td = trash.trash_dir(data)
    fresh_iso = (trash._utcnow() - timedelta(hours=1)).isoformat()
    old_iso = (trash._utcnow() - timedelta(days=40)).isoformat()

    loop = td / "loop"
    loop.symlink_to(loop)
    loop_meta = loop.with_name(loop.name + trash.META_SUFFIX)
    loop_meta.write_text(json.dumps({"deleted_at": fresh_iso}))

    assert purge_old_trash(data, retention_days=30) == 0
    assert os.path.islink(loop), "fresh loop must survive the sweep"

    loop_meta.write_text(json.dumps({"deleted_at": old_iso}))
    assert purge_old_trash(data, retention_days=30) == 1
    assert not os.path.islink(loop) and not loop_meta.exists()


def test_purge_old_trash_ages_symlink_loop_by_link_inode_without_meta(tmp_path):
    """A meta-less symlink-loop entry ages from the link's own mtime (#595).

    Without a meta the deleted_at falls back to the probe's ``st_mtime`` —
    for a loop that is the lstat-ed link inode (the #590 dangling-link
    fallback lane). ``os.utime(..., follow_symlinks=False)`` backdates the
    link inode itself; the entry must then expire on the next sweep.
    """
    import os

    data = tmp_path / "data"
    td = trash.trash_dir(data)

    loop = td / "loop"
    loop.symlink_to(loop)
    old_epoch = (trash._utcnow() - timedelta(days=40)).timestamp()
    os.utime(loop, (old_epoch, old_epoch), follow_symlinks=False)

    assert purge_old_trash(data, retention_days=30) == 1
    assert not os.path.islink(loop) and not list(td.iterdir())


def test_empty_trash_unlinks_symlink_to_live_dir_entry(tmp_path):
    """``empty_trash`` unlinks a symlink-to-live-dir entry, target kept (#595).

    The same ``is_dir()``/``rmtree`` shape as the purge delete path: for a
    link to a live directory, rmtree fails on the symlink, the meta still
    gets deleted, and the entry (item + meta) is miscounted while the
    link survives. The empty must unlink the link itself — never rmtree
    through it — and leave the target's contents intact.
    """
    import os

    data = tmp_path / "data"
    outside = tmp_path / "outside"
    target = outside / "live-dir"
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("keep")
    td = trash.trash_dir(data)

    link = td / "linked-dir"
    link.symlink_to(target)
    link_meta = link.with_name(link.name + trash.META_SUFFIX)
    link_meta.write_text(json.dumps({"deleted_at": (trash._utcnow() - timedelta(days=40)).isoformat()}))

    assert trash.empty_trash(data) == 2
    assert not link.exists() and not os.path.islink(link)
    assert not link_meta.exists()
    assert (target / "keep.txt").read_text() == "keep"
    assert target.is_dir()


def test_empty_trash_unlinks_symlink_loop_entry(tmp_path):
    """``empty_trash`` deletes a symlink-loop entry, item and meta (#595).

    A loop is True for ``is_symlink()`` but False for ``is_dir()`` (its
    ELOOP is swallowed), so the fixed delete unlinks it through the
    first branch. Under the historical unconditional delete this entry
    was already collected, so this guards the link-aware delete against
    regressing that behavior while item + meta still count 2.
    """
    import os

    data = tmp_path / "data"
    td = trash.trash_dir(data)

    loop = td / "loop"
    loop.symlink_to(loop)
    loop_meta = loop.with_name(loop.name + trash.META_SUFFIX)
    loop_meta.write_text(json.dumps({"deleted_at": (trash._utcnow() - timedelta(days=40)).isoformat()}))

    assert trash.empty_trash(data) == 2
    assert not os.path.islink(loop) and not loop_meta.exists()


class TestMoveToTrashSingleFile:
    """Verify single-file trash behavior (regression guard)."""

    def test_single_file_to_trash(self, monkeypatch, tmp_path):
        client, data_dir = build_client(monkeypatch, tmp_path)
        from trash import list_trash, move_to_trash

        item = data_dir / "cats" / "cat.jpg"
        assert item.exists() and item.is_file()

        result = move_to_trash(item, data_dir)
        assert result is True
        assert not item.exists()

        trash_items = list_trash(data_dir)
        assert len(trash_items) == 1
        assert trash_items[0]["original"] == "cats/cat.jpg"


class TestMoveToTrashDirectory:
    """Verify directory move to trash (the bug fix)."""

    def test_single_folder_to_trash(self, monkeypatch, tmp_path):
        from trash import list_trash, move_to_trash

        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = data_dir / "cats"
        assert folder.exists() and folder.is_dir()

        result = move_to_trash(folder, data_dir)
        assert result is True
        assert not folder.exists()

        trash_items = list_trash(data_dir)
        assert len(trash_items) == 1
        assert trash_items[0]["original"] == "cats"
        assert trash_items[0]["size"] > 0

    def test_nested_folder_to_trash(self, monkeypatch, tmp_path):
        from trash import list_trash, move_to_trash

        client, data_dir = build_client(monkeypatch, tmp_path)
        nested = data_dir / "photos" / "vacation" / "beach"
        nested.mkdir(parents=True)
        (nested / "photo1.jpg").write_bytes(b"\xff\xd8\xff\xe0fake_jpeg")
        (nested / "photo2.jpg").write_bytes(b"\xff\xd8\xff\xe0fake_jpeg2")

        result = move_to_trash(nested, data_dir)
        assert result is True
        assert not nested.exists()

        trash_items = list_trash(data_dir)
        assert len(trash_items) == 1
        # The original path should reflect the full relative path
        assert trash_items[0]["original"] == "photos/vacation/beach"

    def test_empty_folder_to_trash(self, monkeypatch, tmp_path):
        from trash import list_trash, move_to_trash

        client, data_dir = build_client(monkeypatch, tmp_path)
        empty_folder = data_dir / "empty_dir"
        empty_folder.mkdir()

        result = move_to_trash(empty_folder, data_dir)
        assert result is True
        assert not empty_folder.exists()

        trash_items = list_trash(data_dir)
        assert len(trash_items) == 1
        assert trash_items[0]["size"] == 0

    def test_nonexistent_path_returns_false(self, monkeypatch, tmp_path):
        from trash import move_to_trash

        client, data_dir = build_client(monkeypatch, tmp_path)
        fake = data_dir / "does_not_exist"

        result = move_to_trash(fake, data_dir)
        assert result is False


class TestBulkDelete:
    """Verify bulk delete handles both files and folders correctly."""

    def test_bulk_delete_mixed_files_and_folders(self, monkeypatch, tmp_path):
        from trash import list_trash, move_to_trash

        client, data_dir = build_client(monkeypatch, tmp_path)
        (data_dir / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0fake_jpeg")
        folder1 = data_dir / "folder_a"
        folder1.mkdir()
        (folder1 / "file_in_a.jpg").write_bytes(b"\xff\xd8\xff\xe0fake_jpeg")
        folder2 = data_dir / "folder_b"
        folder2.mkdir()
        (folder2 / "file_in_b.jpg").write_bytes(b"\xff\xd8\xff\xe0fake_jpeg2")

        for rel in ["photo.jpg", "folder_a", "folder_b"]:
            path = data_dir / rel
            if path.is_file() or path.is_dir():
                move_to_trash(path, data_dir)

        trash_items = list_trash(data_dir)
        assert len(trash_items) == 3
        originals = {t["original"] for t in trash_items}
        assert "photo.jpg" in originals
        assert "folder_a" in originals
        assert "folder_b" in originals

    def test_bulk_delete_folders_via_web_form(self, monkeypatch, tmp_path):
        """Simulate the actual bulk-delete form submission with folder selection."""
        client, data_dir = build_client(monkeypatch, tmp_path)

        folder = data_dir / "test_folder"
        folder.mkdir()
        (folder / "img1.jpg").write_bytes(b"\xff\xd8\xff\xe0fake")
        (folder / "img2.jpg").write_bytes(b"\xff\xd8\xff\xe0fake2")

        (data_dir / "standalone.jpg").write_bytes(b"\xff\xd8\xff\xe0standalone")

        # Login to get session cookie
        login_resp = client.get("/login")
        csrf_match = re.search(
            r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']',
            login_resp.data.decode(),
        )
        assert csrf_match, "CSRF token not found in login form"
        login_csrf = csrf_match.group(1)

        auth_resp = client.post(
            "/auth",
            data={"password": "pass123", "next": "/", "csrf_token": login_csrf},
            follow_redirects=False,
        )
        assert auth_resp.status_code == 302

        # Now get the main page to extract CSRF for bulk-delete form
        main_resp = client.get("/")
        main_html = main_resp.data.decode()
        csrf_match2 = re.search(
            r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']',
            main_html,
        )
        assert csrf_match2, "CSRF token not found in main page"
        bulk_csrf = csrf_match2.group(1)

        resp = client.post(
            "/bulk-delete",
            data={
                "csrf_token": bulk_csrf,
                "current_subpath": "",
                "filenames": ["standalone.jpg"],
                "folders": ["test_folder"],
            },
            follow_redirects=True,
        )

        assert resp.status_code == 200
        body = resp.data.decode()
        # Should show success feedback (moved_files=1 and moved_folders=1)
        assert "success" in body.lower() or "moved" in body.lower()

    def test_bulk_delete_no_selection_is_nop(self, monkeypatch, tmp_path):
        """Bulk delete with no files/folders selected should be a noop."""
        client, data_dir = build_client(monkeypatch, tmp_path)

        # Login first
        login_resp = client.get("/login")
        csrf_match = re.search(
            r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']',
            login_resp.data.decode(),
        )
        assert csrf_match
        _ = client.post(
            "/auth",
            data={"password": "pass123", "next": "/", "csrf_token": csrf_match.group(1)},
            follow_redirects=False,
        )

        main_resp = client.get("/")
        csrf_match2 = re.search(
            r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']',
            main_resp.data.decode(),
        )
        assert csrf_match2

        resp = client.post(
            "/bulk-delete",
            data={
                "csrf_token": csrf_match2.group(1),
                "current_subpath": "",
                "filenames": [],
                "folders": [],
            },
            follow_redirects=True,
        )

        assert resp.status_code == 200
        body = resp.data.decode()
        # Should indicate noop (no items moved)
        assert "no selected items" in body.lower()


class TestTrashRestore:
    """Verify restore from trash works for both files and directories."""

    def test_restore_file_from_trash(self, monkeypatch, tmp_path):
        from trash import list_trash, move_to_trash, restore_from_trash

        client, data_dir = build_client(monkeypatch, tmp_path)
        item = data_dir / "sample.png"
        original_content = item.read_bytes()

        move_to_trash(item, data_dir)
        assert not item.exists()

        trash_items = list_trash(data_dir)
        trash_entry = trash_items[0]["name"]

        result = restore_from_trash(trash_entry, data_dir)
        assert result is True
        assert item.exists()
        assert item.read_bytes() == original_content

    def test_restore_folder_from_trash(self, monkeypatch, tmp_path):
        from trash import list_trash, move_to_trash, restore_from_trash

        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = data_dir / "restore_test"
        folder.mkdir()
        (folder / "file1.jpg").write_bytes(b"\xff\xd8\xff\xe0content1")
        nested = folder / "subdir"
        nested.mkdir()
        (nested / "file2.jpg").write_bytes(b"\xff\xd8\xff\xe0content2")

        content_before = {
            "file1.jpg": (folder / "file1.jpg").read_bytes(),
            "subdir/file2.jpg": (nested / "file2.jpg").read_bytes(),
        }

        move_to_trash(folder, data_dir)
        assert not folder.exists()

        trash_items = list_trash(data_dir)
        trash_entry = trash_items[0]["name"]

        result = restore_from_trash(trash_entry, data_dir)
        assert result is True
        assert folder.exists()
        assert (folder / "file1.jpg").read_bytes() == content_before["file1.jpg"]
        assert (nested / "file2.jpg").read_bytes() == content_before["subdir/file2.jpg"]


class TestRestoreConsumesTrashedDirectory:
    """Regression tests for orphaned directory copies after restore (#516).

    A successful directory restore must consume the trash source and its
    metadata, matching file-restore semantics; a failed restore must keep
    both recoverable.
    """

    def _make_folder(self, data_dir):
        folder = data_dir / "album"
        folder.mkdir()
        (folder / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0a")
        (folder / "sub").mkdir()
        (folder / "sub" / "b.jpg").write_bytes(b"\xff\xd8\xff\xe0b")
        return folder

    def test_restore_directory_removes_trash_source_and_metadata(self, monkeypatch, tmp_path):
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_folder(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item = trash.trash_dir(data_dir) / entry
        meta_file = trash.trash_dir(data_dir) / (entry + trash.META_SUFFIX)
        assert trash_item.is_dir() and meta_file.exists()

        assert restore_from_trash(entry, data_dir) is True
        assert (folder / "a.jpg").read_bytes() == b"\xff\xd8\xff\xe0a"
        assert (folder / "sub" / "b.jpg").read_bytes() == b"\xff\xd8\xff\xe0b"
        assert not trash_item.exists()
        assert not meta_file.exists()
        assert list_trash(data_dir) == []

    def test_restore_directory_collision_renames_and_consumes_trash_entry(self, monkeypatch, tmp_path):
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_folder(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]

        # Recreate a colliding destination holding different bytes.
        folder.mkdir()
        (folder / "new.txt").write_text("new")

        assert restore_from_trash(entry, data_dir) is True
        assert (folder / "new.txt").read_text() == "new"
        renamed = [p for p in data_dir.iterdir() if p.is_dir() and p.name.startswith("album_")]
        assert len(renamed) == 1
        assert (renamed[0] / "a.jpg").read_bytes() == b"\xff\xd8\xff\xe0a"
        assert (renamed[0] / "sub" / "b.jpg").read_bytes() == b"\xff\xd8\xff\xe0b"
        assert list_trash(data_dir) == []

    def test_repeated_trash_restore_cycles_do_not_accumulate_entries(self, monkeypatch, tmp_path):
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_folder(data_dir)

        for _ in range(3):
            assert move_to_trash(folder, data_dir) is True
            entries = list_trash(data_dir)
            assert len(entries) == 1
            assert restore_from_trash(entries[0]["name"], data_dir) is True
            assert list_trash(data_dir) == []
            assert (folder / "a.jpg").read_bytes() == b"\xff\xd8\xff\xe0a"

        unknown = [t for t in list_trash(data_dir) if t["original"] == "unknown"]
        assert unknown == []

    def test_cross_device_directory_restore_fallback_consumes_source(self, monkeypatch, tmp_path):
        """The copytree fallback in restore must consume the source like rename.

        The fallback now stages the copy on the *destination* filesystem
        (under ``dest.parent``); if it staged on the source filesystem
        (under ``td``), the staged ``stage.rename(dest)`` would cross the
        same boundary that just failed the original ``item.rename(dest)``
        and the fallback could never complete. This test fails on the
        old staging-on-source implementation: the staged rename is mocked
        to fail too, so the old code returns False while the new code
        successfully completes the staged rename and the source-metadata
        consume.
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_folder(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item = trash.trash_dir(data_dir) / entry
        meta_file = trash.trash_dir(data_dir) / (entry + trash.META_SUFFIX)

        real_rename = trash.Path.rename
        trash_root_resolved = trash.trash_dir(data_dir).resolve()

        def cross_device_rename(src, dest, dest_fd):
            # Simulate a true cross-filesystem restore: any rename whose
            # source lives under the trash filesystem fails with EXDEV,
            # including the staged rename if the stage is (incorrectly)
            # placed back under td. Renames whose source is under the
            # destination filesystem — which is what the new staging
            # produces — succeed.
            try:
                resolved = src.resolve()
            except OSError:
                return real_rename(src, dest)
            if trash_root_resolved in resolved.parents or resolved == trash_root_resolved:
                raise OSError("cross-device")
            return real_rename(src, dest)

        monkeypatch.setattr(trash, "_rename_onto_dest", cross_device_rename)

        assert restore_from_trash(entry, data_dir) is True
        assert (folder / "a.jpg").read_bytes() == b"\xff\xd8\xff\xe0a"
        assert (folder / "sub" / "b.jpg").read_bytes() == b"\xff\xd8\xff\xe0b"
        assert not trash_item.exists()
        assert not meta_file.exists()
        assert list_trash(data_dir) == []

    def test_failed_directory_restore_keeps_source_and_metadata(self, monkeypatch, tmp_path):
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_folder(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item = trash.trash_dir(data_dir) / entry
        meta_file = trash.trash_dir(data_dir) / (entry + trash.META_SUFFIX)

        real_rename = trash.Path.rename

        def cross_device_rename(src, dest, dest_fd):
            if src == trash_item:
                raise OSError("cross-device")
            return real_rename(src, dest)

        monkeypatch.setattr(trash, "_rename_onto_dest", cross_device_rename)

        def failing_copytree(src, dest, *args, **kwargs):
            # Leave a partial copy behind, like a real interrupted copytree.
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "partial.jpg").write_bytes(b"partial")
            raise OSError("disk full")

        monkeypatch.setattr(trash.shutil, "copytree", failing_copytree)

        assert restore_from_trash(entry, data_dir) is False
        assert trash_item.is_dir()
        assert (trash_item / "a.jpg").read_bytes() == b"\xff\xd8\xff\xe0a"
        assert meta_file.exists()
        assert not folder.exists()
        # The partial copy must not linger at the destination.
        entries = list_trash(data_dir)
        assert len(entries) == 1
        assert entries[0]["original"] != "unknown"
        # No staging directory left behind after the failed restore. The
        # stage lives on the destination filesystem (under dest.parent),
        # not under the trash root, so scan the whole data folder.
        stages = [p for p in data_dir.rglob(".restore-stage-*") if p.is_dir()]
        assert stages == [], f"stage directory leaked: {stages}"

    def test_concurrent_restore_does_not_delete_winners_destination(
        self, monkeypatch, tmp_path
    ):
        """Regression for review blocker on #516: when two restores of the
        same entry race, the loser's fallback cleanup must not delete the
        winner's restored directory.

        The cross-device fallback now stages the copy on the destination
        filesystem (under ``dest.parent``) and only ever rmtree's that
        privately-owned stage — never the function's computed dest.
        Under the previous code, an unconditional
        ``shutil.rmtree(dest)`` in the copytree failure handler would
        delete any directory that happened to occupy ``dest`` (including
        another concurrent restore's restored media).
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_folder(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item = trash.trash_dir(data_dir) / entry
        meta_file = trash.trash_dir(data_dir) / (entry + trash.META_SUFFIX)

        # Track every rmtree call inside the trash module so we can
        # prove no cleanup ever targets the function's computed dest.
        rmtree_targets: list[str] = []
        real_rmtree = trash.shutil.rmtree

        def tracking_rmtree(path, *args, **kwargs):
            rmtree_targets.append(str(path))
            return real_rmtree(path, *args, **kwargs)

        # Force the rename branch to fail (cross-device or already-moved
        # source) so the fallback runs.
        real_rename = trash.Path.rename

        def failing_rename(src, dest, dest_fd):
            if src == trash_item:
                raise OSError("source no longer present")
            return real_rename(src, dest)

        # Force copytree to fail with FileExistsError, simulating the
        # race where another restore has already populated the computed
        # destination path.
        def racing_copytree(src, dest, *args, **kwargs):
            raise FileExistsError(f"dest {dest} already exists")

        monkeypatch.setattr(trash.shutil, "rmtree", tracking_rmtree)
        monkeypatch.setattr(trash, "_rename_onto_dest", failing_rename)
        monkeypatch.setattr(trash.shutil, "copytree", racing_copytree)

        assert restore_from_trash(entry, data_dir) is False

        # The trash source and metadata must remain for a retry.
        assert trash_item.is_dir()
        assert meta_file.exists()
        # No .restore-stage-* directory leaks across calls. The stage lives
        # on the destination filesystem (under dest.parent), not under the
        # trash root, so scan the whole data folder.
        stages = [p for p in data_dir.rglob(".restore-stage-*") if p.is_dir()]
        assert stages == [], f"stage directory leaked: {stages}"

        # The critical guarantee: no rmtree call ever targeted the
        # destination ``dest``. Under the old code, the failure path
        # would have rmtree'd the function's computed dest, clobbering
        # a concurrent winner's restored media. The stage lives on the
        # destination filesystem (not under the trash root), so the
        # invariant can no longer be "every rmtree target is inside the
        # trash root"; it must be the more targeted
        # "rmtree never targets a path owned by another caller", i.e.
        # never the function's computed dest.
        dest_resolved = folder.resolve()
        for target in rmtree_targets:
            try:
                resolved = Path(target).resolve()
            except OSError:
                continue
            assert resolved != dest_resolved, (
                f"rmtree targeted the destination {target}; this is the "
                f"data-loss race the staging fix prevents."
            )

    def test_concurrent_restore_does_not_delete_winners_destination_end_to_end(
        self, monkeypatch, tmp_path
    ):
        """End-to-end regression for the concurrent restore race.

        Simulates two restores of the same entry interleaved: Request A
        has already populated the destination (``album``), and Request
        B observes the source still in trash (passes the exists check)
        but its rename fails (source has been moved concurrently) and
        falls into the copytree fallback. B's fallback cleanup must not
        delete A's restored directory.
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_folder(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item = trash.trash_dir(data_dir) / entry
        meta_file = trash.trash_dir(data_dir) / (entry + trash.META_SUFFIX)

        # Pre-populate the destination as if Request A had already
        # completed its restore. This is the "rival" the loser's
        # cleanup must not delete.
        rival_dest = data_dir / "album"
        rival_dest.mkdir()
        (rival_dest / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0a")
        (rival_dest / "sub").mkdir()
        (rival_dest / "sub" / "b.jpg").write_bytes(b"\xff\xd8\xff\xe0b")
        rival_inode = (rival_dest / "a.jpg").stat().st_ino

        # Force the function's dest-uniqueness check to ignore the
        # pre-existing rival so the function computes dest = album
        # (not album_<ts>). The OLD code's rmtree(dest) on a populated
        # album is what wipes the rival; we want to reproduce that
        # exact failure mode.
        real_exists = trash.Path.exists

        def exists_ignores_rival(self_path):
            if str(self_path) == str(rival_dest):
                return False
            return real_exists(self_path)

        # Force rename to fail for the trash source (B observes
        # "source gone" because A moved it).
        real_rename = trash.Path.rename

        def failing_rename(src, dest, dest_fd):
            if src == trash_item:
                raise OSError("source no longer present")
            return real_rename(src, dest)

        # Force copytree to fail because dest is occupied by A.
        def racing_copytree(src, dest, *args, **kwargs):
            raise FileExistsError(f"dest {dest} already exists")

        monkeypatch.setattr(trash.Path, "exists", exists_ignores_rival)
        monkeypatch.setattr(trash, "_rename_onto_dest", failing_rename)
        monkeypatch.setattr(trash.shutil, "copytree", racing_copytree)

        # Request B runs: rename fails, fallback copytree fails,
        # cleanup runs. B must return False without clobbering the
        # rival's restored directory.
        assert restore_from_trash(entry, data_dir) is False

        # A's restored media is intact (same inode).
        assert rival_dest.is_dir()
        rival_a = rival_dest / "a.jpg"
        assert rival_a.read_bytes() == b"\xff\xd8\xff\xe0a"
        assert rival_a.stat().st_ino == rival_inode
        assert (rival_dest / "sub" / "b.jpg").read_bytes() == b"\xff\xd8\xff\xe0b"
        # Trash source preserved (B's rename failed, so the trash entry
        # remains for a retry).
        assert trash_item.is_dir()
        assert meta_file.exists()
        # No leftover staging directory. The stage lives on the
        # destination filesystem (under dest.parent), not under the
        # trash root, so scan the whole data folder.
        stages = [p for p in data_dir.rglob(".restore-stage-*") if p.is_dir()]
        assert stages == [], f"stage directory leaked: {stages}"

class TestConcurrentRestoreDestinationAllocation:
    """Regression tests for issue #608: a restore must reserve its destination.

    ``restore_from_trash`` used to probe ``dest.exists()`` and only then
    ``rename`` onto it. POSIX ``rename`` overwrites, so two restores of
    *different* trash items whose metadata share one ``original`` path (the
    same gallery path deleted twice over time) could both pass the probe, and
    the later ``rename`` silently discarded the earlier payload while both
    calls reported success. The per-item source flock does not cover this: it
    serializes restores of one trash item, not two items pointing at one
    destination.

    Destinations are now reserved with an exclusive ``flock`` on a per-path
    claim file inside the trash directory (see ``_reserve_restore_dest``). The
    race is reproduced deterministically in a single process by pausing the
    first restore inside its ``rename`` and running the rival restore in that
    window — the exact window the old check-then-rename order lost data in.
    ``flock`` is taken per open file description, so a second claim conflicts
    inside one process just as it would between Gunicorn workers.
    """

    def _meta(self, item, original):
        item.with_name(item.name + trash.META_SUFFIX).write_text(
            json.dumps({"original": original, "deleted_at": "2026-01-01T00:00:00+00:00"})
        )
        return item

    def _file_item(self, td, name, original, payload):
        item = td / name
        item.write_bytes(payload)
        return self._meta(item, original)

    def _dir_item(self, td, name, original, filename, payload):
        item = td / name
        (item / "sub").mkdir(parents=True)
        (item / filename).write_bytes(payload)
        return self._meta(item, original)

    def test_concurrent_file_restores_retain_both_payloads(self, monkeypatch, tmp_path):
        """The payload-loss case: each restore must end up with its own file."""
        data = tmp_path / "data"
        td = trash.trash_dir(data)
        (data / "albums").mkdir(parents=True)
        first = self._file_item(td, "a.bin", "albums/photo.txt", b"first")
        self._file_item(td, "b.bin", "albums/photo.txt", b"second")
        base = data / "albums/photo.txt"

        real_rename = trash._rename_onto_dest
        rival_results: list[bool] = []

        def interleaved_rename(src, dest, dest_fd):
            if src == first and not rival_results:
                # The rival restore runs after the first restore picked
                # ``base`` and before its rename lands: with the old
                # probe-then-rename order both saw a free destination.
                rival_results.append(restore_from_trash("b.bin", data))
            return real_rename(src, dest, dest_fd)

        monkeypatch.setattr(trash, "_rename_onto_dest", interleaved_rename)

        assert restore_from_trash("a.bin", data) is True
        assert rival_results == [True]
        # Both payloads survive: the winner holds the name, the rival took a
        # timestamped one instead of overwriting through it.
        assert base.read_bytes() == b"first"
        rival = list((data / "albums").glob("photo_*.txt"))
        assert [p.read_bytes() for p in rival] == [b"second"]
        assert list_trash(data) == []

    def test_concurrent_directory_restores_retain_both_trees(self, monkeypatch, tmp_path):
        """The same race on the directory branch keeps both trees."""
        data = tmp_path / "data"
        td = trash.trash_dir(data)
        first = self._dir_item(td, "dir_a", "album", "a.txt", b"A")
        self._dir_item(td, "dir_b", "album", "b.txt", b"B")

        real_rename = trash._rename_onto_dest
        rival_results: list[bool] = []

        def interleaved_rename(src, dest, dest_fd):
            if src == first and not rival_results:
                rival_results.append(restore_from_trash("dir_b", data))
            return real_rename(src, dest, dest_fd)

        monkeypatch.setattr(trash, "_rename_onto_dest", interleaved_rename)

        assert restore_from_trash("dir_a", data) is True
        assert rival_results == [True]
        assert (data / "album" / "a.txt").read_bytes() == b"A"
        renamed = [p for p in data.iterdir() if p.is_dir() and p.name.startswith("album_")]
        assert len(renamed) == 1
        assert (renamed[0] / "b.txt").read_bytes() == b"B"
        assert list_trash(data) == []
        stages = [p for p in data.rglob(f"{trash.STAGE_PREFIX}*")]
        assert stages == [], f"stage directory leaked: {stages}"

    def test_restore_replaces_a_symlink_at_the_destination(self, tmp_path):
        """A symlink sitting at the destination is replaced, never followed."""
        data = tmp_path / "data"
        td = trash.trash_dir(data)
        albums = data / "albums"
        albums.mkdir(parents=True)
        item = self._file_item(td, "a.bin", "albums/photo.txt", b"payload")
        victim = albums / "victim.txt"
        link = albums / "photo.txt"
        link.symlink_to(victim)

        assert restore_from_trash(item.name, data) is True
        assert not link.is_symlink()
        assert link.read_bytes() == b"payload"
        assert not victim.exists(), "the restore must not write through the planted link"

    def test_restore_replaces_a_symlink_pointing_at_an_existing_file(self, tmp_path):
        """A symlink to a live file is replaced too, and its target survives.

        ``Path.exists()`` follows a symlink, so probing the destination with
        it made a link to an existing file look occupied: the restore took a
        timestamped name and left the link in place. The rename targets the
        unresolved path, so the link itself is replaced — the file it pointed
        at keeps its own bytes and is never written through.
        """
        data = tmp_path / "data"
        td = trash.trash_dir(data)
        albums = data / "albums"
        albums.mkdir(parents=True)
        item = self._file_item(td, "a.bin", "albums/photo.txt", b"payload")
        victim = albums / "victim.txt"
        victim.write_bytes(b"victim")
        link = albums / "photo.txt"
        link.symlink_to(victim)

        assert restore_from_trash(item.name, data) is True
        assert not link.is_symlink()
        assert link.read_bytes() == b"payload"
        assert victim.read_bytes() == b"victim", "the restore must not write through the link"
        assert list_trash(data) == []

    def test_directory_restore_does_not_replace_a_symlink_at_the_destination(self, tmp_path):
        """A directory payload cannot take a symlink's name, so it suffixes.

        ``rename(2)`` refuses a directory onto a symlink (``ENOTDIR``), so the
        restore must not pick that name: the planted link stays and the tree
        lands on a timestamped name instead.
        """
        data = tmp_path / "data"
        td = trash.trash_dir(data)
        albums = data / "albums"
        albums.mkdir(parents=True)
        item = self._dir_item(td, "dir_a", "albums/folder", "a.txt", b"A")
        victim = albums / "victim.txt"
        victim.write_bytes(b"victim")
        link = albums / "folder"
        link.symlink_to(victim)

        assert restore_from_trash(item.name, data) is True
        assert link.is_symlink(), "a directory payload must not take a symlink's name"
        assert victim.read_bytes() == b"victim"
        restored = list(albums.glob("folder_*"))
        assert len(restored) == 1
        assert (restored[0] / "a.txt").read_bytes() == b"A"
        assert list_trash(data) == []

    def test_directory_restore_succeeds_onto_a_dangling_symlink_name(self, tmp_path):
        """A dangling symlink at the destination must not fail the restore.

        ``rename(2)`` refuses a directory onto a symlink (``ENOTDIR``) whatever
        the symlink points at, dangling included, so the restore has to pick
        another name: the tree lands on a timestamped one and the restore
        reports success instead of leaving the item in the trash.
        """
        data = tmp_path / "data"
        td = trash.trash_dir(data)
        albums = data / "albums"
        albums.mkdir(parents=True)
        item = self._dir_item(td, "dir_a", "albums/folder", "a.txt", b"A")
        link = albums / "folder"
        link.symlink_to(albums / "missing.txt")

        assert restore_from_trash(item.name, data) is True
        assert link.is_symlink(), "a directory payload must not take a symlink's name"
        restored = list(albums.glob("folder_*"))
        assert len(restored) == 1
        assert (restored[0] / "a.txt").read_bytes() == b"A"
        assert list_trash(data) == []

    def test_concurrent_restores_via_a_symlinked_album_dir_retain_both_payloads(
        self, monkeypatch, tmp_path
    ):
        """Two ``original`` paths reaching one directory share one reservation.

        ``albums`` and its alias can name the same directory, so the claim key
        has to be the directory itself: keyed on the path as written, the two
        restores reserve different files and overwrite each other again.
        """
        data = tmp_path / "data"
        td = trash.trash_dir(data)
        (data / "albums").mkdir(parents=True)
        (data / "alias").symlink_to(data / "albums")
        first = self._file_item(td, "a.bin", "alias/photo.png", b"A")
        self._file_item(td, "b.bin", "albums/photo.png", b"B")

        real_rename = trash._rename_onto_dest
        rival_results: list[bool] = []

        def interleaved_rename(src, dest, dest_fd):
            if src == first and not rival_results:
                rival_results.append(restore_from_trash("b.bin", data))
            return real_rename(src, dest, dest_fd)

        monkeypatch.setattr(trash, "_rename_onto_dest", interleaved_rename)

        assert restore_from_trash("a.bin", data) is True
        assert rival_results == [True]
        assert sorted(p.read_bytes() for p in (data / "albums").glob("photo*")) == [b"A", b"B"]
        assert list_trash(data) == []

    def test_claim_files_are_bookkeeping_for_the_trash_sweeps(self, monkeypatch, tmp_path):
        """Claim files are not trash items, and only free+aged ones retire."""
        import fcntl
        import os

        data = tmp_path / "data"
        td = trash.trash_dir(data)
        (data / "albums").mkdir(parents=True)
        item = self._file_item(td, "a.bin", "albums/photo.txt", b"payload")
        dest = data / "albums/photo.txt"
        dest.write_bytes(b"occupied")

        assert restore_from_trash(item.name, data) is True
        claims = [p for p in td.iterdir() if trash._is_restore_claim_name(p.name)]
        assert claims, "the reservation must leave a claim file in the trash dir"
        claim = trash._restore_claim_path(td, data.resolve() / "albums/photo.txt")
        assert claim in claims

        # Neither listed nor deleted, and not counted as deleted trash.
        assert list_trash(data) == []
        assert trash.empty_trash(data) == 0
        assert purge_old_trash(data, retention_days=30) == 0
        assert claim.exists()

        # A live holder protects the claim even with the grace switched off:
        # freeing it would let the next reservation create a second inode for
        # the same destination, and two inodes means no mutual exclusion.
        fh = os.open(claim, os.O_RDWR)
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            monkeypatch.setattr(trash, "STAGE_ABANDON_GRACE_SECONDS", -1)
            trash.empty_trash(data)
            assert claim.exists(), "maintenance must not free a claim a restore holds"
        finally:
            os.close(fh)

        # Free and past the grace: retired with the rest of the debris.
        trash.empty_trash(data)
        assert not claim.exists()
        assert list_trash(data) == []

    def test_restore_without_fcntl_fails_closed(self, monkeypatch, tmp_path):
        """Without flock support the restore refuses instead of racing."""
        data = tmp_path / "data"
        td = trash.trash_dir(data)
        (data / "albums").mkdir(parents=True)
        item = self._file_item(td, "a.bin", "albums/photo.txt", b"payload")
        dest = data / "albums/photo.txt"
        dest.write_bytes(b"occupied")

        monkeypatch.setattr(trash, "fcntl", None)

        # Without the claim the restore cannot tell an owned destination from
        # a free one, and POSIX rename overwrites: renaming anyway would
        # restore the check-then-rename race the claim closes (issue #608).
        with pytest.raises(trash.RestoreDestinationUnavailable):
            restore_from_trash(item.name, data)
        assert dest.read_bytes() == b"occupied"
        assert [t["name"] for t in list_trash(data)] == ["a.bin"]
        assert [p for p in td.iterdir() if trash._is_restore_claim_name(p.name)] == []

    def test_unreservable_destination_never_overwrites_an_existing_file(self, monkeypatch, tmp_path):
        """A destination that cannot be reserved is never renamed onto.

        The check-then-rename order this issue removed lost a payload when two
        restores shared one ``original`` path. The lockless fallback kept
        that window open: a restore that cannot lock the claim probed the
        destination and renamed onto it anyway, so a second restore could
        overwrite the first payload. The restore must fail closed instead.
        """
        data = tmp_path / "data"
        td = trash.trash_dir(data)
        (data / "albums").mkdir(parents=True)
        item = self._file_item(td, "a.bin", "albums/photo.txt", b"payload")
        dest = data / "albums/photo.txt"
        dest.write_bytes(b"occupied")

        def unsupported_flock(*args, **kwargs):
            raise OSError(errno.ENOLCK, "flock unsupported")

        monkeypatch.setattr(trash.fcntl, "flock", unsupported_flock)

        with pytest.raises(trash.RestoreDestinationUnavailable):
            restore_from_trash(item.name, data)
        # The existing file keeps its bytes: nothing was renamed onto it, and
        # the payload is still in the trash for a retry.
        assert dest.read_bytes() == b"occupied"
        assert [t["name"] for t in list_trash(data)] == ["a.bin"]

    def test_failed_restore_releases_the_source_lock(self, monkeypatch, tmp_path):
        """A failed restore must not leave the trash item locked.

        The source lock lives on the item's metadata file. If a failed restore
        kept it, the next restore of the same item would fail with
        ``SOURCE_LOCK_HELD`` even though nothing else holds the item, and the
        item would sit in the trash forever.
        """
        data = tmp_path / "data"
        td = trash.trash_dir(data)
        (data / "albums").mkdir(parents=True)
        item = self._file_item(td, "a.bin", "albums/photo.txt", b"payload")

        def no_locking(*args, **kwargs):
            return None, trash.RESTORE_CLAIM_UNSUPPORTED, "the reservation is unavailable"

        monkeypatch.setattr(trash, "_acquire_restore_claim", no_locking)
        with pytest.raises(trash.RestoreDestinationUnavailable):
            restore_from_trash(item.name, data)
        assert [t["name"] for t in list_trash(data)] == ["a.bin"]

        # The source lock is gone: the same item restores once the
        # reservation works again, and it keeps its bytes.
        monkeypatch.undo()
        assert restore_from_trash(item.name, data) is True
        assert (data / "albums/photo.txt").read_bytes() == b"payload"

    def test_symlinked_claim_fails_the_restore_closed(self, monkeypatch, tmp_path):
        """A claim path planted as a symlink is never opened through."""
        import fcntl
        import os

        data = tmp_path / "data"
        td = trash.trash_dir(data)
        (data / "albums").mkdir(parents=True)
        item = self._file_item(td, "a.bin", "albums/photo.txt", b"payload")
        victim = data / "victim.txt"
        victim.write_text("victim")
        claim = trash._restore_claim_path(td, data.resolve() / "albums/photo.txt")
        claim.symlink_to(victim)

        victim_lockable: list[bool] = []
        real_rename = trash._rename_onto_dest

        def check_victim(src, dest, dest_fd):
            if src == item:
                fd = os.open(victim, os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    victim_lockable.append(False)
                else:
                    victim_lockable.append(True)
                finally:
                    os.close(fd)
            return real_rename(src, dest, dest_fd)

        monkeypatch.setattr(trash, "_rename_onto_dest", check_victim)

        # O_NOFOLLOW refuses to open the planted link, so the reservation for
        # this candidate name is unusable and the restore walks on to the
        # next candidate name. The planted link is never opened through, so
        # the reservation never becomes an flock on whatever it points at:
        # the victim is still free to lock at rename time.
        assert restore_from_trash(item.name, data) is True
        assert victim_lockable == [True]
        assert victim.read_text() == "victim"
        assert claim.is_symlink()
        # The payload landed on a suffixed name, not on the reserved one.
        restored = [p for p in (data / "albums").iterdir() if p.name != "photo.txt"]
        assert [p.read_bytes() for p in restored] == [b"payload"]
        assert [t["name"] for t in list_trash(data)] == []

    def test_failed_restore_releases_the_reservation(self, monkeypatch, tmp_path):
        """A failed restore must not pin the destination name it wanted."""
        data = tmp_path / "data"
        td = trash.trash_dir(data)
        first = self._file_item(td, "a.bin", "albums/photo.txt", b"first")
        self._file_item(td, "b.bin", "albums/photo.txt", b"second")

        real_rename = trash._rename_onto_dest

        def failing_first_rename(src, dest, dest_fd):
            if src == first:
                raise OSError("disk full")
            return real_rename(src, dest, dest_fd)

        monkeypatch.setattr(trash, "_rename_onto_dest", failing_first_rename)
        assert restore_from_trash("a.bin", data) is False
        monkeypatch.undo()

        # The rival gets the original name, not a timestamped fallback, and the
        # failed item stays in trash for its own retry.
        assert restore_from_trash("b.bin", data) is True
        assert (data / "albums/photo.txt").read_bytes() == b"second"
        assert [t["name"] for t in list_trash(data)] == ["a.bin"]

    def test_concurrent_directory_restores_retain_both_trees_across_devices(
        self, monkeypatch, tmp_path
    ):
        """The cross-device staging fallback reserves the same destination."""
        data = tmp_path / "data"
        td = trash.trash_dir(data)
        first = self._dir_item(td, "dir_a", "album", "a.txt", b"A")
        self._dir_item(td, "dir_b", "album", "b.txt", b"B")
        trash_root = td.resolve()

        real_rename = trash._rename_onto_dest
        rival_results: list[bool] = []

        def cross_device_rename(src, dest, dest_fd):
            # Every rename out of the trash fails, so both restores must use
            # the staged copytree fallback; the rival runs while the first one
            # holds the reservation for ``album``.
            resolved = src.resolve()
            if trash_root in resolved.parents or resolved == trash_root:
                if src == first and not rival_results:
                    rival_results.append(restore_from_trash("dir_b", data))
                raise OSError("cross-device")
            return real_rename(src, dest, dest_fd)

        monkeypatch.setattr(trash, "_rename_onto_dest", cross_device_rename)

        assert restore_from_trash("dir_a", data) is True
        assert rival_results == [True]
        # The staged restore that reserved ``album`` first keeps the name; the
        # rival staged its way to a timestamped one instead.
        assert (data / "album" / "a.txt").read_bytes() == b"A"
        renamed = [p for p in data.iterdir() if p.is_dir() and p.name.startswith("album_")]
        assert len(renamed) == 1
        assert (renamed[0] / "b.txt").read_bytes() == b"B"
        stages = [p for p in data.rglob(f"{trash.STAGE_PREFIX}*")]
        assert stages == [], f"stage directory leaked: {stages}"
        assert list_trash(data) == []

    def test_claim_reclaim_keeps_claims_it_cannot_verify(self, monkeypatch, tmp_path):
        """No lock verdict means no delete, however old the claim looks."""
        data = tmp_path / "data"
        td = trash.trash_dir(data)
        claim = trash._restore_claim_path(td, data.resolve() / "album/photo.txt")
        claim.write_text("")

        monkeypatch.setattr(trash, "STAGE_ABANDON_GRACE_SECONDS", -1)
        monkeypatch.setattr(trash, "fcntl", None)
        trash._reclaim_restore_claims(td)
        assert claim.exists(), "an unverifiable claim must survive upkeep"

    def test_claim_reclaim_never_resurrects_a_vanished_claim(self, monkeypatch, tmp_path):
        """Upkeep tolerates bookkeeping another sweep retired first.

        Two workers can run the same sweep, so a claim can be gone between this
        sweep's listing and its reservation. The sweep must neither raise (a
        sweep runs inside request handlers) nor recreate the file: a fresh
        inode for a name somebody else still holds breaks the one-inode rule
        the reservation depends on.
        """
        data = tmp_path / "data"
        td = trash.trash_dir(data)
        claim = trash._restore_claim_path(td, data.resolve() / "album/photo.txt")
        claim.write_text("")

        real_acquire = trash._acquire_restore_claim

        def rival_retired(claim_path, create=True):
            claim_path.unlink(missing_ok=True)
            return real_acquire(claim_path, create=create)

        monkeypatch.setattr(trash, "_acquire_restore_claim", rival_retired)
        trash._reclaim_restore_claims(td)
        assert not claim.exists(), "upkeep must not recreate a claim it did not reserve"

        monkeypatch.setattr(trash, "_acquire_restore_claim", real_acquire)

        def vanished_trash_dir(self):
            raise OSError("trash directory vanished")

        monkeypatch.setattr(trash.Path, "iterdir", vanished_trash_dir)
        trash._reclaim_restore_claims(td)

    def test_claim_retired_between_open_and_lock_is_not_a_reservation(
        self, monkeypatch, tmp_path
    ):
        """A restore must never end up holding a lock on a retired claim inode.

        The sweep locks a claim before it unlinks it, so a restore whose
        ``flock`` attempt lands before the sweep's is refused — but a restore
        that *opened* the claim first and only attempts its ``flock`` after the
        sweep has locked, unlinked, and released then locks an inode that is
        no longer in the trash directory. That lock excludes nobody: the next
        restore creates a fresh inode for the same destination and reserves
        it too, so both restores would rename onto one destination and one
        payload would be lost (issue #608). The lock must therefore only count
        as a reservation while the descriptor still refers to the claim file.
        """
        data = tmp_path / "data"
        td = trash.trash_dir(data)
        claim = trash._restore_claim_path(td, data.resolve() / "album/photo.txt")
        claim.write_text("")
        monkeypatch.setattr(trash, "STAGE_ABANDON_GRACE_SECONDS", -1)

        real_flock = trash.fcntl.flock
        swept = False

        def flock_after_sweep(fd, operation):
            nonlocal swept
            if not swept:
                swept = True
                trash._reclaim_restore_claims(td)
            return real_flock(fd, operation)

        monkeypatch.setattr(trash.fcntl, "flock", flock_after_sweep)

        fd, state, _reason = trash._acquire_restore_claim(claim)
        assert state == trash.RESTORE_CLAIM_UNUSABLE, (
            "a lock on an inode the sweep retired must not be a reservation"
        )
        assert fd is None

        fd, state, _reason = trash._acquire_restore_claim(claim)
        assert state == trash.RESTORE_CLAIM_ACQUIRED
        trash._release_restore_claim(fd)

    def test_claim_reclaim_never_unlinks_a_replaced_claim(self, monkeypatch, tmp_path):
        """The sweep retires only the inode it locked, never a live replacement.

        A claim file can be replaced between this sweep's reservation and its
        unlink (another sweep retired the old inode and a restore created a
        fresh one). Unlinking by name would then destroy a live claim's file
        behind the lock the replacement's holder relies on, so the sweep must
        prove the locked inode is still the one at the path before unlinking.
        """
        data = tmp_path / "data"
        td = trash.trash_dir(data)
        claim = trash._restore_claim_path(td, data.resolve() / "album/photo.txt")
        claim.write_text("")
        monkeypatch.setattr(trash, "STAGE_ABANDON_GRACE_SECONDS", -1)

        real_grace = trash._exceeds_abandon_grace

        def grace_after_replacement(path, grace_seconds=None):
            if path == claim:
                claim.unlink(missing_ok=True)
                claim.write_text("")
            return real_grace(path, grace_seconds)

        monkeypatch.setattr(trash, "_exceeds_abandon_grace", grace_after_replacement)
        trash._reclaim_restore_claims(td)
        assert claim.exists(), "the sweep must not retire a claim it did not lock"


class TestActiveRestoreStageProtection:
    """Regression tests for issue #557: trash maintenance must never
    reclaim a ``.restore-stage-*`` directory that an in-flight cross-device
    restore is copying into, while genuinely abandoned stages stay
    reclaimable under an explicit policy.

    The distinguishing signal is an exclusive ``flock`` on
    ``<stage>.lock`` held by the restoring process for the whole copy.
    ``flock`` is honored across processes (and treated independently per
    open file description even within one process, which is what these
    deterministic single-process interleavings exploit), and the kernel
    drops it when the owner crashes — so any worker's maintenance can tell
    an active stage from an abandoned one. A per-process set could not.
    """

    def _make_album(self, data_dir):
        folder = data_dir / "album"
        folder.mkdir()
        (folder / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0a")
        (folder / "sub").mkdir()
        (folder / "sub" / "b.jpg").write_bytes(b"\xff\xd8\xff\xe0b")
        return folder

    def _interleave_maintenance(self, monkeypatch, data_dir, maintenance):
        """Force the cross-device fallback and run *maintenance* mid-restore.

        Renames whose source lives under the trash fail with EXDEV (a true
        cross-filesystem restore), and *maintenance* is invoked between the
        real ``copytree`` and the staged ``rename`` — the window in which a
        second worker's ordinary maintenance used to delete the live stage.
        The assertion inside the copytree wrapper fails loudly if the sweep
        removes the stage the restore still owns.
        """
        real_rename = trash.Path.rename
        trash_root = trash.trash_dir(data_dir).resolve()

        def cross_device_rename(src, dest, dest_fd):
            try:
                resolved = src.resolve()
            except OSError:
                return real_rename(src, dest)
            if trash_root in resolved.parents or resolved == trash_root:
                raise OSError(errno.EXDEV, "cross-device link")
            return real_rename(src, dest)

        real_copytree = trash.shutil.copytree

        def copytree_with_maintenance(src, dest, *args, **kwargs):
            result = real_copytree(src, dest, *args, **kwargs)
            # shutil.copytree converts to str and recurses through this same
            # attribute; only the top-level call from restore_from_trash
            # passes Path objects, so inject maintenance exactly once.
            if isinstance(dest, Path):
                maintenance()
                assert dest.is_dir(), (
                    "concurrent trash maintenance deleted an active restore stage"
                )
            return result

        monkeypatch.setattr(trash, "_rename_onto_dest", cross_device_rename)
        monkeypatch.setattr(trash.shutil, "copytree", copytree_with_maintenance)

    def test_purge_during_active_cross_device_restore_preserves_stage(
        self, monkeypatch, tmp_path
    ):
        """The issue #557 repro: ``purge_old_trash`` with the default
        30-day retention used to delete a stage created milliseconds
        earlier. Maintenance must skip the locked stage and the restore
        must complete."""
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_album(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item = trash.trash_dir(data_dir) / entry
        meta_file = trash.trash_dir(data_dir) / (entry + trash.META_SUFFIX)

        self._interleave_maintenance(
            monkeypatch, data_dir, lambda: trash.purge_old_trash(data_dir, retention_days=30)
        )

        assert restore_from_trash(entry, data_dir) is True
        assert (folder / "a.jpg").read_bytes() == b"\xff\xd8\xff\xe0a"
        assert (folder / "sub" / "b.jpg").read_bytes() == b"\xff\xd8\xff\xe0b"
        assert not trash_item.exists()
        assert not meta_file.exists()
        assert list_trash(data_dir) == []
        leftovers = [p for p in data_dir.rglob(f"{trash.STAGE_PREFIX}*")]
        assert leftovers == [], f"stage artifacts leaked: {leftovers}"

    def test_empty_during_active_cross_device_restore_preserves_stage(
        self, monkeypatch, tmp_path
    ):
        """``empty_trash`` run mid-restore must not delete the live stage.

        Emptying the trash removes the restore's trash source from under
        it; the staged copy is already complete at that point, so the
        restore still reports success with the full tree at its
        destination rather than failing after the media was restored.
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_album(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]

        self._interleave_maintenance(
            monkeypatch, data_dir, lambda: trash.empty_trash(data_dir)
        )

        assert restore_from_trash(entry, data_dir) is True
        assert (folder / "a.jpg").read_bytes() == b"\xff\xd8\xff\xe0a"
        assert (folder / "sub" / "b.jpg").read_bytes() == b"\xff\xd8\xff\xe0b"
        assert list_trash(data_dir) == []
        leftovers = [p for p in data_dir.rglob(f"{trash.STAGE_PREFIX}*")]
        assert leftovers == [], f"stage artifacts leaked: {leftovers}"

    def test_crashed_owner_stage_reclaimed_immediately(self, monkeypatch, tmp_path):
        """A stage plus a released lock file is abandoned, not active.

        A crashed owner's ``flock`` is dropped by the kernel, so the lock
        file is present but held by nobody. Maintenance reclaims such a
        stage immediately under both entry points, without waiting out the
        grace period (the orphans here carry fresh timestamps).
        """
        client, data_dir = build_client(monkeypatch, tmp_path)

        orphan_parent = data_dir / "album"
        orphan_parent.mkdir()
        orphan = orphan_parent / ".restore-stage-deadbeef"
        orphan.mkdir()
        (orphan / "partial.jpg").write_bytes(b"partial")
        lock = trash._stage_lock_path(orphan)
        lock.write_text("")

        trash.empty_trash(data_dir)
        assert not orphan.exists(), "empty_trash must reclaim a stage whose lock is released"
        assert not lock.exists(), "the released lock file must be reclaimed with its stage"

        # Same again under purge_old_trash with a fresh crashed orphan.
        orphan.mkdir()
        (orphan / "partial.jpg").write_bytes(b"partial")
        lock.write_text("")
        trash.purge_old_trash(data_dir, retention_days=30)
        assert not orphan.exists(), "purge_old_trash must reclaim a stage whose lock is released"

        # And a crashed orphan at the data folder root, not under a subdir.
        orphan_root = data_dir / ".restore-stage-abcdef"
        orphan_root.mkdir()
        trash._stage_lock_path(orphan_root).write_text("")
        trash.empty_trash(data_dir)
        assert not orphan_root.exists(), (
            "empty_trash must reclaim orphan stages at the data folder root"
        )

    def test_held_lock_protects_stage_even_past_grace(self, monkeypatch, tmp_path):
        """A lock held by a live process outranks the age policy.

        With the grace period forced negative (everything looks old), a
        stage whose lock this process still holds must survive both
        maintenance entry points — this is what distinguishes an active
        stage from an abandoned one.
        """
        import fcntl

        client, data_dir = build_client(monkeypatch, tmp_path)
        album = data_dir / "album"
        album.mkdir()
        # The directory name must match the sweep glob, or the sweep would
        # never inspect it and the test would pass vacuously.
        stage = album / ".restore-stage-held"
        stage.mkdir()
        (stage / "partial.jpg").write_bytes(b"partial")
        lock = trash._stage_lock_path(stage)
        fh = open(lock, "w")  # noqa: SIM115 - held open to keep the flock live
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            monkeypatch.setattr(trash, "STAGE_ABANDON_GRACE_SECONDS", -1)
            trash.empty_trash(data_dir)
            trash.purge_old_trash(data_dir, retention_days=1)
            assert stage.exists(), "maintenance must not reclaim a stage under a held lock"
            assert lock.exists()
        finally:
            fh.close()

        # Control: release the lock (simulating the owner's death) and the
        # very same stage becomes reclaimable, proving the sweep did see it.
        trash.empty_trash(data_dir)
        assert not stage.exists(), "released stage must be reclaimed once lock is free"

    def test_lock_free_stage_respects_explicit_grace_policy(self, monkeypatch, tmp_path):
        """Stages with no verifiable lock are abandoned only by age.

        Lock-free stages are legacy leftovers (written before the lock
        protocol) or restores on filesystems without flock support. They
        are preserved while they could still be a live copy, and reclaimed
        once older than ``STAGE_ABANDON_GRACE_SECONDS``.
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        orphan_parent = data_dir / "album"
        orphan_parent.mkdir()
        orphan = orphan_parent / ".restore-stage-legacy"
        orphan.mkdir()
        (orphan / "partial.jpg").write_bytes(b"partial")

        # Fresh and unverifiable: keep it (it could be a live copy).
        trash.empty_trash(data_dir)
        trash.purge_old_trash(data_dir, retention_days=1)
        assert orphan.exists(), "a fresh lock-free stage must survive the grace period"

        # Past the grace period: abandoned, reclaimed by both entry points.
        monkeypatch.setattr(trash, "STAGE_ABANDON_GRACE_SECONDS", -1)
        trash.empty_trash(data_dir)
        assert not orphan.exists(), "empty_trash must reclaim a grace-expired lock-free stage"

        orphan.mkdir()
        monkeypatch.setattr(trash, "STAGE_ABANDON_GRACE_SECONDS", -1)
        trash.purge_old_trash(data_dir, retention_days=1)
        assert not orphan.exists(), "purge_old_trash must reclaim a grace-expired stage"

        # A grace-expired orphan at the data folder root too.
        orphan_root = data_dir / ".restore-stage-abcdef"
        orphan_root.mkdir()
        trash.empty_trash(data_dir)
        assert not orphan_root.exists(), (
            "empty_trash must reclaim orphan stages at the data folder root"
        )

    def test_leaked_lock_file_lifecycle(self, monkeypatch, tmp_path):
        """A lock file whose stage directory never appeared is reclaimed
        only under the grace policy, and never while a live restore holds
        it.

        A free lock without a directory proves nothing: the owner may be
        moments from creating the stage (the restore takes the flock just
        before copytree creates the directory). Unlinking it would strip a
        live restore of its protection mid-copy, so freshness preserves it
        even when it looks free.
        """
        import fcntl

        client, data_dir = build_client(monkeypatch, tmp_path)
        leak = data_dir / "album"
        leak.mkdir()
        lock = leak / f"{trash.STAGE_PREFIX}ghost{trash.STAGE_LOCK_SUFFIX}"

        # Fresh free lock-only entry: looks abandoned, is not provably so.
        lock.write_text("")
        trash.empty_trash(data_dir)
        trash.purge_old_trash(data_dir, retention_days=30)
        assert lock.exists(), "a fresh orphan lock must survive the grace period"

        # Past the grace period with no stage ever appearing: a real leak.
        monkeypatch.setattr(trash, "STAGE_ABANDON_GRACE_SECONDS", -1)
        trash.empty_trash(data_dir)
        assert not lock.exists(), "empty_trash must reclaim a grace-expired orphan lock file"

        # Held lock (owner mid-restore): preserved even with grace negative.
        lock.write_text("")
        fh = open(lock, "w")  # noqa: SIM115 - held open to keep the flock live
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            monkeypatch.setattr(trash, "STAGE_ABANDON_GRACE_SECONDS", -1)
            trash.empty_trash(data_dir)
            assert lock.exists(), "maintenance must not unlink a lock an active restore holds"
        finally:
            fh.close()

    def test_lock_acquisition_failure_leaves_no_abandon_signal(self, monkeypatch, tmp_path):
        """A failed lock attempt must clean up after itself.

        If flock is unsupported (or the open itself fails), no lock file
        may remain: a present-but-free lock file is the signal for a
        *crashed* owner, and leaving one behind for a live restore would
        invite immediate reclaim by the very maintenance it is meant to
        deter. Without a lock file, the stage only faces the conservative
        grace policy.
        """

        def unsupported_flock(*args, **kwargs):
            raise OSError(errno.EINVAL, "flock unsupported")

        monkeypatch.setattr(trash.fcntl, "flock", unsupported_flock)

        assert trash._acquire_stage_lock(tmp_path / ".restore-stage-x") is None
        assert not (tmp_path / ".restore-stage-x.lock").exists(), (
            "failed acquisition must leave no lock file behind"
        )

        def boom_open(*args, **kwargs):
            raise OSError(errno.EROFS, "read-only filesystem")

        monkeypatch.setattr("builtins.open", boom_open)
        assert trash._acquire_stage_lock(tmp_path / ".restore-stage-y") is None

    def test_unverifiable_lock_state_falls_back_to_grace_policy(self, monkeypatch, tmp_path):
        """Lock states other than held/free classify as unknown, not abandoned.

        On a filesystem where flock errors (not EAGAIN), maintenance must
        not read the lock as released-and-reclaim-now; the stage is only
        reclaimed once past the explicit grace period.
        """

        def unsupported_flock(*args, **kwargs):
            raise OSError(errno.EINVAL, "flock unsupported")

        client, data_dir = build_client(monkeypatch, tmp_path)
        stage = data_dir / ".restore-stage-nolockfs"
        stage.mkdir()
        lock = trash._stage_lock_path(stage)
        lock.write_text("")

        monkeypatch.setattr(trash.fcntl, "flock", unsupported_flock)
        assert trash._stage_lock_state(lock) is None

        trash.empty_trash(data_dir)
        assert stage.exists(), "unverifiable fresh stage must survive the grace period"

        monkeypatch.setattr(trash, "STAGE_ABANDON_GRACE_SECONDS", -1)
        trash.empty_trash(data_dir)
        assert not stage.exists(), "unverifiable grace-expired stage must be reclaimed"
        assert not lock.exists()

    def test_failed_source_cleanup_with_source_present_keeps_retry_semantics(
        self, monkeypatch, tmp_path
    ):
        """The vanished-source success is narrowly scoped.

        When the trash source still exists but cannot be removed (e.g.
        permission error), the restore must keep its historical behavior:
        return False with the trash entry and metadata preserved so a
        retry stays possible. Only a source that is *gone* (reclaimed by
        concurrent maintenance after the copy completed) counts as done.
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_album(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item = trash.trash_dir(data_dir) / entry
        meta_file = trash.trash_dir(data_dir) / (entry + trash.META_SUFFIX)

        real_rename = trash.Path.rename
        trash_root = trash.trash_dir(data_dir).resolve()

        def cross_device_rename(src, dest, dest_fd):
            try:
                resolved = src.resolve()
            except OSError:
                return real_rename(src, dest)
            if trash_root in resolved.parents or resolved == trash_root:
                raise OSError(errno.EXDEV, "cross-device link")
            return real_rename(src, dest)

        real_rmtree = trash.shutil.rmtree

        def stuck_rmtree(path, *args, **kwargs):
            if Path(path) == trash_item:
                raise OSError("directory busy")
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(trash, "_rename_onto_dest", cross_device_rename)
        monkeypatch.setattr(trash.shutil, "rmtree", stuck_rmtree)

        assert restore_from_trash(entry, data_dir) is False
        assert trash_item.is_dir(), "unremovable trash source must stay for a retry"
        assert meta_file.exists()
        assert (folder / "a.jpg").read_bytes() == b"\xff\xd8\xff\xe0a"
        leftovers = [p for p in data_dir.rglob(f"{trash.STAGE_PREFIX}*")]
        assert leftovers == [], f"stage artifacts leaked: {leftovers}"


class TestSourceLockProtectsTrashSource:
    """Regression tests for the #557 review blocker: the trash SOURCE.

    The stage lock only protects the staged copy on the destination
    filesystem; ``empty_trash``/``purge_old_trash`` could still delete the
    trash item itself while a cross-device ``copytree`` was reading it,
    letting a partial restore report success after the source was gone.

    The distinguishing signal is now an exclusive ``flock`` on the item's
    ``<item>.meta.json`` (the meta file doubles as the source lock, so no
    extra lock file is needed). The restore holds it for the whole restore;
    maintenance must ACQUIRE it — not merely probe it — before deleting, so
    a held lock skips the item entirely and a crashed restore leaves a free
    lock (the kernel drops flocks on process death) that any sweep can
    reclaim immediately.
    """

    def _make_album(self, data_dir):
        folder = data_dir / "album"
        folder.mkdir()
        (folder / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0a")
        (folder / "sub").mkdir()
        (folder / "sub" / "b.jpg").write_bytes(b"\xff\xd8\xff\xe0b")
        return folder

    def _trash_paths(self, data_dir, entry):
        trash_item = trash.trash_dir(data_dir) / entry
        meta_file = trash.trash_dir(data_dir) / (entry + trash.META_SUFFIX)
        return trash_item, meta_file

    def _hold_meta_lock(self, meta_file):
        """Open the meta file and flock it like an external worker would.

        ``flock`` is treated independently per open file description even
        within one process, so this fd makes the item look in-use to the
        restore/sweep code running in the same test process.
        """
        import fcntl

        fh = open(meta_file, "r")  # noqa: SIM115 - held open to keep the flock live
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh

    def _force_cross_device_restore(self, monkeypatch, data_dir):
        """Make every rename whose source lives under the trash fail EXDEV."""
        real_rename = trash.Path.rename
        trash_root = trash.trash_dir(data_dir).resolve()

        def cross_device_rename(src, dest, dest_fd):
            try:
                resolved = src.resolve()
            except OSError:
                return real_rename(src, dest)
            if trash_root in resolved.parents or resolved == trash_root:
                raise OSError(errno.EXDEV, "cross-device link")
            return real_rename(src, dest)

        monkeypatch.setattr(trash, "_rename_onto_dest", cross_device_rename)

    def test_empty_mid_copytree_preserves_trash_source(self, monkeypatch, tmp_path):
        """The #557 data-loss repro: ``empty_trash`` running mid-``copytree``.

        The restore holds the source lock for the whole copy, so the sweep
        must skip the trash item and its meta entirely — the wrapper below
        runs ``empty_trash`` between the real ``copytree`` and the staged
        rename and asserts, mid-copy, that the sweep counted nothing and
        the source still exists. The restore then completes with the full
        tree at its destination and consumes item+meta.
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_album(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item, meta_file = self._trash_paths(data_dir, entry)

        self._force_cross_device_restore(monkeypatch, data_dir)
        real_copytree = trash.shutil.copytree

        def copytree_with_maintenance(src, dest, *args, **kwargs):
            result = real_copytree(src, dest, *args, **kwargs)
            # shutil.copytree recurses through this same attribute; only the
            # top-level call from restore_from_trash passes Path objects.
            if isinstance(dest, Path):
                deleted = trash.empty_trash(data_dir)
                assert deleted == 0, (
                    "empty_trash deleted entries while a restore was copying"
                )
                assert trash_item.is_dir(), "the trash source was deleted mid-copytree"
                assert meta_file.exists(), "the trash metadata was deleted mid-copytree"
            return result

        monkeypatch.setattr(trash.shutil, "copytree", copytree_with_maintenance)

        assert restore_from_trash(entry, data_dir) is True
        assert (folder / "a.jpg").read_bytes() == b"\xff\xd8\xff\xe0a"
        assert (folder / "sub" / "b.jpg").read_bytes() == b"\xff\xd8\xff\xe0b"
        assert not trash_item.exists()
        assert not meta_file.exists()
        assert list_trash(data_dir) == []
        leftovers = [p for p in data_dir.rglob(f"{trash.STAGE_PREFIX}*")]
        assert leftovers == [], f"stage artifacts leaked: {leftovers}"

    def test_maintenance_skips_items_with_externally_held_source_lock(
        self, monkeypatch, tmp_path
    ):
        """Both sweeps skip an item whose meta lock another worker holds.

        While the lock is held, ``empty_trash``/``purge_old_trash`` must
        leave item and meta untouched and count nothing. Once the external
        holder releases, the same sweeps delete normally with the exact
        legacy counts: item and meta each count 1 under ``empty_trash``,
        and the pair counts once under ``purge_old_trash``.
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_album(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item, meta_file = self._trash_paths(data_dir, entry)

        # Make the item look long expired for the purge half of the test.
        old_iso = (trash._utcnow() - timedelta(days=40)).isoformat()
        meta_file.write_text(json.dumps({"original": "album", "deleted_at": old_iso}))

        fh = self._hold_meta_lock(meta_file)
        try:
            assert trash.empty_trash(data_dir) == 0
            assert trash_item.is_dir()
            assert meta_file.exists()
            assert trash.purge_old_trash(data_dir, retention_days=1) == 0
            assert trash_item.is_dir()
            assert meta_file.exists()
        finally:
            fh.close()

        # Released: the item is reclaimable again with legacy counts
        # (empty_trash counts the item and its meta separately).
        assert trash.empty_trash(data_dir) == 2
        assert not trash_item.exists()
        assert not meta_file.exists()

        # Same for purge_old_trash on a fresh expired item.
        folder = self._make_album(data_dir)
        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item, meta_file = self._trash_paths(data_dir, entry)
        old_iso = (trash._utcnow() - timedelta(days=40)).isoformat()
        meta_file.write_text(json.dumps({"original": "album", "deleted_at": old_iso}))

        fh = self._hold_meta_lock(meta_file)
        try:
            assert trash.purge_old_trash(data_dir, retention_days=1) == 0
            assert trash_item.is_dir()
            assert meta_file.exists()
        finally:
            fh.close()

        assert trash.purge_old_trash(data_dir, retention_days=1) == 1
        assert not trash_item.exists()
        assert not meta_file.exists()

    def test_restore_fails_fast_when_source_lock_held(self, monkeypatch, tmp_path):
        """A held source lock makes the restore return False before copying.

        If maintenance (or another worker) holds the item's meta flock, the
        restore must fail fast — before any copy starts — instead of
        racing a concurrent empty/purge. The rename is forced to fail with
        EXDEV so the restore would take the cross-device ``copytree`` path,
        and ``copytree`` records any invocation: on pre-fix code the copy
        runs (and a mid-copy ``empty_trash`` deletes the source); with the
        fix the invocation list stays empty.
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_album(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item, meta_file = self._trash_paths(data_dir, entry)

        self._force_cross_device_restore(monkeypatch, data_dir)
        real_copytree = trash.shutil.copytree
        copy_calls = []

        def must_not_copy(*args, **kwargs):
            copy_calls.append(True)
            raise AssertionError("restore must fail fast on a held source lock")

        monkeypatch.setattr(trash.shutil, "copytree", must_not_copy)

        fh = self._hold_meta_lock(meta_file)
        try:
            assert restore_from_trash(entry, data_dir) is False
            assert copy_calls == [], "restore started copying despite a held source lock"
            # The held item and its metadata are untouched.
            assert trash_item.is_dir()
            assert meta_file.exists()
            assert not folder.exists()
        finally:
            fh.close()

        # Once the lock is free the very same item restores normally — the
        # EXDEV forcing stays active, so this exercises the real cross-device
        # staging path with the genuine copytree restored.
        monkeypatch.setattr(trash.shutil, "copytree", real_copytree)
        assert restore_from_trash(entry, data_dir) is True
        assert (folder / "a.jpg").read_bytes() == b"\xff\xd8\xff\xe0a"
        assert list_trash(data_dir) == []

    def test_released_source_lock_item_deleted_normally(self, monkeypatch, tmp_path):
        """Crash semantics: a released lock leaves the item reclaimable.

        A crashed owner's flock is dropped by the kernel; holding the lock
        and releasing it must leave item+meta in exactly the state any
        sweep treats as ordinary trash. Both entry points must delete it
        immediately, with the legacy counts.
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = self._make_album(data_dir)

        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item, meta_file = self._trash_paths(data_dir, entry)

        fh = self._hold_meta_lock(meta_file)
        fh.close()  # kernel releases the flock: owner "died"

        assert trash.empty_trash(data_dir) == 2
        assert not trash_item.exists()
        assert not meta_file.exists()

        # Same for purge_old_trash on a fresh expired item.
        folder = self._make_album(data_dir)
        assert move_to_trash(folder, data_dir) is True
        entry = list_trash(data_dir)[0]["name"]
        trash_item, meta_file = self._trash_paths(data_dir, entry)
        old_iso = (trash._utcnow() - timedelta(days=40)).isoformat()
        meta_file.write_text(json.dumps({"original": "album", "deleted_at": old_iso}))

        fh = self._hold_meta_lock(meta_file)
        fh.close()
        assert trash.purge_old_trash(data_dir, retention_days=1) == 1
        assert not trash_item.exists()
        assert not meta_file.exists()

    def test_stale_meta_without_item_is_reclaimed_by_empty(self, monkeypatch, tmp_path):
        """A meta file with no paired item past the claim grace is stale:
        unlink it, count 1.

        Deleting the meta before the item would strip the item of its lock
        file, so a meta whose item still exists is left for the item's own
        iteration. A lone meta inside the claim grace may belong to an
        in-flight delete (#606, see test_empty_trash_keeps_live_claim_sidecar);
        aging the clock here models the crashed owner whose grace elapsed.
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        td = trash.trash_dir(data_dir)
        stale = td / "ghost.txt.meta.json"
        stale.write_text(json.dumps({"original": "ghost.txt"}))
        now = time.time()
        monkeypatch.setattr(trash.time, "time", lambda: now + 25 * 86400)

        assert trash.empty_trash(data_dir) == 1
        assert not stale.exists()

    def test_entry_named_exactly_meta_suffix_is_deleted_as_item(
        self, monkeypatch, tmp_path
    ):
        """A trashed entry literally named ".meta.json" is an item, not a meta.

        Stripping META_SUFFIX would leave an empty paired name, so this
        entry must take the ordinary item path (deleted and counted) rather
        than being misclassified as a stale meta and skipped forever.
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        td = trash.trash_dir(data_dir)
        odd = td / ".meta.json"
        odd.write_bytes(b"data")

        assert trash.empty_trash(data_dir) == 1
        assert not odd.exists()


class TestRestorePathTraversal:
    """Regression tests for path traversal in restore_from_trash (#382).

    The <path:> converter in Flask allows / and .., so a crafted item_name
    could escape the trash directory. These tests verify that traversal
    attempts are rejected.
    """

    def test_restore_rejects_dotdot_in_name(self, monkeypatch, tmp_path):
        from trash import restore_from_trash

        client, data_dir = build_client(monkeypatch, tmp_path)

        # Create a file outside the trash dir that an attacker might target
        secret = data_dir / "secret.txt"
        secret.write_text("top_secret")

        with pytest.raises(ValueError, match="Invalid trash item name"):
            restore_from_trash("../secret.txt", data_dir)

        # Verify the secret file was not moved
        assert secret.exists()
        assert secret.read_text() == "top_secret"

    def test_restore_rejects_double_dotdot(self, monkeypatch, tmp_path):
        from trash import restore_from_trash

        client, data_dir = build_client(monkeypatch, tmp_path)

        # Create a file outside the data dir entirely
        external = tmp_path / "external_secret.txt"
        external.write_text("external_data")

        with pytest.raises(ValueError, match="Invalid trash item name"):
            restore_from_trash("../../external_secret.txt", data_dir)

        # Verify the external file was not touched
        assert external.exists()
        assert external.read_text() == "external_data"

    def test_restore_rejects_slash_in_name(self, monkeypatch, tmp_path):
        from trash import restore_from_trash

        client, data_dir = build_client(monkeypatch, tmp_path)

        with pytest.raises(ValueError, match="Invalid trash item name"):
            restore_from_trash("subdir/file.txt", data_dir)

    def test_restore_rejects_backslash_in_name(self, monkeypatch, tmp_path):
        from trash import restore_from_trash

        client, data_dir = build_client(monkeypatch, tmp_path)

        with pytest.raises(ValueError, match="Invalid trash item name"):
            restore_from_trash("subdir\\file.txt", data_dir)

    def test_restore_rejects_mixed_traversal(self, monkeypatch, tmp_path):
        from trash import restore_from_trash

        client, data_dir = build_client(monkeypatch, tmp_path)

        with pytest.raises(ValueError, match="Invalid trash item name"):
            restore_from_trash("foo/../../../etc/passwd", data_dir)

    def test_restore_rejects_null_byte_in_name(self, monkeypatch, tmp_path):
        """An embedded null byte must be rejected before any path syscall.

        ``Path("bad\\x00name").exists()`` would raise a bare
        ``ValueError: embedded null byte`` from os.stat; the explicit guard
        raises the canonical "Invalid trash item name" error instead and
        never touches the filesystem.
        """
        from trash import restore_from_trash, trash_dir

        client, data_dir = build_client(monkeypatch, tmp_path)

        td = trash_dir(data_dir)
        with pytest.raises(ValueError, match="Invalid trash item name"):
            restore_from_trash("bad\x00name", data_dir)

        # Nothing was read, created, or deleted in the trash directory.
        assert list(td.iterdir()) == []

    def test_restore_rejects_null_byte_in_meta_original(self, monkeypatch, tmp_path):
        """A null byte in the stored original path is refused, not crashed on.

        The stored path never reaches the filesystem: resolving it raises the
        ``ValueError`` the restore route turns into a flash message, and the
        trash entry stays intact for a retry.
        """
        from trash import restore_from_trash, trash_dir

        client, data_dir = build_client(monkeypatch, tmp_path)

        td = trash_dir(data_dir)
        td.mkdir(parents=True, exist_ok=True)

        fake_item = td / "fake_item.txt"
        fake_item.write_text("harmless")
        meta_file = td / "fake_item.txt.meta.json"
        meta_file.write_text(
            json.dumps({"original": "albums/bad\x00name.txt", "deleted_at": 0})
        )

        with pytest.raises(ValueError):
            restore_from_trash("fake_item.txt", data_dir)

        # The entry stays in the trash: nothing was consumed.
        assert fake_item.exists()
        assert meta_file.exists()

    def test_restore_rejects_malicious_meta_original(self, monkeypatch, tmp_path):
        """Even if item_name is valid, a crafted .meta.json 'original' must not escape."""
        from trash import restore_from_trash, trash_dir

        client, data_dir = build_client(monkeypatch, tmp_path)

        td = trash_dir(data_dir)
        td.mkdir(parents=True, exist_ok=True)

        # Create a valid-looking item in trash with a malicious meta
        fake_item = td / "fake_item.txt"
        fake_item.write_text("harmless")
        meta_file = td / "fake_item.txt.meta.json"
        meta_file.write_text(
            json.dumps({"original": "../../../etc/passwd", "deleted_at": 0})
        )

        with pytest.raises(ValueError, match="Invalid original path"):
            restore_from_trash("fake_item.txt", data_dir)

    def test_restore_rejects_meta_original_dotdot(self, monkeypatch, tmp_path):
        """Meta 'original' with .. segments must be rejected."""
        from trash import restore_from_trash, trash_dir

        client, data_dir = build_client(monkeypatch, tmp_path)

        td = trash_dir(data_dir)
        td.mkdir(parents=True, exist_ok=True)

        fake_item = td / "fake2.txt"
        fake_item.write_text("harmless")
        meta_file = td / "fake2.txt.meta.json"
        meta_file.write_text(
            json.dumps({"original": "../outside_trash.txt", "deleted_at": 0})
        )

        with pytest.raises(ValueError, match="Invalid original path"):
            restore_from_trash("fake2.txt", data_dir)


class TestRestoreSymlinkSafety:
    """Regression tests for symlink inlining on restore (#455).

    shutil.copytree defaults to symlinks=False, which follows symlinks and
    copies target contents as regular files. A trashed directory containing a
    symlink (e.g. passwd_link.png -> /etc/passwd) must therefore be refused
    on restore so external-target bytes never land under DATA_FOLDER.
    """

    def test_restore_refuses_directory_containing_symlink(self, monkeypatch, tmp_path):
        from trash import list_trash, move_to_trash, restore_from_trash

        client, data_dir = build_client(monkeypatch, tmp_path)

        # External secret that a symlink inside the trashed dir would expose
        secret = tmp_path / "secret_target.txt"
        secret.write_text("TOP_SECRET_BYTES")

        folder = data_dir / "photos"
        folder.mkdir()
        (folder / "real.jpg").write_bytes(b"\xff\xd8\xff\xe0real")
        (folder / "passwd_link.png").symlink_to(secret)

        assert move_to_trash(folder, data_dir) is True
        assert not folder.exists()

        trash_items = list_trash(data_dir)
        trash_entry = trash_items[0]["name"]

        with pytest.raises(ValueError, match="contains symlinks"):
            restore_from_trash(trash_entry, data_dir)

        # Nothing may have been restored into DATA_FOLDER
        assert not folder.exists()
        for path in data_dir.rglob("*"):
            if path.is_file() and not path.is_symlink():
                assert path.read_bytes() != b"TOP_SECRET_BYTES"

        # The trashed item must remain in trash (not consumed by the refusal)
        assert any(t["name"] == trash_entry for t in list_trash(data_dir))

    def test_restore_refuses_symlinked_directory_item(self, monkeypatch, tmp_path):
        """A trashed item that is itself a symlink must also be refused."""
        from trash import restore_from_trash, trash_dir

        client, data_dir = build_client(monkeypatch, tmp_path)

        external = tmp_path / "external_dir"
        external.mkdir()
        (external / "leak.png").write_bytes(b"EXTERNAL")

        td = trash_dir(data_dir)
        link = td / "linked_dir"
        link.symlink_to(external)
        link.with_name(link.name + trash.META_SUFFIX).write_text(
            json.dumps({"original": "linked_dir"})
        )

        with pytest.raises(ValueError, match="contains symlinks"):
            restore_from_trash("linked_dir", data_dir)

        assert not (data_dir / "linked_dir").exists()

    def test_restore_symlink_free_directory_still_works(self, monkeypatch, tmp_path):
        """Legitimate symlink-free restores must not regress."""
        from trash import move_to_trash, restore_from_trash

        client, data_dir = build_client(monkeypatch, tmp_path)

        folder = data_dir / "clean"
        folder.mkdir()
        (folder / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0a")
        (folder / "sub").mkdir()
        (folder / "sub" / "b.jpg").write_bytes(b"\xff\xd8\xff\xe0b")

        assert move_to_trash(folder, data_dir) is True
        entry = [t["name"] for t in trash.list_trash(data_dir)][0]

        assert restore_from_trash(entry, data_dir) is True
        assert (folder / "a.jpg").read_bytes() == b"\xff\xd8\xff\xe0a"
        assert (folder / "sub" / "b.jpg").read_bytes() == b"\xff\xd8\xff\xe0b"

    def test_cross_device_trash_fallback_preserves_symlinks(self, monkeypatch, tmp_path):
        """The copytree fallback in move_to_trash must not inline symlink targets."""
        from trash import move_to_trash, trash_dir

        client, data_dir = build_client(monkeypatch, tmp_path)

        secret = tmp_path / "secret.txt"
        secret.write_text("SECRET")
        source = data_dir / "album"
        source.mkdir()
        (source / "photo.txt").write_text("photo")
        (source / "link.txt").symlink_to(secret)

        real_rename = trash.Path.rename

        def cross_device_rename(path, destination):
            if path == source:
                raise OSError("cross-device")
            return real_rename(path, destination)

        monkeypatch.setattr(trash.Path, "rename", cross_device_rename)
        assert move_to_trash(source, data_dir) is True

        trashed = next(
            p for p in trash_dir(data_dir).iterdir()
            if p.is_dir() and not p.name.endswith(trash.META_SUFFIX)
        )
        link = trashed / "link.txt"
        assert link.is_symlink(), "fallback copy must preserve symlinks as links"
        # The link must still point at the external target, not a copied file
        assert link.resolve() == secret.resolve()


class TestTrashListIncludesDirs:
    """Verify trash listing correctly shows directories with sizes."""

    def test_list_trash_shows_directory_size(self, monkeypatch, tmp_path):
        from trash import list_trash, move_to_trash

        client, data_dir = build_client(monkeypatch, tmp_path)
        folder = data_dir / "listed_folder"
        folder.mkdir()
        (folder / "big.jpg").write_bytes(b"\xff\xd8\xff\xe0" * 100)

        move_to_trash(folder, data_dir)
        trash_items = list_trash(data_dir)

        assert len(trash_items) == 1
        entry = trash_items[0]
        assert trash_items[0]["original"] == "listed_folder"
        assert entry["size"] > 0


class TestConflictingNames:
    """Verify handling of name collisions in trash."""

    def test_trash_handles_name_collision(self, monkeypatch, tmp_path):
        from trash import list_trash, move_to_trash

        client, data_dir = build_client(monkeypatch, tmp_path)

        for _ in range(2):
            folder = data_dir / "collision_test"
            folder.mkdir()
            (folder / "file.jpg").write_bytes(b"fakesize")
            move_to_trash(folder, data_dir)
            time.sleep(0.01)

        trash_items = list_trash(data_dir)
        assert len(trash_items) == 2
        originals = {t["original"] for t in trash_items}
        assert "collision_test" in originals


class TestDirSizeSymlinkConsistency:
    """Verify dir_size() consistently skips symlinks (regression for #266).

    Previously, _dir_size() did not skip symlinks while dir_size() did.
    After standardization, all size calculations should use dir_size() which
    skips symlinks for security.
    """

    def test_dir_size_skips_symlinks(self, monkeypatch, tmp_path):
        from trash import dir_size

        # Create a directory with a real file and a symlink to a large external file
        folder = tmp_path / "test_folder"
        folder.mkdir()
        (folder / "real.txt").write_bytes(b"x" * 100)

        # Create an external file outside the folder
        external = tmp_path / "external_large.txt"
        external.write_bytes(b"y" * 10000)

        # Create a symlink inside the folder pointing to the external file
        (folder / "link.txt").symlink_to(external)

        # dir_size should only count the real file, not follow symlinks
        size = dir_size(folder)
        assert size == 100, f"Expected 100 bytes (real file only), got {size}"

    def test_move_to_trash_uses_dir_size_not_private(self, monkeypatch, tmp_path):
        """Verify move_to_trash records size using symlink-safe dir_size()."""
        from trash import list_trash, move_to_trash

        client, data_dir = build_client(monkeypatch, tmp_path)

        folder = data_dir / "symlink_test"
        folder.mkdir()
        (folder / "real.txt").write_bytes(b"a" * 50)

        # Create external file and symlink inside the folder
        external = data_dir / "external.txt"
        external.write_bytes(b"b" * 5000)
        (folder / "link.txt").symlink_to(external)

        move_to_trash(folder, data_dir)

        trash_items = list_trash(data_dir)
        assert len(trash_items) == 1
        # Size should be 50 (real file only), not 5050 (which would include symlink target)
        assert trash_items[0]["size"] == 50, (
            f"Expected 50 bytes (symlinks skipped), got {trash_items[0]['size']}"
        )


class TestTrashPurgeValidation:
    """Verify trash_purge() validates days input and provides user feedback (regression for #364)."""

    @pytest.fixture(autouse=True)
    def reset_rate_limiters(self):
        """Reset rate limiter state before each test to avoid cross-test interference."""
        from security import FALLBACK_LIMITER, _primary_limiter

        FALLBACK_LIMITER.reset()
        if _primary_limiter is not None:
            with contextlib.suppress(Exception):
                _primary_limiter._client.flushdb()

    def test_trash_purge_invalid_days_logs_warning_and_flashes(self, monkeypatch, tmp_path, caplog):
        client, data_dir = build_client(monkeypatch, tmp_path)

        # Create a trashed item to purge
        folder = data_dir / "purge_me"
        folder.mkdir()
        (folder / "file.jpg").write_bytes(b"fakesize")
        from trash import move_to_trash

        move_to_trash(folder, data_dir)

        # Login first
        login_resp = client.get("/login")
        csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', login_resp.data.decode())
        assert csrf_match, "CSRF token not found in login form"
        login_csrf = csrf_match.group(1)
        auth_resp = client.post(
            "/auth",
            data={"password": "pass123", "next": "/", "csrf_token": login_csrf},
            follow_redirects=False,
        )
        assert auth_resp.status_code == 302

        # Get CSRF token from trash page
        trash_resp = client.get("/trash")
        csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', trash_resp.data.decode())
        assert csrf_match, "CSRF token not found on trash page"
        csrf_token = csrf_match.group(1)

        # Submit invalid days value
        with caplog.at_level("WARNING"):
            response = client.post(
                "/trash/purge",
                data={"csrf_token": csrf_token, "days": "abc"},
                follow_redirects=True,
            )

        assert response.status_code == 200
        # Verify flash message is present in the rendered page
        assert b"Invalid retention period" in response.data
        # Verify a warning was logged about the invalid input
        assert any("Invalid 'days' value" in record.message for record in caplog.records)

    def test_trash_purge_valid_days(self, monkeypatch, tmp_path):
        client, data_dir = build_client(monkeypatch, tmp_path)

        # Create a trashed item
        folder = data_dir / "purge_me2"
        folder.mkdir()
        (folder / "file.jpg").write_bytes(b"fakesize")
        from trash import move_to_trash

        move_to_trash(folder, data_dir)

        # Login first
        login_resp = client.get("/login")
        csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', login_resp.data.decode())
        assert csrf_match, "CSRF token not found in login form"
        login_csrf = csrf_match.group(1)
        auth_resp = client.post(
            "/auth",
            data={"password": "pass123", "next": "/", "csrf_token": login_csrf},
            follow_redirects=False,
        )
        assert auth_resp.status_code == 302

        # Get CSRF token from trash page
        trash_resp = client.get("/trash")
        csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', trash_resp.data.decode())
        assert csrf_match, "CSRF token not found on trash page"
        csrf_token = csrf_match.group(1)

        response = client.post(
            "/trash/purge",
            data={"csrf_token": csrf_token, "days": "7"},
            follow_redirects=False,
        )
        assert response.status_code == 302

    def test_trash_purge_route_survives_failed_entry_in_sweep(self, monkeypatch, tmp_path):
        """A raced non-ENOENT OSError mid-sweep no longer 500s /trash/purge (#597).

        The purge sweep skips (and debug-logs) an entry whose delete raises,
        so the route still redirects instead of erroring while the sibling
        expired entries are purged.
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        td = trash.trash_dir(data_dir)
        old_iso = (trash._utcnow() - timedelta(days=40)).isoformat()

        victim = td / "victim.txt"
        victim.write_text("victim")
        victim.with_name(victim.name + trash.META_SUFFIX).write_text(
            json.dumps({"original": "victim.txt", "deleted_at": old_iso})
        )
        expired = td / "expired.txt"
        expired.write_text("expired")
        expired.with_name(expired.name + trash.META_SUFFIX).write_text(
            json.dumps({"original": "expired.txt", "deleted_at": old_iso})
        )

        original_unlink = trash.Path.unlink

        def racy_unlink(path, *args, **kwargs):
            if path == victim:
                raise OSError(errno.EACCES, "permission denied")
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(trash.Path, "unlink", racy_unlink)

        # Login first
        login_resp = client.get("/login")
        csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', login_resp.data.decode())
        assert csrf_match, "CSRF token not found in login form"
        login_csrf = csrf_match.group(1)
        auth_resp = client.post(
            "/auth",
            data={"password": "pass123", "next": "/", "csrf_token": login_csrf},
            follow_redirects=False,
        )
        assert auth_resp.status_code == 302

        # Get CSRF token from trash page
        trash_resp = client.get("/trash")
        csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', trash_resp.data.decode())
        assert csrf_match, "CSRF token not found on trash page"
        csrf_token = csrf_match.group(1)

        response = client.post(
            "/trash/purge",
            data={"csrf_token": csrf_token, "days": "1"},
            follow_redirects=False,
        )
        assert response.status_code == 302

        assert not expired.exists(), "the sweep must continue past the failed entry"
        assert victim.exists()
        assert victim.with_name(victim.name + trash.META_SUFFIX).exists()

    def test_trash_restore_route_reports_an_unreservable_destination(self, monkeypatch, tmp_path):
        """A restore that cannot reserve its destination reports the reason.

        The route must not report the failure as a rejected request: the
        destination could not be reserved because the file system refused the
        lock, so the item stays in the trash and the user is told why.
        """
        client, data_dir = build_client(monkeypatch, tmp_path)
        td = trash.trash_dir(data_dir)
        item = td / "a.bin"
        item.write_bytes(b"payload")
        item.with_name(item.name + trash.META_SUFFIX).write_text(
            json.dumps({"original": "photo.txt", "deleted_at": "2026-01-01T00:00:00+00:00"})
        )

        def no_locking(*args, **kwargs):
            return None, trash.RESTORE_CLAIM_UNSUPPORTED, "the reservation is unavailable"

        monkeypatch.setattr(trash, "_acquire_restore_claim", no_locking)

        login_resp = client.get("/login")
        csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', login_resp.data.decode())
        assert csrf_match, "CSRF token not found in login form"
        login_csrf = csrf_match.group(1)
        auth_resp = client.post(
            "/auth",
            data={"password": "pass123", "next": "/", "csrf_token": login_csrf},
            follow_redirects=False,
        )
        assert auth_resp.status_code == 302

        trash_resp = client.get("/trash")
        csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', trash_resp.data.decode())
        assert csrf_match, "CSRF token not found on trash page"
        csrf_token = csrf_match.group(1)

        response = client.post(
            "/trash/restore/a.bin",
            data={"csrf_token": csrf_token},
            follow_redirects=True,
        )

        assert response.status_code == 200
        assert b"the destination could not be reserved" in response.data
        # The item is still in the trash and its payload is untouched.
        assert [t["name"] for t in list_trash(data_dir)] == ["a.bin"]
