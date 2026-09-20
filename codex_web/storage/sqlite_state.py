from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from codex_web.storage.state_store import (
    OperationTimingMetrics,
    parse_state_record_storage_key,
    state_record_marker,
    state_record_prefix,
    state_record_storage_key,
)


class SQLiteStateStore:
    """Small transactional document store for codex-web runtime state."""

    SCHEMA_VERSION = 1

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self._keyed_mutation_metrics = OperationTimingMetrics()
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

    @staticmethod
    def _record_collection_exists_in_connection(
        connection: sqlite3.Connection,
        namespace: str,
    ) -> bool:
        row = connection.execute(
            "SELECT 1 FROM state_documents WHERE namespace = ?",
            (state_record_marker(namespace),),
        ).fetchone()
        return row is not None

    @staticmethod
    def _record_items_in_connection(
        connection: sqlite3.Connection,
        namespace: str,
    ) -> dict[str, Any]:
        prefix = f"{state_record_prefix(namespace)}k/"
        rows = connection.execute(
            """
            SELECT namespace, payload
            FROM state_documents
            WHERE substr(namespace, 1, length(?)) = ?
            ORDER BY namespace
            """,
            (prefix, prefix),
        ).fetchall()
        result: dict[str, Any] = {}
        for storage_namespace, payload in rows:
            parsed = parse_state_record_storage_key(str(storage_namespace))
            if parsed is None or parsed[1] is None:
                continue
            result[parsed[1]] = json.loads(payload)
        return result

    def _replace_records_in_connection(
        self,
        connection: sqlite3.Connection,
        namespace: str,
        records: dict[str, Any],
    ) -> None:
        prefix = state_record_prefix(namespace)
        connection.execute(
            "DELETE FROM state_documents WHERE substr(namespace, 1, length(?)) = ?",
            (prefix, prefix),
        )
        connection.execute(
            "DELETE FROM state_documents WHERE namespace = ?",
            (namespace,),
        )
        self._upsert(
            connection,
            state_record_marker(namespace),
            {"schemaVersion": 1},
        )
        for key, payload in records.items():
            self._upsert(
                connection,
                state_record_storage_key(namespace, str(key)),
                payload,
            )

    def _ensure_record_collection_in_connection(
        self,
        connection: sqlite3.Connection,
        namespace: str,
    ) -> None:
        if self._record_collection_exists_in_connection(connection, namespace):
            return
        row = connection.execute(
            "SELECT payload FROM state_documents WHERE namespace = ?",
            (namespace,),
        ).fetchone()
        if row is None:
            self._upsert(
                connection,
                state_record_marker(namespace),
                {"schemaVersion": 1},
            )
            return
        payload = self._decode(row)
        if not isinstance(payload, dict):
            raise TypeError(
                f"state namespace {namespace!r} is not a keyed mapping"
            )
        self._replace_records_in_connection(connection, namespace, payload)

    def record_collection_exists(self, namespace: str) -> bool:
        with self._connection() as connection:
            return self._record_collection_exists_in_connection(
                connection,
                namespace,
            )

    def record_get(self, namespace: str, key: str) -> Any | None:
        with self._connection() as connection:
            if self._record_collection_exists_in_connection(
                connection,
                namespace,
            ):
                row = connection.execute(
                    "SELECT payload FROM state_documents WHERE namespace = ?",
                    (state_record_storage_key(namespace, key),),
                ).fetchone()
                return self._decode(row)
            row = connection.execute(
                "SELECT payload FROM state_documents WHERE namespace = ?",
                (namespace,),
            ).fetchone()
            payload = self._decode(row)
            if isinstance(payload, dict):
                return payload.get(key)
            return None

    def record_items(self, namespace: str) -> dict[str, Any]:
        with self._connection() as connection:
            if self._record_collection_exists_in_connection(
                connection,
                namespace,
            ):
                return self._record_items_in_connection(
                    connection,
                    namespace,
                )
            row = connection.execute(
                "SELECT payload FROM state_documents WHERE namespace = ?",
                (namespace,),
            ).fetchone()
            payload = self._decode(row)
            return dict(payload) if isinstance(payload, dict) else {}

    def record_page(
        self,
        namespace: str,
        *,
        key_prefix: str | None = None,
        after: str | None = None,
        limit: int = 100,
    ) -> tuple[dict[str, Any], str | None]:
        page_size = max(1, min(int(limit), 1000))
        with self._connection() as connection:
            if not self._record_collection_exists_in_connection(
                connection,
                namespace,
            ):
                connection.execute("BEGIN IMMEDIATE")
                self._ensure_record_collection_in_connection(
                    connection,
                    namespace,
                )
            lower = (
                state_record_storage_key(namespace, key_prefix)
                if key_prefix is not None
                else f"{state_record_prefix(namespace)}k/"
            )
            upper = f"{lower}\uffff"
            after_storage = (
                state_record_storage_key(namespace, after)
                if after is not None
                else lower
            )
            rows = connection.execute(
                """
                SELECT namespace, payload
                FROM state_documents
                WHERE namespace >= ?
                  AND namespace < ?
                  AND namespace > ?
                ORDER BY namespace
                LIMIT ?
                """,
                (lower, upper, after_storage, page_size + 1),
            ).fetchall()

        page: dict[str, Any] = {}
        for storage_namespace, payload in rows[:page_size]:
            parsed = parse_state_record_storage_key(
                str(storage_namespace)
            )
            if parsed is None or parsed[1] is None:
                continue
            page[parsed[1]] = json.loads(payload)
        next_cursor = (
            next(reversed(page))
            if len(rows) > page_size and page
            else None
        )
        return page, next_cursor

    def record_apply(
        self,
        namespace: str,
        *,
        upserts: dict[str, Any],
        deletes: tuple[str, ...] = (),
    ) -> None:
        started = time.perf_counter()
        success = False
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._ensure_record_collection_in_connection(
                    connection,
                    namespace,
                )
                for key in dict.fromkeys(str(item) for item in deletes):
                    connection.execute(
                        "DELETE FROM state_documents WHERE namespace = ?",
                        (state_record_storage_key(namespace, key),),
                    )
                for key, payload in upserts.items():
                    self._upsert(
                        connection,
                        state_record_storage_key(namespace, str(key)),
                        payload,
                    )
            success = True
        finally:
            self._keyed_mutation_metrics.observe(
                time.perf_counter() - started,
                success=success,
            )

    def record_update(
        self,
        namespace: str,
        key: str,
        updater: Callable[[Any], Any],
        *,
        default: Any,
    ) -> Any:
        started = time.perf_counter()
        success = False
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._ensure_record_collection_in_connection(
                    connection,
                    namespace,
                )
                storage_key = state_record_storage_key(namespace, str(key))
                row = connection.execute(
                    "SELECT payload FROM state_documents WHERE namespace = ?",
                    (storage_key,),
                ).fetchone()
                current = self._decode(row, default)
                updated = updater(current)
                if updated is None:
                    connection.execute(
                        "DELETE FROM state_documents WHERE namespace = ?",
                        (storage_key,),
                    )
                else:
                    self._upsert(connection, storage_key, updated)
            success = True
            return updated
        finally:
            self._keyed_mutation_metrics.observe(
                time.perf_counter() - started,
                success=success,
            )

    def record_replace(
        self,
        namespace: str,
        records: dict[str, Any],
    ) -> None:
        started = time.perf_counter()
        success = False
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._replace_records_in_connection(
                    connection,
                    namespace,
                    records,
                )
            success = True
        finally:
            self._keyed_mutation_metrics.observe(
                time.perf_counter() - started,
                success=success,
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
            document_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM state_documents
                WHERE namespace NOT LIKE ?
                   OR namespace LIKE ?
                """,
                ("__codex_records__/%", "__codex_records__/%/__meta__"),
            ).fetchone()[0]
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
            "keyedMutationMetrics": self._keyed_mutation_metrics.snapshot(),
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
            if row is not None:
                return self._decode(row)
            if self._record_collection_exists_in_connection(
                connection,
                namespace,
            ):
                return self._record_items_in_connection(
                    connection,
                    namespace,
                )
            return None

    def put(self, namespace: str, payload: Any) -> None:
        if (
            isinstance(payload, dict)
            and self.record_collection_exists(namespace)
        ):
            self.record_replace(namespace, payload)
            return
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._record_collection_exists_in_connection(
                connection,
                namespace,
            ):
                prefix = state_record_prefix(namespace)
                connection.execute(
                    "DELETE FROM state_documents WHERE substr(namespace, 1, length(?)) = ?",
                    (prefix, prefix),
                )
            self._upsert(connection, namespace, payload)

    def update(
        self,
        namespace: str,
        updater: Callable[[Any], Any],
        *,
        default: Any,
    ) -> Any:
        """Atomically transform one logical namespace.

        Record-backed mappings remain physically keyed while compatibility
        callers can continue to use the document-level update contract.
        """
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._record_collection_exists_in_connection(
                connection,
                namespace,
            ):
                current = self._record_items_in_connection(
                    connection,
                    namespace,
                )
                updated = updater(current)
                if not isinstance(updated, dict):
                    raise TypeError(
                        "record-backed namespace updates must return a mapping"
                    )
                self._replace_records_in_connection(
                    connection,
                    namespace,
                    updated,
                )
                return updated
            row = connection.execute(
                "SELECT payload FROM state_documents WHERE namespace = ?",
                (namespace,),
            ).fetchone()
            current = self._decode(row, default)
            updated = updater(current)
            self._upsert(connection, namespace, updated)
            return updated

    def update_many(
        self,
        defaults: dict[str, Any],
        updater: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        """Atomically read, transform, and replace multiple namespace documents.

        This is the cross-domain transaction primitive for operations whose
        safety invariant spans canonical documents, such as consuming an
        ApprovalRequest in the same commit as the guarded mutation. All
        namespaces are read after one BEGIN IMMEDIATE writer lock is acquired.
        """

        namespaces = tuple(dict.fromkeys(defaults))
        if not namespaces:
            raise ValueError("update_many requires at least one namespace")

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current: dict[str, Any] = {}
            record_backed: set[str] = set()
            for namespace in namespaces:
                if self._record_collection_exists_in_connection(
                    connection,
                    namespace,
                ):
                    record_backed.add(namespace)
                    current[namespace] = self._record_items_in_connection(
                        connection,
                        namespace,
                    )
                    continue
                row = connection.execute(
                    "SELECT payload FROM state_documents WHERE namespace = ?",
                    (namespace,),
                ).fetchone()
                current[namespace] = self._decode(row, defaults[namespace])

            updated = updater(current)
            if set(updated) != set(namespaces):
                raise ValueError(
                    "update_many updater must return exactly the requested namespaces"
                )
            for namespace in namespaces:
                if namespace in record_backed:
                    if not isinstance(updated[namespace], dict):
                        raise TypeError(
                            "record-backed namespace updates must return a mapping"
                        )
                    self._replace_records_in_connection(
                        connection,
                        namespace,
                        updated[namespace],
                    )
                else:
                    self._upsert(connection, namespace, updated[namespace])
            return updated

    def namespace_revision(self, namespace: str) -> float | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT updated_at FROM state_documents WHERE namespace = ?",
                (namespace,),
            ).fetchone()
            if row is not None:
                return float(row[0])
            marker = state_record_marker(namespace)
            row = connection.execute(
                "SELECT updated_at FROM state_documents WHERE namespace = ?",
                (marker,),
            ).fetchone()
            if row is None:
                return None
            prefix = state_record_prefix(namespace)
            latest = connection.execute(
                """
                SELECT MAX(updated_at)
                FROM state_documents
                WHERE substr(namespace, 1, length(?)) = ?
                """,
                (prefix, prefix),
            ).fetchone()
            return (
                float(latest[0])
                if latest and latest[0] is not None
                else float(row[0])
            )

    def contains(self, namespace: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM state_documents WHERE namespace = ?",
                (namespace,),
            ).fetchone()
            return (
                row is not None
                or self._record_collection_exists_in_connection(
                    connection,
                    namespace,
                )
            )

    def delete(self, namespace: str) -> bool:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM state_documents WHERE namespace = ?",
                (namespace,),
            )
            prefix = state_record_prefix(namespace)
            record_cursor = connection.execute(
                "DELETE FROM state_documents WHERE substr(namespace, 1, length(?)) = ?",
                (prefix, prefix),
            )
            return bool(cursor.rowcount or record_cursor.rowcount)

    def documents(self) -> dict[str, Any]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT namespace, payload FROM state_documents ORDER BY namespace"
            ).fetchall()
        documents: dict[str, Any] = {}
        records: dict[str, dict[str, Any]] = {}
        record_collections: set[str] = set()
        for storage_namespace, payload in rows:
            name = str(storage_namespace)
            parsed = parse_state_record_storage_key(name)
            if parsed is None:
                documents[name] = json.loads(payload)
                continue
            logical_namespace, key = parsed
            record_collections.add(logical_namespace)
            if key is not None:
                records.setdefault(logical_namespace, {})[key] = json.loads(
                    payload
                )
        for namespace in record_collections:
            if namespace not in documents:
                documents[namespace] = records.get(namespace, {})
        return documents
