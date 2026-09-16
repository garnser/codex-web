from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


class SQLiteStateStore:
    """Small transactional document store for codex-web runtime state."""

    SCHEMA_VERSION = 1

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self._initialize()

    def _secure_database_files(self) -> None:
        for candidate in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            try:
                os.chmod(candidate, 0o600)
            except FileNotFoundError:
                continue

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        self._secure_database_files()
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        self._secure_database_files()
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Provide transactional use while always closing the DB handle."""
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            self._secure_database_files()
            connection.close()
            self._secure_database_files()

    @staticmethod
    def _decode(row: tuple[Any, ...] | None, default: Any = None) -> Any:
        if row is None:
            return default
        return json.loads(row[0])

    @staticmethod
    def _serialized(payload: Any) -> str:
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    def _upsert(self, connection: sqlite3.Connection, namespace: str, payload: Any) -> None:
        connection.execute(
            """
            INSERT INTO state_documents(namespace, payload, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(namespace) DO UPDATE SET
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            (namespace, self._serialized(payload), time.time()),
        )

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS state_documents (
                    namespace TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS state_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            row = connection.execute(
                "SELECT value FROM state_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO state_metadata(key, value, updated_at) VALUES ('schema_version', ?, ?)",
                    (str(self.SCHEMA_VERSION), time.time()),
                )
            else:
                version = int(row[0])
                if version > self.SCHEMA_VERSION:
                    raise RuntimeError(
                        f"State database schema version {version} is newer than supported version {self.SCHEMA_VERSION}"
                    )

    def schema_version(self) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM state_metadata WHERE key = 'schema_version'"
            ).fetchone()
        return int(row[0]) if row else 0

    def status(self) -> dict[str, Any]:
        with self._connection() as connection:
            integrity_row = connection.execute("PRAGMA quick_check").fetchone()
            journal_row = connection.execute("PRAGMA journal_mode").fetchone()
            document_count = connection.execute("SELECT COUNT(*) FROM state_documents").fetchone()[0]
            updated_row = connection.execute("SELECT MAX(updated_at) FROM state_documents").fetchone()
        integrity = str(integrity_row[0]) if integrity_row else "unknown"
        return {
            "backend": "sqlite",
            "schemaVersion": self.schema_version(),
            "supportedSchemaVersion": self.SCHEMA_VERSION,
            "journalMode": str(journal_row[0]) if journal_row else "unknown",
            "integrity": integrity,
            "ok": integrity.lower() == "ok",
            "documents": int(document_count or 0),
            "lastDocumentUpdateAt": float(updated_row[0]) if updated_row and updated_row[0] is not None else None,
        }

    def checkpoint(self, *, truncate: bool = False) -> dict[str, int]:
        mode = "TRUNCATE" if truncate else "PASSIVE"
        with self._connection() as connection:
            row = connection.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        busy, log_frames, checkpointed_frames = row or (0, 0, 0)
        return {
            "busy": int(busy),
            "logFrames": int(log_frames),
            "checkpointedFrames": int(checkpointed_frames),
        }

    def backup_to(self, destination: Path) -> Path:
        destination = destination.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = self._connect()
        target = sqlite3.connect(destination, timeout=5.0)
        try:
            with target:
                source.backup(target)
        finally:
            target.close()
            source.close()
            self._secure_database_files()
        os.chmod(destination, 0o600)
        return destination

    def get(self, namespace: str) -> Any | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM state_documents WHERE namespace = ?",
                (namespace,),
            ).fetchone()
        return self._decode(row)

    def put(self, namespace: str, payload: Any) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._upsert(connection, namespace, payload)

    def update(
        self,
        namespace: str,
        updater: Callable[[Any], Any],
        *,
        default: Any,
    ) -> Any:
        """Atomically read, transform and replace one namespace document.

        `BEGIN IMMEDIATE` serializes competing writers before the read so an
        updater always sees the latest committed value. This lets repositories
        merge only their local delta instead of overwriting unrelated changes
        made by another worker between load() and save().
        """
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM state_documents WHERE namespace = ?",
                (namespace,),
            ).fetchone()
            current = self._decode(row, default)
            updated = updater(current)
            self._upsert(connection, namespace, updated)
            return updated

    def contains(self, namespace: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM state_documents WHERE namespace = ?",
                (namespace,),
            ).fetchone()
        return row is not None
