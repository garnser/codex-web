from __future__ import annotations

import json
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from codex_web.storage.state_store import (
    parse_state_record_storage_key,
    state_record_marker,
    state_record_prefix,
    state_record_storage_key,
)


class PostgresStateStore:
    """Shared transactional document store backed by PostgreSQL.

    psycopg is an optional deployment dependency and is imported only when this
    backend is selected. Repository/domain code continues to consume the common
    StateStore contract.
    """

    SCHEMA_VERSION = 1

    def __init__(
        self,
        dsn: str,
        *,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self.dsn = str(dsn or "").strip()
        if not self.dsn:
            raise ValueError("PostgreSQL DSN must not be empty")
        if connect is None:
            try:
                import psycopg  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "PostgreSQL state storage requires the optional 'psycopg' package"
                ) from exc
            connect = psycopg.connect
        self._connect_factory = connect
        self._initialize()

    def _connect(self):
        return self._connect_factory(self.dsn)

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _serialized(payload: Any) -> str:
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _decode(row: Any, default: Any = None) -> Any:
        if row is None:
            return default
        value = row[0]
        if isinstance(value, str):
            return json.loads(value)
        return value

    @staticmethod
    def _lock(cursor: Any, namespace: str) -> None:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (namespace,),
        )

    @classmethod
    def _lock_many(cls, cursor: Any, namespaces: tuple[str, ...]) -> None:
        for namespace in sorted(namespaces):
            cls._lock(cursor, namespace)

    @classmethod
    def _upsert(cls, cursor: Any, namespace: str, payload: Any) -> None:
        cursor.execute(
            """
            INSERT INTO codex_state_documents(namespace, payload, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT(namespace) DO UPDATE SET
                payload = EXCLUDED.payload,
                updated_at = EXCLUDED.updated_at
            """,
            (namespace, cls._serialized(payload), time.time()),
        )

    def _initialize(self) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS codex_state_documents (
                        namespace TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        updated_at DOUBLE PRECISION NOT NULL
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS codex_state_metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at DOUBLE PRECISION NOT NULL
                    )
                    """
                )
                cursor.execute(
                    "SELECT value FROM codex_state_metadata WHERE key = %s",
                    ("schema_version",),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        """
                        INSERT INTO codex_state_metadata(key, value, updated_at)
                        VALUES (%s, %s, %s)
                        """,
                        ("schema_version", str(self.SCHEMA_VERSION), time.time()),
                    )
                elif int(row[0]) > self.SCHEMA_VERSION:
                    raise RuntimeError(
                        "PostgreSQL state schema is newer than this codex-web binary"
                    )

    @staticmethod
    def _record_collection_exists_in_cursor(
        cursor: Any,
        namespace: str,
    ) -> bool:
        cursor.execute(
            "SELECT 1 FROM codex_state_documents WHERE namespace = %s",
            (state_record_marker(namespace),),
        )
        return cursor.fetchone() is not None

    @staticmethod
    def _record_items_in_cursor(
        cursor: Any,
        namespace: str,
    ) -> dict[str, Any]:
        prefix = f"{state_record_prefix(namespace)}k/"
        cursor.execute(
            """
            SELECT namespace, payload
            FROM codex_state_documents
            WHERE LEFT(namespace, LENGTH(%s)) = %s
            ORDER BY namespace
            """,
            (prefix, prefix),
        )
        rows = cursor.fetchall()
        result: dict[str, Any] = {}
        for storage_namespace, payload in rows:
            parsed = parse_state_record_storage_key(str(storage_namespace))
            if parsed is None or parsed[1] is None:
                continue
            result[parsed[1]] = (
                json.loads(payload) if isinstance(payload, str) else payload
            )
        return result

    @classmethod
    def _replace_records_in_cursor(
        cls,
        cursor: Any,
        namespace: str,
        records: dict[str, Any],
    ) -> None:
        prefix = state_record_prefix(namespace)
        cursor.execute(
            "DELETE FROM codex_state_documents WHERE LEFT(namespace, LENGTH(%s)) = %s",
            (prefix, prefix),
        )
        cursor.execute(
            "DELETE FROM codex_state_documents WHERE namespace = %s",
            (namespace,),
        )
        cls._upsert(
            cursor,
            state_record_marker(namespace),
            {"schemaVersion": 1},
        )
        for key, payload in records.items():
            cls._upsert(
                cursor,
                state_record_storage_key(namespace, str(key)),
                payload,
            )

    @classmethod
    def _ensure_record_collection_in_cursor(
        cls,
        cursor: Any,
        namespace: str,
    ) -> None:
        if cls._record_collection_exists_in_cursor(cursor, namespace):
            return
        cursor.execute(
            "SELECT payload FROM codex_state_documents WHERE namespace = %s FOR UPDATE",
            (namespace,),
        )
        row = cursor.fetchone()
        if row is None:
            cls._upsert(
                cursor,
                state_record_marker(namespace),
                {"schemaVersion": 1},
            )
            return
        payload = cls._decode(row)
        if not isinstance(payload, dict):
            raise TypeError(
                f"state namespace {namespace!r} is not a keyed mapping"
            )
        cls._replace_records_in_cursor(cursor, namespace, payload)

    def record_collection_exists(self, namespace: str) -> bool:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                return self._record_collection_exists_in_cursor(
                    cursor,
                    namespace,
                )

    def record_get(self, namespace: str, key: str) -> Any | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                if self._record_collection_exists_in_cursor(
                    cursor,
                    namespace,
                ):
                    cursor.execute(
                        "SELECT payload FROM codex_state_documents WHERE namespace = %s",
                        (state_record_storage_key(namespace, key),),
                    )
                    return self._decode(cursor.fetchone())
                cursor.execute(
                    "SELECT payload FROM codex_state_documents WHERE namespace = %s",
                    (namespace,),
                )
                payload = self._decode(cursor.fetchone())
                if isinstance(payload, dict):
                    return payload.get(key)
                return None

    def record_items(self, namespace: str) -> dict[str, Any]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                if self._record_collection_exists_in_cursor(
                    cursor,
                    namespace,
                ):
                    return self._record_items_in_cursor(
                        cursor,
                        namespace,
                    )
                cursor.execute(
                    "SELECT payload FROM codex_state_documents WHERE namespace = %s",
                    (namespace,),
                )
                payload = self._decode(cursor.fetchone())
                return dict(payload) if isinstance(payload, dict) else {}

    def record_apply(
        self,
        namespace: str,
        *,
        upserts: dict[str, Any],
        deletes: tuple[str, ...] = (),
    ) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                self._lock(cursor, namespace)
                self._ensure_record_collection_in_cursor(
                    cursor,
                    namespace,
                )
                for key in dict.fromkeys(str(item) for item in deletes):
                    cursor.execute(
                        "DELETE FROM codex_state_documents WHERE namespace = %s",
                        (state_record_storage_key(namespace, key),),
                    )
                for key, payload in upserts.items():
                    self._upsert(
                        cursor,
                        state_record_storage_key(namespace, str(key)),
                        payload,
                    )

    def record_replace(
        self,
        namespace: str,
        records: dict[str, Any],
    ) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                self._lock(cursor, namespace)
                self._replace_records_in_cursor(
                    cursor,
                    namespace,
                    records,
                )

    def schema_version(self) -> int:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT value FROM codex_state_metadata WHERE key = %s",
                    ("schema_version",),
                )
                row = cursor.fetchone()
        return int(row[0]) if row else 0

    def status(self) -> dict[str, Any]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*), MAX(updated_at) FROM codex_state_documents"
                )
                row = cursor.fetchone() or (0, None)
                cursor.execute("SELECT 1")
                healthy = cursor.fetchone() == (1,)
        return {
            "backend": "postgresql",
            "schemaVersion": self.schema_version(),
            "supportedSchemaVersion": self.SCHEMA_VERSION,
            "ok": bool(healthy),
            "documents": int(row[0] or 0),
            "lastDocumentUpdateAt": (
                float(row[1]) if row[1] is not None else None
            ),
            "shared": True,
        }

    def get(self, namespace: str) -> Any | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM codex_state_documents WHERE namespace = %s",
                    (namespace,),
                )
                row = cursor.fetchone()
                if row is not None:
                    return self._decode(row)
                if self._record_collection_exists_in_cursor(
                    cursor,
                    namespace,
                ):
                    return self._record_items_in_cursor(
                        cursor,
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
            with connection.cursor() as cursor:
                self._lock(cursor, namespace)
                if self._record_collection_exists_in_cursor(
                    cursor,
                    namespace,
                ):
                    prefix = state_record_prefix(namespace)
                    cursor.execute(
                        "DELETE FROM codex_state_documents WHERE LEFT(namespace, LENGTH(%s)) = %s",
                        (prefix, prefix),
                    )
                self._upsert(cursor, namespace, payload)

    def update(
        self,
        namespace: str,
        updater: Callable[[Any], Any],
        *,
        default: Any,
    ) -> Any:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                self._lock(cursor, namespace)
                if self._record_collection_exists_in_cursor(
                    cursor,
                    namespace,
                ):
                    current = self._record_items_in_cursor(
                        cursor,
                        namespace,
                    )
                    updated = updater(current)
                    if not isinstance(updated, dict):
                        raise TypeError(
                            "record-backed namespace updates must return a mapping"
                        )
                    self._replace_records_in_cursor(
                        cursor,
                        namespace,
                        updated,
                    )
                    return updated
                cursor.execute(
                    "SELECT payload FROM codex_state_documents WHERE namespace = %s FOR UPDATE",
                    (namespace,),
                )
                current = self._decode(cursor.fetchone(), default)
                updated = updater(current)
                self._upsert(cursor, namespace, updated)
                return updated

    def update_many(
        self,
        defaults: dict[str, Any],
        updater: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        namespaces = tuple(dict.fromkeys(defaults))
        if not namespaces:
            raise ValueError("update_many requires at least one namespace")
        with self._connection() as connection:
            with connection.cursor() as cursor:
                self._lock_many(cursor, namespaces)
                current: dict[str, Any] = {}
                record_backed: set[str] = set()
                for namespace in namespaces:
                    if self._record_collection_exists_in_cursor(
                        cursor,
                        namespace,
                    ):
                        record_backed.add(namespace)
                        current[namespace] = self._record_items_in_cursor(
                            cursor,
                            namespace,
                        )
                        continue
                    cursor.execute(
                        "SELECT payload FROM codex_state_documents WHERE namespace = %s FOR UPDATE",
                        (namespace,),
                    )
                    current[namespace] = self._decode(
                        cursor.fetchone(),
                        defaults[namespace],
                    )
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
                        self._replace_records_in_cursor(
                            cursor,
                            namespace,
                            updated[namespace],
                        )
                    else:
                        self._upsert(cursor, namespace, updated[namespace])
                return updated

    def contains(self, namespace: str) -> bool:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM codex_state_documents WHERE namespace = %s",
                    (namespace,),
                )
                return (
                    cursor.fetchone() is not None
                    or self._record_collection_exists_in_cursor(
                        cursor,
                        namespace,
                    )
                )

    def delete(self, namespace: str) -> bool:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                self._lock(cursor, namespace)
                cursor.execute(
                    "DELETE FROM codex_state_documents WHERE namespace = %s",
                    (namespace,),
                )
                direct_count = int(cursor.rowcount or 0)
                prefix = state_record_prefix(namespace)
                cursor.execute(
                    "DELETE FROM codex_state_documents WHERE LEFT(namespace, LENGTH(%s)) = %s",
                    (prefix, prefix),
                )
                return bool(direct_count or cursor.rowcount)

    def documents(self) -> dict[str, Any]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT namespace, payload FROM codex_state_documents ORDER BY namespace"
                )
                rows = cursor.fetchall()
        documents: dict[str, Any] = {}
        records: dict[str, dict[str, Any]] = {}
        record_collections: set[str] = set()
        for storage_namespace, payload in rows:
            name = str(storage_namespace)
            parsed = parse_state_record_storage_key(name)
            decoded = json.loads(payload) if isinstance(payload, str) else payload
            if parsed is None:
                documents[name] = decoded
                continue
            logical_namespace, key = parsed
            record_collections.add(logical_namespace)
            if key is not None:
                records.setdefault(logical_namespace, {})[key] = decoded
        for namespace in record_collections:
            if namespace not in documents:
                documents[namespace] = records.get(namespace, {})
        return documents
