from __future__ import annotations

import json
import math
import os
import sqlite3
import stat
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from codex_web.storage.state_store import (
    storage_operation_budget, check_operation_budget, operation_lock,
    OperationTimingMetrics,
    parse_state_record_storage_key,
    state_record_marker,
    state_record_prefix,
    state_record_storage_key,
    record_projection_fields,
    project_record_fields,
)


class SQLiteStateStore:
    """Small transactional document store for codex-web runtime state."""

    SCHEMA_VERSION = 1

    def __init__(self, path: Path, *, initialize: bool = True) -> None:
        self.path = path
        self._existing_only = not initialize
        if initialize:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self.path.parent, 0o700)
        elif not self.path.is_file():
            raise RuntimeError('Existing canonical state database is required')
        self._keyed_mutation_metrics = OperationTimingMetrics()
        self._write_lock = threading.RLock()
        if initialize:
            self._initialize()
        else:
            # A helper may inspect existing identity before it has authority;
            # it must never create schema, metadata or a replacement file.
            with closing(sqlite3.connect(self.path.absolute().as_uri() + '?mode=ro', uri=True)) as connection:
                row = connection.execute("SELECT value FROM state_metadata WHERE key='schema_version'").fetchone()
                connection.execute('SELECT namespace FROM state_documents LIMIT 0')
            if row is None or int(row[0]) != self.SCHEMA_VERSION:
                raise RuntimeError('Existing canonical state schema is unsupported')

    def _secure_database_files(self) -> None:
        for candidate in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            try:
                # State operations open short-lived SQLite connections. Avoid
                # issuing chmod for every open/close when the file is already
                # private: on journaling filesystems that needless metadata
                # churn can serialize otherwise independent state workers.
                if stat.S_IMODE(candidate.stat().st_mode) != 0o600:
                    os.chmod(candidate, 0o600)
            except FileNotFoundError:
                continue

    def _connect(self) -> sqlite3.Connection:
        check_operation_budget()
        budget = storage_operation_budget.get()
        timeout = min(5.0, budget.remaining()) if budget else 5.0
        connection = (sqlite3.connect(self.path.absolute().as_uri() + '?mode=rw', uri=True, timeout=timeout)
                      if getattr(self, '_existing_only', False) else sqlite3.connect(self.path, timeout=timeout))
        if budget:
            connection.set_progress_handler(lambda: int(budget.cancelled.is_set() or time.monotonic() >= budget.deadline), 1000)
        try:
            self._secure_database_files()
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA busy_timeout={max(1, int(timeout * 1000))}")
            self._secure_database_files()
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Provide transactional use while always closing the DB handle."""
        connection = self._connect()
        try:
            with connection:
                yield connection
                check_operation_budget()
        finally:
            self._secure_database_files()
            connection.close()
            self._secure_database_files()

    @contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        """Serialize this process's SQLite write transactions.

        Runtime event listeners and API handlers use worker threads, so they
        can otherwise contend for the single WAL writer and exhaust SQLite's
        busy timeout. Reads retain their normal concurrent WAL behavior.
        """
        with operation_lock(self._write_lock):
            with self._connection() as connection:
                yield connection

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
        with self._write_connection() as connection:
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
        previous = connection.execute(
            "SELECT MAX(updated_at) FROM state_documents WHERE namespace = ? OR (namespace >= ? AND namespace < ?)",
            (namespace, prefix, prefix + "\uffff"),
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM state_documents WHERE namespace >= ? AND namespace < ?",
            (prefix, prefix + "\uffff"),
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
        self._touch_record_revision(connection, namespace, minimum=previous or 0.0)

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

    def record_collections_exist(self, namespaces: tuple[str, ...]) -> dict[str, bool]:
        """Probe current collection markers in one bounded database query."""
        if len(namespaces) > 1000:
            raise ValueError("collection batch exceeds 1000 namespaces")
        namespaces = tuple(dict.fromkeys(namespaces))
        if not namespaces:
            return {}
        markers = tuple(state_record_marker(name) for name in namespaces)
        placeholders = ",".join("?" for _ in markers)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT namespace FROM state_documents WHERE namespace IN ({placeholders})",
                markers,
            ).fetchall()
        present = {row[0] for row in rows}
        return {name: marker in present for name, marker in zip(namespaces, markers)}

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

    def record_get_many(self, namespace: str, keys: tuple[str, ...]) -> dict[str, Any]:
        if len(keys) > 1000:
            raise ValueError("record batch exceeds 1000 keys")
        keys = tuple(dict.fromkeys(str(key) for key in keys))
        if not keys:
            return {}
        with self._connection() as connection:
            if not self._record_collection_exists_in_connection(connection, namespace):
                row = connection.execute(
                    "SELECT payload FROM state_documents WHERE namespace = ?", (namespace,),
                ).fetchone()
                payload = self._decode(row)
                return {key: payload[key] for key in keys if key in payload} if isinstance(payload, dict) else {}
            storage_keys = {state_record_storage_key(namespace, key): key for key in keys}
            placeholders = ",".join("?" for _ in storage_keys)
            rows = connection.execute(
                f"SELECT namespace, payload FROM state_documents WHERE namespace IN ({placeholders})",
                tuple(storage_keys),
            ).fetchall()
            return {storage_keys[row[0]]: self._decode((row[1],)) for row in rows}

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

    def record_count(self, namespace: str) -> int:
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
            prefix = f"{state_record_prefix(namespace)}k/"
            row = connection.execute(
                """
                SELECT COUNT(*)
                FROM state_documents
                WHERE substr(namespace, 1, length(?)) = ?
                """,
                (prefix, prefix),
            ).fetchone()
        return int(row[0] or 0) if row else 0

    def record_page(
        self,
        namespace: str,
        *,
        key_prefix: str | None = None,
        after: str | None = None,
        limit: int = 100,
    ) -> tuple[dict[str, Any], str | None]:
        return self._record_page(namespace, key_prefix=key_prefix, after=after, limit=limit)

    def record_page_projected(
        self, namespace: str, *, fields: tuple[str, ...],
        key_prefix: str | None = None, after: str | None = None, limit: int = 100,
    ) -> tuple[dict[str, Any], str | None]:
        return self._record_page(namespace, key_prefix=key_prefix, after=after,
            limit=limit, fields=record_projection_fields(fields))

    def _record_page(
        self, namespace: str, *, key_prefix: str | None, after: str | None,
        limit: int, fields: tuple[str, ...] | None = None,
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
            projection = "payload"
            parameters: tuple[Any, ...] = ()
            if fields is not None:
                slots = ",".join("?" for _ in fields)
                projection = f"""CASE WHEN json_valid(payload) = 0 THEN payload
                    WHEN json_type(payload) = 'object' THEN
                    (SELECT json_group_object(field.key,
                        CASE field.type
                            WHEN 'true' THEN json('true')
                            WHEN 'false' THEN json('false')
                            ELSE json_extract(state_documents.payload, '$.' || field.key)
                        END)
                     FROM json_each(state_documents.payload) AS field
                     WHERE field.key IN ({slots})) ELSE payload END"""
                parameters = fields
            rows = connection.execute(
                f"""
                SELECT namespace, {projection}
                FROM state_documents
                WHERE namespace >= ?
                  AND namespace < ?
                  AND namespace > ?
                ORDER BY namespace
                LIMIT ?
                """,
                (*parameters, lower, upper, after_storage, page_size + 1),
            ).fetchall()

        page: dict[str, Any] = {}
        for storage_namespace, payload in rows[:page_size]:
            parsed = parse_state_record_storage_key(
                str(storage_namespace)
            )
            if parsed is None or parsed[1] is None:
                continue
            value = json.loads(payload)
            page[parsed[1]] = project_record_fields(value, fields) if fields is not None else value
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
            with self._write_connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._ensure_record_collection_in_connection(
                    connection,
                    namespace,
                )
                changed = bool(upserts)
                deleted_revision = 0.0
                for key in dict.fromkeys(str(item) for item in deletes):
                    deleted = connection.execute(
                        "DELETE FROM state_documents WHERE namespace = ? RETURNING updated_at",
                        (state_record_storage_key(namespace, key),),
                    ).fetchone()
                    if deleted is not None:
                        changed = True
                        deleted_revision = max(deleted_revision, deleted[0])
                for key, payload in upserts.items():
                    self._upsert(
                        connection,
                        state_record_storage_key(namespace, str(key)),
                        payload,
                    )
                if changed:
                    self._touch_record_revision(connection, namespace, minimum=deleted_revision)
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
            with self._write_connection() as connection:
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
                deleted_revision = 0.0
                if updated is None:
                    deleted = connection.execute(
                        "DELETE FROM state_documents WHERE namespace = ? RETURNING updated_at",
                        (storage_key,),
                    ).fetchone()
                    changed = deleted is not None
                    deleted_revision = deleted[0] if deleted is not None else 0.0
                else:
                    self._upsert(connection, storage_key, updated)
                    changed = True
                if changed:
                    self._touch_record_revision(connection, namespace, minimum=deleted_revision)
            success = True
            return updated
        finally:
            self._keyed_mutation_metrics.observe(
                time.perf_counter() - started,
                success=success,
            )

    def record_mutate_many(self, selected, updater):
        """Inspect bounded rows across canonical collections under one writer lock.

        Dynamic output keys (outbox indexes and receipts) share the same lock;
        unselected records are neither decoded nor rewritten. This serializes
        with both record_mutate and cross-domain update_many.
        """
        if not selected:
            raise ValueError("record_mutate_many requires selected namespaces")
        started = time.perf_counter()
        success = False
        try:
            with self._write_connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = {}
                for namespace, keys in selected.items():
                    if None in keys:
                        if keys != (None,) or self._record_collection_exists_in_connection(connection, namespace):
                            raise ValueError("document selector must name a single non-record namespace")
                        row = connection.execute("SELECT payload FROM state_documents WHERE namespace = ?", (namespace,)).fetchone()
                        current[namespace] = {None: self._decode(row)}
                        continue
                    self._ensure_record_collection_in_connection(connection, namespace)
                    current[namespace] = {}
                    for key in dict.fromkeys(keys):
                        row = connection.execute("SELECT payload FROM state_documents WHERE namespace = ?",
                            (state_record_storage_key(namespace, str(key)),)).fetchone()
                        current[namespace][key] = self._decode(row)
                changes = updater(current)
                if not isinstance(changes, dict) or set(changes) - set(selected):
                    raise TypeError("record_mutate_many must return deltas for selected collections")
                for namespace, delta in changes.items():
                    if not isinstance(delta, dict):
                        raise TypeError("collection delta must be a mapping")
                    if selected[namespace] == (None,):
                        if set(delta) != {None}:
                            raise ValueError("document delta must contain exactly its None row")
                        if delta[None] is None:
                            connection.execute("DELETE FROM state_documents WHERE namespace = ?", (namespace,))
                        else:
                            self._upsert(connection, namespace, delta[None])
                        continue
                    if None in delta:
                        raise ValueError("record delta cannot use a document selector")
                    changed, deleted_revision = False, 0.0
                    for key, payload in delta.items():
                        storage_key = state_record_storage_key(namespace, str(key))
                        if payload is None:
                            deleted = connection.execute("DELETE FROM state_documents WHERE namespace = ? RETURNING updated_at",
                                                         (storage_key,)).fetchone()
                            if deleted is not None:
                                changed = True
                                deleted_revision = max(deleted_revision, deleted[0])
                        else:
                            self._upsert(connection, storage_key, payload)
                            changed = True
                    if changed:
                        self._touch_record_revision(connection, namespace, minimum=deleted_revision)
            success = True
            return changes
        finally:
            self._keyed_mutation_metrics.observe(time.perf_counter() - started, success=success)

    def record_mutate(
        self,
        namespace: str,
        keys: tuple[str, ...],
        updater: Callable[[dict[str, Any | None]], dict[str, Any | None]],
    ) -> dict[str, Any | None]:
        """Atomically inspect selected records and apply a bounded record delta."""
        selected = tuple(dict.fromkeys(str(key) for key in keys))
        started = time.perf_counter()
        success = False
        try:
            with self._write_connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._ensure_record_collection_in_connection(connection, namespace)
                current: dict[str, Any | None] = {}
                for key in selected:
                    row = connection.execute(
                        "SELECT payload FROM state_documents WHERE namespace = ?",
                        (state_record_storage_key(namespace, key),),
                    ).fetchone()
                    current[key] = self._decode(row)
                changes = updater(current)
                if not isinstance(changes, dict):
                    raise TypeError("record_mutate updater must return a mapping")
                changed = False
                deleted_revision = 0.0
                for key, payload in changes.items():
                    storage_key = state_record_storage_key(namespace, str(key))
                    if payload is None:
                        deleted = connection.execute(
                            "DELETE FROM state_documents WHERE namespace = ? RETURNING updated_at",
                            (storage_key,),
                        ).fetchone()
                        if deleted is not None:
                            changed = True
                            deleted_revision = max(deleted_revision, deleted[0])
                    else:
                        self._upsert(connection, storage_key, payload)
                        changed = True
                if changed:
                    self._touch_record_revision(connection, namespace, minimum=deleted_revision)
            success = True
            return changes
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
            with self._write_connection() as connection:
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
        with self._write_connection() as connection:
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

    def document_get(self, namespace: str) -> Any | None:
        """Read only the legacy document, without reconstructing keyed records."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM state_documents WHERE namespace = ?",
                (namespace,),
            ).fetchone()
            return self._decode(row) if row is not None else None

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

    def document_array_item(
        self,
        namespace: str,
        array_name: str,
        item_id: str,
    ) -> Any | None:
        """Read one identified object from a JSON document array.

        SQLite performs the document scan in native code and Python decodes
        only the matching item. This avoids holding the interpreter lock while
        decoding a large catalog for point lookups.
        """

        if not array_name or not array_name.replace("_", "").isalnum():
            raise ValueError("array_name must be an identifier")
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT item.value
                FROM state_documents AS document,
                     json_each(document.payload, ?) AS item
                WHERE document.namespace = ?
                  AND json_extract(item.value, '$.id') = ?
                LIMIT 1
                """,
                (f"$.{array_name}", namespace, str(item_id)),
            ).fetchone()
        return self._decode(row)

    def put(self, namespace: str, payload: Any) -> None:
        if (
            isinstance(payload, dict)
            and self.record_collection_exists(namespace)
        ):
            self.record_replace(namespace, payload)
            return
        with self._write_connection() as connection:
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
        with self._write_connection() as connection:
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

        with self._write_connection() as connection:
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

    def _touch_record_revision(self, connection, namespace: str, *, minimum: float = 0.0) -> None:
        marker = state_record_marker(namespace)
        payload, timestamp = connection.execute(
            "SELECT payload, updated_at FROM state_documents WHERE namespace = ?",
            (marker,),
        ).fetchone()
        metadata = json.loads(payload)
        if not metadata.get("revisionTracked"):
            # One-time initialization for collections written by older releases.
            # Subsequent mutations read only their collection marker.
            prefix = state_record_prefix(namespace)
            latest = connection.execute(
                "SELECT MAX(updated_at) FROM state_documents WHERE namespace >= ? AND namespace < ?",
                (prefix, prefix + "\uffff"),
            ).fetchone()[0]
            timestamp = max(timestamp, latest or timestamp)
            metadata["revisionTracked"] = True
        revision = max(time.time(), math.nextafter(max(float(timestamp), minimum), math.inf))
        connection.execute(
            "UPDATE state_documents SET payload = ?, updated_at = ? WHERE namespace = ?",
            (self._serialized(metadata), revision, marker),
        )

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
                "SELECT payload, updated_at FROM state_documents WHERE namespace = ?",
                (marker,),
            ).fetchone()
            if row is None:
                return None
            metadata = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            if metadata.get("revisionTracked"):
                return float(row[1])
            prefix = state_record_prefix(namespace)
            latest = connection.execute(
                """
                SELECT MAX(updated_at)
                FROM state_documents
                WHERE namespace >= ? AND namespace < ?
                """,
                (prefix, prefix + "\uffff"),
            ).fetchone()
            return (
                float(latest[0])
                if latest and latest[0] is not None
                else float(row[1])
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
        with self._write_connection() as connection:
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

    def inspect_storage_pages(self, namespace, *, max_pages=10):
        from codex_web.storage.storage_preservation import inspect_database_page, StoragePreservationRefused
        if not 1 <= max_pages <= 10:
            raise StoragePreservationRefused("invalid_inspection_page_bound")
        with self._connection() as connection:
            connection.execute("BEGIN")
            after = None
            for _ in range(max_pages):
                rows, after = inspect_database_page(connection, namespace=namespace,
                    after=after, limit=100, backend="sqlite")
                yield rows, after
                if after is None:
                    return

    def inspect_storage_page(self, namespace, *, after=None, limit=100):
        from codex_web.storage.storage_preservation import inspect_database_page
        with self._connection() as connection:
            return inspect_database_page(connection, namespace=namespace, after=after,
                                         limit=limit, backend="sqlite")

    def iter_storage_snapshot(self, *, limits=None):
        from codex_web.storage.storage_preservation import (
            StateStorageSnapshotV1, StorageSnapshotLimits, database_rows, export_rows,
        )
        limits = limits or StorageSnapshotLimits()
        with self._connection() as connection:
            connection.execute("BEGIN")
            from codex_web.storage.storage_preservation import StoragePreservationRefused
            if connection.execute("SELECT value FROM state_metadata WHERE key='schema_version'").fetchone() != (str(self.SCHEMA_VERSION),):
                raise StoragePreservationRefused("unsupported_storage_schema")
            rows = database_rows(connection, backend="sqlite", limits=limits)
            try:
                yield from export_rows(StateStorageSnapshotV1(backend="sqlite", storage_schema_version=self.SCHEMA_VERSION), rows, limits)
            finally:
                rows.close()

    def restore_storage_snapshot(self, chunks, *, limits=None, max_target_bytes=None):
        from codex_web.storage.storage_preservation import StorageSnapshotLimits, restore_database_rows
        try:
            with self._write_connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                restore_database_rows(connection, chunks, backend="sqlite",
                                      schema_version=self.SCHEMA_VERSION,
                                      limits=limits or StorageSnapshotLimits(),max_target_bytes=max_target_bytes)
        except sqlite3.OperationalError as error:
            # The connection context has already rolled back every batch.
            # Only our real deadline/cancellation interrupt becomes the public
            # budget exception; unrelated SQLite failures keep their contract.
            if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
                check_operation_budget()
            raise

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
