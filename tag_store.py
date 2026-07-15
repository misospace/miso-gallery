"""Persistent SQLite-backed tag storage."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence


class TagStore:
    """Store tags for gallery media using gallery-relative paths."""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.database_path), timeout=30)
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS media_tags (
                    media_path TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    PRIMARY KEY (media_path, tag)
                )
                """
            )
            yield connection
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _normalize_tags(tags: Sequence[object]) -> list[str]:
        return sorted({str(tag).strip() for tag in tags if str(tag).strip()})

    def get_tags(self, media_path: str) -> list[str]:
        """Return the persisted tags for one media path."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT tag FROM media_tags WHERE media_path = ? ORDER BY tag",
                (media_path,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def get_tags_for_paths(self, media_paths: Sequence[str]) -> dict[str, list[str]]:
        """Return tags for several paths without opening one connection per image."""
        unique_paths = list(dict.fromkeys(media_paths))
        result = {media_path: [] for media_path in unique_paths}
        if not unique_paths:
            return result

        # Stay below SQLite's default bind-variable limit on older versions.
        chunk_size = 500
        with self._connect() as connection:
            for offset in range(0, len(unique_paths), chunk_size):
                chunk = unique_paths[offset : offset + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT media_path, tag FROM media_tags WHERE media_path IN ({placeholders}) ORDER BY media_path, tag",
                    chunk,
                ).fetchall()
                for media_path, tag in rows:
                    result[str(media_path)].append(str(tag))
        return result

    def add_tags(self, media_path: str, tags: Sequence[object]) -> list[str]:
        """Add tags idempotently and return all current tags for the media."""
        normalized = self._normalize_tags(tags)
        if normalized:
            with self._connect() as connection:
                connection.executemany(
                    "INSERT OR IGNORE INTO media_tags (media_path, tag) VALUES (?, ?)",
                    [(media_path, tag) for tag in normalized],
                )
        return self.get_tags(media_path)

    def remove_tags(self, media_path: str, tags: Sequence[object]) -> list[str]:
        """Remove tags idempotently and return all current tags for the media."""
        normalized = self._normalize_tags(tags)
        if normalized:
            with self._connect() as connection:
                connection.executemany(
                    "DELETE FROM media_tags WHERE media_path = ? AND tag = ?",
                    [(media_path, tag) for tag in normalized],
                )
        return self.get_tags(media_path)
