"""Persistent SQLite-backed tag storage."""

import sqlite3
import threading
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Callable, Iterator, Sequence, TypeVar

_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_tags (
    media_path TEXT NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY (media_path, tag)
)
"""

Result = TypeVar("Result")


class TagStore:
    """Store tags for gallery media using gallery-relative paths."""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        # sqlite3 connections are not shareable across threads with the default
        # check_same_thread=True, and Flask/Gunicorn serve requests on a thread
        # pool, so the connection cache is keyed per thread instead of holding
        # one process-wide connection behind a lock (issue #633).
        self._local = threading.local()

    def _open_connection(self) -> sqlite3.Connection:
        """Open a new connection, applying the PRAGMAs and schema DDL once."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.database_path), timeout=30)
        try:
            # Enable WAL mode for concurrent reader/writer access under multi-worker load.
            # synchronous=NORMAL reduces fsync overhead while maintaining durability.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute(_SCHEMA)
            connection.commit()
        except Exception:
            connection.close()
            raise
        return connection

    def _thread_connection(self) -> sqlite3.Connection:
        """Return the calling thread's cached connection, opening it on first use.

        Steady-state tag reads and writes reuse this connection instead of
        opening a fresh one per operation (issue #633).
        """
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = self._open_connection()
            self._local.connection = connection
        return connection

    def _drop_connection(self) -> None:
        """Close and forget the calling thread's cached connection, if any."""
        connection = getattr(self._local, "connection", None)
        self._local.connection = None
        if connection is not None:
            with suppress(sqlite3.Error):
                connection.close()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield the calling thread's cached connection, committing on success.

        The WAL PRAGMAs and schema DDL run only when the connection is first
        opened, so repeated _connect() calls reuse the cached connection
        without re-running them (issue #633).
        """
        connection = self._thread_connection()
        try:
            yield connection
            connection.commit()
        except BaseException:
            with suppress(sqlite3.Error):
                connection.rollback()
            raise

    def _run(self, operation: Callable[[sqlite3.Connection], Result]) -> Result:
        """Run an operation against the cached connection.

        If the cached connection has been invalidated out-of-band (e.g. the
        database file was removed or replaced underneath the store), the
        attempt raises sqlite3.OperationalError; drop the cached connection
        and retry the operation once on a freshly opened one.
        """
        try:
            with self._connect() as connection:
                result = operation(connection)
        except sqlite3.OperationalError:
            self._drop_connection()
            with self._connect() as connection:
                result = operation(connection)
        return result

    @staticmethod
    def _normalize_tags(tags: Sequence[object]) -> list[str]:
        return sorted({str(tag).strip() for tag in tags if str(tag).strip()})

    def get_tags(self, media_path: str) -> list[str]:
        """Return the persisted tags for one media path."""
        rows = self._run(
            lambda connection: connection.execute(
                "SELECT tag FROM media_tags WHERE media_path = ? ORDER BY tag",
                (media_path,),
            ).fetchall()
        )
        return [str(row[0]) for row in rows]

    def get_tags_for_paths(self, media_paths: Sequence[str]) -> dict[str, list[str]]:
        """Return tags for several paths without opening one connection per image."""
        unique_paths = list(dict.fromkeys(media_paths))
        result = {media_path: [] for media_path in unique_paths}
        if not unique_paths:
            return result

        # Stay below SQLite's default bind-variable limit on older versions.
        chunk_size = 500

        def query_chunks(connection: sqlite3.Connection) -> list[tuple]:
            rows: list[tuple] = []
            for offset in range(0, len(unique_paths), chunk_size):
                chunk = unique_paths[offset : offset + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                rows.extend(
                    connection.execute(
                        f"SELECT media_path, tag FROM media_tags WHERE media_path IN ({placeholders}) ORDER BY media_path, tag",
                        chunk,
                    ).fetchall()
                )
            return rows

        rows = self._run(query_chunks)
        for media_path, tag in rows:
            result[str(media_path)].append(str(tag))
        return result

    def add_tags(self, media_path: str, tags: Sequence[object]) -> list[str]:
        """Add tags idempotently and return all current tags for the media."""
        normalized = self._normalize_tags(tags)
        if normalized:
            self._run(
                lambda connection: connection.executemany(
                    "INSERT OR IGNORE INTO media_tags (media_path, tag) VALUES (?, ?)",
                    [(media_path, tag) for tag in normalized],
                )
            )
        return self.get_tags(media_path)

    def remove_tags(self, media_path: str, tags: Sequence[object]) -> list[str]:
        """Remove tags idempotently and return all current tags for the media."""
        normalized = self._normalize_tags(tags)
        if normalized:
            self._run(
                lambda connection: connection.executemany(
                    "DELETE FROM media_tags WHERE media_path = ? AND tag = ?",
                    [(media_path, tag) for tag in normalized],
                )
            )
        return self.get_tags(media_path)
