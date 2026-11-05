"""Regression tests for issue #633: TagStore connection reuse.

TagStore used to open a fresh sqlite3.connect() per operation and re-run the
WAL PRAGMAs and schema DDL on every connect — including reads. These tests pin
the replacement behavior: one cached connection per thread, reused across
steady-state operations, with a fallback reconnect when the cached connection
is invalidated out-of-band.
"""

import sqlite3
import threading

from tag_store import TagStore


def test_tag_store_reuses_one_connection_across_steady_state_operations(tmp_path, monkeypatch):
    """Repeated tag operations must not open a new SQLite connection per
    operation: the per-thread connection is reused, so the WAL PRAGMAs and
    schema DDL run only on the first open."""
    connects: list[str] = []
    real_connect = sqlite3.connect

    def counting_connect(path, *args, **kwargs):
        connects.append(str(path))
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counting_connect)

    store = TagStore(tmp_path / "tags.db")
    store.add_tags("sample.png", ["alpha", "beta"])
    steady_state_connects = len(connects)
    assert steady_state_connects == 1, "the first operation opens exactly one connection"

    store.get_tags("sample.png")
    store.remove_tags("sample.png", ["alpha"])
    store.get_tags_for_paths(["sample.png", "other.png"])
    store.add_tags("sample.png", ["gamma"])

    assert len(connects) == steady_state_connects, (
        "steady-state tag operations must reuse the cached connection, "
        f"but {len(connects) - steady_state_connects} new connection(s) were opened"
    )
    assert store.get_tags("sample.png") == ["beta", "gamma"]


def test_tag_store_connections_are_per_thread(tmp_path):
    """Each thread gets its own cached connection (check_same_thread stays at
    its safe default) and writes from one thread are visible to another."""
    store = TagStore(tmp_path / "tags.db")
    main_thread_connection = store._thread_connection()
    seen: dict[str, object] = {}

    def worker():
        seen["connection"] = store._thread_connection()
        store.add_tags("b.png", ["from-thread"])

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert seen["connection"] is not main_thread_connection
    assert store.get_tags("b.png") == ["from-thread"]


def test_tag_store_recovers_when_cached_connection_is_invalidated(tmp_path):
    """A cached connection whose schema vanished out-of-band must trigger one
    fallback reconnect (re-running the schema DDL) instead of failing the
    read with sqlite3.OperationalError."""
    store = TagStore(tmp_path / "tags.db")
    store.add_tags("sample.png", ["alpha"])
    assert store.get_tags("sample.png") == ["alpha"]

    # Simulate out-of-band invalidation: the schema disappears underneath the
    # cached connection (e.g. another process recreated the database file).
    with store._connect() as connection:
        connection.execute("DROP TABLE media_tags")

    assert store.get_tags("sample.png") == []
