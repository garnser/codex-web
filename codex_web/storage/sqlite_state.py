from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class SQLiteStateStore:
    """Small transactional document store for codex-web runtime state."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Provide transactional use while always closing the DB handle.

        `sqlite3.Connection.__exit__` commits or rolls back but does not close the
        connection. Keeping close ownership here avoids leaking file descriptors
        across the many short state-store reads performed by the runtime.
        """
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

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

    def get(self, namespace: str) -> Any | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM state_documents WHERE namespace = ?",
                (namespace,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def put(self, namespace: str, payload: Any) -> None:
        serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO state_documents(namespace, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(namespace) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (namespace, serialized, time.time()),
            )
            connection.commit()

    def contains(self, namespace: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM state_documents WHERE namespace = ?",
                (namespace,),
            ).fetchone()
        return row is not None
