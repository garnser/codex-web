from __future__ import annotations

import json
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator


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
        return self._decode(row)

    def put(self, namespace: str, payload: Any) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                self._lock(cursor, namespace)
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
                for namespace in namespaces:
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
                    self._upsert(cursor, namespace, updated[namespace])
                return updated

    def contains(self, namespace: str) -> bool:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM codex_state_documents WHERE namespace = %s",
                    (namespace,),
                )
                return cursor.fetchone() is not None

    def delete(self, namespace: str) -> bool:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                self._lock(cursor, namespace)
                cursor.execute(
                    "DELETE FROM codex_state_documents WHERE namespace = %s",
                    (namespace,),
                )
                return bool(cursor.rowcount)

    def documents(self) -> dict[str, Any]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT namespace, payload FROM codex_state_documents ORDER BY namespace"
                )
                rows = cursor.fetchall()
        return {
            str(namespace): json.loads(payload) if isinstance(payload, str) else payload
            for namespace, payload in rows
        }
