from __future__ import annotations

import atexit
import json
import math
import time
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from codex_web.storage.state_store import (
    check_operation_budget, storage_operation_budget,
    OperationTimingMetrics,
    parse_state_record_storage_key,
    state_record_marker,
    state_record_prefix,
    state_record_storage_key,
)


_SHARED_POOLS: dict[tuple[str, int, float], Any] = {}
_SHARED_POOLS_LOCK = threading.Lock()


def shared_postgres_connection_pool(dsn: str, *, max_size: int = 16, timeout: float = 5.0):
    """Reuse transport resources only for identical DSN/TLS and pool context."""
    key = (dsn, int(max_size), float(timeout))
    with _SHARED_POOLS_LOCK:
        pool = _SHARED_POOLS.get(key)
        if pool is None or pool.closed:
            try:
                from psycopg_pool import ConnectionPool  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "PostgreSQL state storage requires the optional 'psycopg_pool' package"
                ) from exc
            pool = ConnectionPool(
                conninfo=dsn, min_size=1, max_size=int(max_size),
                timeout=float(timeout), name="canonical-state", open=True,
            )
            _SHARED_POOLS[key] = pool
        return pool


def close_postgres_connection_pools() -> None:
    with _SHARED_POOLS_LOCK:
        pools = tuple(_SHARED_POOLS.values())
        _SHARED_POOLS.clear()
    for pool in pools:
        pool.close()


atexit.register(close_postgres_connection_pools)


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
        connection_pool: Any | None = None,
        pool_max_size: int = 16,
        pool_timeout_seconds: float = 5.0,
        initialize: bool = True,
    ) -> None:
        self.dsn = str(dsn or "").strip()
        if not self.dsn:
            raise ValueError("PostgreSQL DSN must not be empty")
        if int(pool_max_size) < 1 or float(pool_timeout_seconds) <= 0:
            raise ValueError("PostgreSQL pool limits must be positive")
        self._pool = connection_pool
        self._shared_pool = False
        self._closed = False
        self._callback_reads = threading.local()
        if connect is None:
            try:
                import psycopg  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "PostgreSQL state storage requires the optional 'psycopg' package"
                ) from exc
            connect = psycopg.connect
            if self._pool is None:
                self._pool = shared_postgres_connection_pool(
                    self.dsn, max_size=int(pool_max_size),
                    timeout=float(pool_timeout_seconds),
                )
                self._shared_pool = True
        self._connect_factory = connect
        self._keyed_mutation_metrics = OperationTimingMetrics()
        try:
            if initialize:
                self._initialize()
            else:
                with self._connection(read_only_snapshot=True) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT value FROM codex_state_metadata WHERE key=%s', ('schema_version',))
                        row = cursor.fetchone()
                        cursor.execute('SELECT namespace FROM codex_state_documents LIMIT 0')
                if row is None or int(row[0]) != self.SCHEMA_VERSION:
                    raise RuntimeError('Existing canonical state schema is unsupported')
        except BaseException:
            self.close()
            raise
        if self._pool is not None and not self._shared_pool:
            atexit.register(self._pool.close)

    def _connect(self):
        return self._connect_factory(self.dsn)

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        # Short-lived stores release their lease, not the process-owned pool.
        # Pool shutdown is coordinated once after all runtime owners stop.
        if getattr(self, "_pool", None) is not None and not self._shared_pool:
            atexit.unregister(self._pool.close)
            self._pool.close()

    @contextmanager
    def _connection(self, *, read_only_snapshot=False) -> Iterator[Any]:
        check_operation_budget()
        if getattr(self._callback_reads, "connection", None) is not None:
            raise RuntimeError("nested mutation inside a canonical state callback is unsupported")
        if getattr(self, "_closed", False):
            raise RuntimeError("PostgreSQL state store is closed")
        if getattr(self, "_pool", None) is not None:
            # The official pool commits/rolls back before returning this
            # connection. Never share an in-flight transaction across callers.
            budget = storage_operation_budget.get()
            checkout = self._pool.connection(timeout=budget.remaining()) if budget else self._pool.connection()
            with checkout as connection:
                if read_only_snapshot:
                    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                self._operation_deadline(connection)
                yield connection
                check_operation_budget()
            return
        connection = self._connect()
        try:
            with connection:
                if read_only_snapshot:
                    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                self._operation_deadline(connection)
                yield connection
                check_operation_budget()
        finally:
            connection.close()

    @contextmanager
    def _read_connection(self) -> Iterator[Any]:
        """A synchronous callback's reads belong to its owning transaction.

        Thread-local custody prevents another thread from borrowing this live
        connection. Only the outer operation commits, rolls back and returns it.
        Ordinary reads still acquire and release their own bounded-pool lease.
        """
        check_operation_budget()
        if getattr(self, "_closed", False):
            raise RuntimeError("PostgreSQL state store is closed")
        connection = getattr(self._callback_reads, "connection", None)
        if connection is None:
            with self._connection() as connection:
                yield connection
        else:
            yield connection
            check_operation_budget()

    def _run_updater(self, connection, updater, current):
        previous = getattr(self._callback_reads, "connection", None)
        self._callback_reads.connection = connection
        try:
            return updater(current)
        finally:
            self._callback_reads.connection = previous

    @staticmethod
    def _operation_deadline(connection):
        budget = storage_operation_budget.get()
        if budget is not None:
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('statement_timeout', %s, true)", (str(max(1, int(budget.remaining() * 1000))),))

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
                # Record keys are encoded byte strings. Default locale ordering
                # can exclude real keys from prefix ranges; retain a matching
                # expression index without rewriting existing rows or the PK.
                cursor.execute('CREATE INDEX IF NOT EXISTS codex_state_documents_namespace_c_idx '
                               'ON codex_state_documents (namespace COLLATE "C")')
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
            WHERE namespace COLLATE "C" >= %s AND namespace COLLATE "C" < %s
            ORDER BY namespace COLLATE "C"
            """,
            (prefix, prefix + "\uffff"),
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
            'SELECT MAX(updated_at) FROM codex_state_documents WHERE namespace = %s OR (namespace COLLATE "C" >= %s AND namespace COLLATE "C" < %s)',
            (namespace, prefix, prefix + "\uffff"),
        )
        previous = cursor.fetchone()[0]
        cursor.execute(
            'DELETE FROM codex_state_documents WHERE namespace COLLATE "C" >= %s AND namespace COLLATE "C" < %s',
            (prefix, prefix + "\uffff"),
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
        cls._touch_record_revision(cursor, namespace, minimum=previous or 0.0)

    @classmethod
    def _update_records_in_cursor(cls, cursor, namespace, before, records):
        """Keep retained rows intact while applying an already locked update.

        ``before`` contains serialized snapshots taken before the callback: an
        updater may mutate nested values in place, and JSON booleans/numbers
        must remain distinguishable even though Python compares True == 1.
        Explicit record replacement and migration retain their separate path.
        """
        prefix = state_record_prefix(namespace)
        cursor.execute(
            'SELECT MAX(updated_at) FROM codex_state_documents WHERE namespace = %s OR '
            '(namespace COLLATE "C" >= %s AND namespace COLLATE "C" < %s)',
            (namespace, prefix, prefix + "\uffff"),
        )
        previous = cursor.fetchone()[0]
        normalized = {str(key): payload for key, payload in records.items()}
        for key in before.keys() - normalized.keys():
            cursor.execute("DELETE FROM codex_state_documents WHERE namespace = %s",
                           (state_record_storage_key(namespace, key),))
        for key, payload in normalized.items():
            if key not in before or before[key] != cls._serialized(payload):
                cls._upsert(cursor, state_record_storage_key(namespace, key), payload)
        # Preserve the old mapping-update revision and legacy-shadow cleanup
        # contracts without deleting/reinserting the retained collection.
        cursor.execute("DELETE FROM codex_state_documents WHERE namespace = %s", (namespace,))
        cls._touch_record_revision(cursor, namespace, minimum=previous or 0.0)

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
        with self._read_connection() as connection:
            with connection.cursor() as cursor:
                return self._record_collection_exists_in_cursor(
                    cursor,
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
        with self._read_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT namespace FROM codex_state_documents WHERE namespace = ANY(%s)",
                    (list(markers),),
                )
                rows = cursor.fetchall()
        present = {row[0] for row in rows}
        return {name: marker in present for name, marker in zip(namespaces, markers)}

    def record_get(self, namespace: str, key: str) -> Any | None:
        with self._read_connection() as connection:
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

    def record_get_many(self, namespace: str, keys: tuple[str, ...]) -> dict[str, Any]:
        if len(keys) > 1000:
            raise ValueError("record batch exceeds 1000 keys")
        keys = tuple(dict.fromkeys(str(key) for key in keys))
        if not keys:
            return {}
        with self._read_connection() as connection:
            with connection.cursor() as cursor:
                if not self._record_collection_exists_in_cursor(cursor, namespace):
                    cursor.execute(
                        "SELECT payload FROM codex_state_documents WHERE namespace = %s", (namespace,),
                    )
                    payload = self._decode(cursor.fetchone())
                    return {key: payload[key] for key in keys if key in payload} if isinstance(payload, dict) else {}
                storage_keys = {state_record_storage_key(namespace, key): key for key in keys}
                cursor.execute(
                    "SELECT namespace, payload FROM codex_state_documents WHERE namespace = ANY(%s)",
                    (list(storage_keys),),
                )
                return {storage_keys[row[0]]: self._decode((row[1],)) for row in cursor.fetchall()}

    def record_items(self, namespace: str) -> dict[str, Any]:
        with self._read_connection() as connection:
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

    def record_count(self, namespace: str) -> int:
        with self._read_connection() as connection:
            with connection.cursor() as cursor:
                if not self._record_collection_exists_in_cursor(
                    cursor,
                    namespace,
                ):
                    if getattr(self._callback_reads, "connection", None) is not None:
                        raise RuntimeError("callback record count requires an initialized collection")
                    self._lock(cursor, namespace)
                    self._ensure_record_collection_in_cursor(
                        cursor,
                        namespace,
                    )
                prefix = f"{state_record_prefix(namespace)}k/"
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM codex_state_documents
                    WHERE namespace COLLATE "C" >= %s AND namespace COLLATE "C" < %s
                    """,
                    (prefix, prefix + "\uffff"),
                )
                row = cursor.fetchone()
        return int(row[0] or 0) if row else 0

    def record_page(
        self,
        namespace: str,
        *,
        key_prefix: str | None = None,
        after: str | None = None,
        limit: int = 100,
    ) -> tuple[dict[str, Any], str | None]:
        page_size = max(1, min(int(limit), 1000))
        with self._read_connection() as connection:
            with connection.cursor() as cursor:
                if not self._record_collection_exists_in_cursor(
                    cursor,
                    namespace,
                ):
                    if getattr(self._callback_reads, "connection", None) is not None:
                        raise RuntimeError("callback record page requires an initialized collection")
                    self._lock(cursor, namespace)
                    self._ensure_record_collection_in_cursor(
                        cursor,
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
                cursor.execute(
                    """
                    SELECT namespace, payload
                    FROM codex_state_documents
                    WHERE namespace COLLATE "C" >= %s
                      AND namespace COLLATE "C" < %s
                      AND namespace COLLATE "C" > %s
                    ORDER BY namespace COLLATE "C"
                    LIMIT %s
                    """,
                    (lower, upper, after_storage, page_size + 1),
                )
                rows = cursor.fetchall()

        page: dict[str, Any] = {}
        for storage_namespace, payload in rows[:page_size]:
            parsed = parse_state_record_storage_key(
                str(storage_namespace)
            )
            if parsed is None or parsed[1] is None:
                continue
            page[parsed[1]] = (
                json.loads(payload)
                if isinstance(payload, str)
                else payload
            )
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
                with connection.cursor() as cursor:
                    self._lock(cursor, namespace)
                    self._ensure_record_collection_in_cursor(
                        cursor,
                        namespace,
                    )
                    changed = bool(upserts)
                    deleted_revision = 0.0
                    for key in dict.fromkeys(str(item) for item in deletes):
                        cursor.execute(
                            "DELETE FROM codex_state_documents WHERE namespace = %s RETURNING updated_at",
                            (state_record_storage_key(namespace, key),),
                        )
                        deleted = cursor.fetchone()
                        if deleted is not None:
                            changed = True
                            deleted_revision = max(deleted_revision, deleted[0])
                    for key, payload in upserts.items():
                        self._upsert(
                            cursor,
                            state_record_storage_key(namespace, str(key)),
                            payload,
                        )
                    if changed:
                        self._touch_record_revision(cursor, namespace, minimum=deleted_revision)
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
                with connection.cursor() as cursor:
                    # All collection writers acquire the namespace lock before
                    # record/marker locks, including replacement and deletion.
                    self._lock(cursor, namespace)
                    if not self._record_collection_exists_in_cursor(
                        cursor,
                        namespace,
                    ):
                        self._ensure_record_collection_in_cursor(
                            cursor,
                            namespace,
                        )
                    storage_key = state_record_storage_key(
                        namespace,
                        str(key),
                    )
                    cursor.execute(
                        "SELECT payload FROM codex_state_documents WHERE namespace = %s FOR UPDATE",
                        (storage_key,),
                    )
                    current = self._decode(cursor.fetchone(), default)
                    updated = self._run_updater(connection, updater, current)
                    deleted_revision = 0.0
                    if updated is None:
                        cursor.execute(
                            "DELETE FROM codex_state_documents WHERE namespace = %s RETURNING updated_at",
                            (storage_key,),
                        )
                        deleted = cursor.fetchone()
                        changed = deleted is not None
                        deleted_revision = deleted[0] if deleted is not None else 0.0
                    else:
                        self._upsert(cursor, storage_key, updated)
                        changed = True
                    if changed:
                        self._touch_record_revision(cursor, namespace, minimum=deleted_revision)
            success = True
            return updated
        finally:
            self._keyed_mutation_metrics.observe(
                time.perf_counter() - started,
                success=success,
            )

    def record_mutate_many(self, selected, updater):
        """Bounded cross-collection rows; same advisory locks as update_many.

        Sorted namespace locks protect dynamic outbox index/receipt keys and
        serialize with existing per-record and bulk canonical transitions.
        """
        if not selected:
            raise ValueError("record_mutate_many requires selected namespaces")
        started = time.perf_counter()
        success = False
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    self._lock_many(cursor, tuple(selected))
                    current = {}
                    for namespace, keys in selected.items():
                        if None in keys:
                            if keys != (None,) or self._record_collection_exists_in_cursor(cursor, namespace):
                                raise ValueError("document selector must name a single non-record namespace")
                            cursor.execute("SELECT payload FROM codex_state_documents WHERE namespace = %s FOR UPDATE", (namespace,))
                            current[namespace] = {None: self._decode(cursor.fetchone())}
                            continue
                        self._ensure_record_collection_in_cursor(cursor, namespace)
                        current[namespace] = {}
                        for key in dict.fromkeys(keys):
                            cursor.execute("SELECT payload FROM codex_state_documents WHERE namespace = %s FOR UPDATE",
                                (state_record_storage_key(namespace, str(key)),))
                            current[namespace][key] = self._decode(cursor.fetchone())
                    changes = self._run_updater(connection, updater, current)
                    if not isinstance(changes, dict) or set(changes) - set(selected):
                        raise TypeError("record_mutate_many must return deltas for selected collections")
                    for namespace, delta in changes.items():
                        if not isinstance(delta, dict):
                            raise TypeError("collection delta must be a mapping")
                        if selected[namespace] == (None,):
                            if set(delta) != {None}:
                                raise ValueError("document delta must contain exactly its None row")
                            if delta[None] is None:
                                cursor.execute("DELETE FROM codex_state_documents WHERE namespace = %s", (namespace,))
                            else:
                                self._upsert(cursor, namespace, delta[None])
                            continue
                        if None in delta:
                            raise ValueError("record delta cannot use a document selector")
                        changed, deleted_revision = False, 0.0
                        for key, payload in delta.items():
                            storage_key = state_record_storage_key(namespace, str(key))
                            if payload is None:
                                cursor.execute("DELETE FROM codex_state_documents WHERE namespace = %s RETURNING updated_at",
                                               (storage_key,))
                                deleted = cursor.fetchone()
                                if deleted is not None:
                                    changed = True
                                    deleted_revision = max(deleted_revision, deleted[0])
                            else:
                                self._upsert(cursor, storage_key, payload)
                                changed = True
                        if changed:
                            self._touch_record_revision(cursor, namespace, minimum=deleted_revision)
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
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    # The namespace lock protects dynamic keys returned by the
                    # updater as well as the selected records.
                    self._lock(cursor, namespace)
                    self._ensure_record_collection_in_cursor(cursor, namespace)
                    current: dict[str, Any | None] = {}
                    for key in selected:
                        cursor.execute(
                            "SELECT payload FROM codex_state_documents WHERE namespace = %s FOR UPDATE",
                            (state_record_storage_key(namespace, key),),
                        )
                        current[key] = self._decode(cursor.fetchone())
                    changes = self._run_updater(connection, updater, current)
                    if not isinstance(changes, dict):
                        raise TypeError("record_mutate updater must return a mapping")
                    changed = False
                    deleted_revision = 0.0
                    for key, payload in changes.items():
                        storage_key = state_record_storage_key(namespace, str(key))
                        if payload is None:
                            cursor.execute(
                                "DELETE FROM codex_state_documents WHERE namespace = %s RETURNING updated_at",
                                (storage_key,),
                            )
                            deleted = cursor.fetchone()
                            if deleted is not None:
                                changed = True
                                deleted_revision = max(deleted_revision, deleted[0])
                        else:
                            self._upsert(cursor, storage_key, payload)
                            changed = True
                    if changed:
                        self._touch_record_revision(cursor, namespace, minimum=deleted_revision)
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
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    self._lock(cursor, namespace)
                    self._replace_records_in_cursor(
                        cursor,
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
        with self._read_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT value FROM codex_state_metadata WHERE key = %s",
                    ("schema_version",),
                )
                row = cursor.fetchone()
        return int(row[0]) if row else 0

    def status(self) -> dict[str, Any]:
        with self._read_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (
                            WHERE namespace NOT LIKE %s
                               OR namespace LIKE %s
                        ),
                        MAX(updated_at)
                    FROM codex_state_documents
                    """,
                    (
                        "__codex_records__/%",
                        "__codex_records__/%/__meta__",
                    ),
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
            "keyedMutationMetrics": self._keyed_mutation_metrics.snapshot(),
            "connectionPool": (
                {
                    "minSize": self._pool.min_size,
                    "maxSize": self._pool.max_size,
                    "stats": self._pool.get_stats(),
                }
                if getattr(self, "_pool", None) is not None else None
            ),
        }

    def document_get(self, namespace: str) -> Any | None:
        """Read only the legacy document, without reconstructing keyed records."""
        with self._read_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM codex_state_documents WHERE namespace = %s",
                    (namespace,),
                )
                row = cursor.fetchone()
                return self._decode(row) if row is not None else None

    def get(self, namespace: str) -> Any | None:
        with self._read_connection() as connection:
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
                        'DELETE FROM codex_state_documents WHERE namespace COLLATE "C" >= %s AND namespace COLLATE "C" < %s',
                        (prefix, prefix + "\uffff"),
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
                    before = {key: self._serialized(value) for key, value in current.items()}
                    updated = self._run_updater(connection, updater, current)
                    if not isinstance(updated, dict):
                        raise TypeError(
                            "record-backed namespace updates must return a mapping"
                        )
                    self._update_records_in_cursor(
                        cursor,
                        namespace,
                        before,
                        updated,
                    )
                    return updated
                cursor.execute(
                    "SELECT payload FROM codex_state_documents WHERE namespace = %s FOR UPDATE",
                    (namespace,),
                )
                current = self._decode(cursor.fetchone(), default)
                updated = self._run_updater(connection, updater, current)
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
                before = {namespace: {key: self._serialized(value)
                                     for key, value in current[namespace].items()}
                          for namespace in record_backed}
                updated = self._run_updater(connection, updater, current)
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
                        self._update_records_in_cursor(
                            cursor,
                            namespace,
                            before[namespace],
                            updated[namespace],
                        )
                    else:
                        self._upsert(cursor, namespace, updated[namespace])
                return updated

    @classmethod
    def _touch_record_revision(cls, cursor, namespace: str, *, minimum: float = 0.0) -> None:
        marker = state_record_marker(namespace)
        cursor.execute(
            "SELECT payload, updated_at FROM codex_state_documents WHERE namespace = %s FOR UPDATE",
            (marker,),
        )
        payload, timestamp = cursor.fetchone()
        metadata = json.loads(payload) if isinstance(payload, str) else dict(payload)
        if not metadata.get("revisionTracked"):
            prefix = state_record_prefix(namespace)
            cursor.execute(
                'SELECT MAX(updated_at) FROM codex_state_documents WHERE namespace COLLATE "C" >= %s AND namespace COLLATE "C" < %s',
                (prefix, prefix + "\uffff"),
            )
            latest = cursor.fetchone()[0]
            timestamp = max(timestamp, latest or timestamp)
            metadata["revisionTracked"] = True
        revision = max(time.time(), math.nextafter(max(float(timestamp), minimum), math.inf))
        cursor.execute(
            "UPDATE codex_state_documents SET payload = %s, updated_at = %s WHERE namespace = %s",
            (cls._serialized(metadata), revision, marker),
        )

    def namespace_revision(self, namespace: str) -> float | None:
        with self._read_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT updated_at FROM codex_state_documents WHERE namespace = %s",
                    (namespace,),
                )
                row = cursor.fetchone()
                if row is not None:
                    return float(row[0])
                marker = state_record_marker(namespace)
                cursor.execute(
                    "SELECT payload, updated_at FROM codex_state_documents WHERE namespace = %s",
                    (marker,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                metadata = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                if metadata.get("revisionTracked"):
                    return float(row[1])
                prefix = state_record_prefix(namespace)
                cursor.execute(
                    """
                    SELECT MAX(updated_at)
                    FROM codex_state_documents
                    WHERE namespace COLLATE "C" >= %s AND namespace COLLATE "C" < %s
                    """,
                    (prefix, prefix + "\uffff"),
                )
                latest = cursor.fetchone()
                return (
                    float(latest[0])
                    if latest and latest[0] is not None
                    else float(row[1])
                )

    def contains(self, namespace: str) -> bool:
        with self._read_connection() as connection:
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
                    'DELETE FROM codex_state_documents WHERE namespace COLLATE "C" >= %s AND namespace COLLATE "C" < %s',
                    (prefix, prefix + "\uffff"),
                )
                return bool(direct_count or cursor.rowcount)

    def inspect_storage_pages(self, namespace, *, max_pages=10):
        from codex_web.storage.storage_preservation import inspect_database_page, StoragePreservationRefused
        if not 1 <= max_pages <= 10:
            raise StoragePreservationRefused("invalid_inspection_page_bound")
        if getattr(self._callback_reads, "connection", None) is not None:
            raise StoragePreservationRefused("inspection_requires_own_transaction")
        with self._connection(read_only_snapshot=True) as connection:
            after = None
            for _ in range(max_pages):
                rows, after = inspect_database_page(connection, namespace=namespace,
                    after=after, limit=100, backend="postgres")
                yield rows, after
                if after is None:
                    return

    def inspect_storage_page(self, namespace, *, after=None, limit=100):
        from codex_web.storage.storage_preservation import inspect_database_page
        with self._read_connection() as connection:
            return inspect_database_page(connection, namespace=namespace, after=after,
                                         limit=limit, backend="postgres")

    def iter_storage_snapshot(self, *, limits=None):
        from codex_web.storage.storage_preservation import (
            StateStorageSnapshotV1, StorageSnapshotLimits, StoragePreservationRefused,
            database_rows, export_rows,
        )
        limits = limits or StorageSnapshotLimits()
        # A callback may already have issued READ COMMITTED queries. Promoting
        # its isolation here is unsafe; refuse before any nested pool checkout.
        if getattr(self._callback_reads, "connection", None) is not None:
            raise StoragePreservationRefused("raw_snapshot_requires_own_transaction")
        with self._connection(read_only_snapshot=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SHOW server_encoding")
                if cursor.fetchone()[0] != "UTF8":
                    raise StoragePreservationRefused("unsupported_database_encoding")
                cursor.execute("SELECT value FROM codex_state_metadata WHERE key='schema_version'")
                if cursor.fetchone() != (str(self.SCHEMA_VERSION),):
                    raise StoragePreservationRefused("unsupported_storage_schema")
            rows = database_rows(connection, backend="postgres", limits=limits,
                                 deadline=lambda: self._operation_deadline(connection))
            try:
                yield from export_rows(StateStorageSnapshotV1(backend="postgres", storage_schema_version=self.SCHEMA_VERSION), rows, limits)
            finally:
                rows.close()

    def restore_storage_snapshot(self, chunks, *, limits=None, max_target_bytes=None):
        from codex_web.storage.storage_preservation import StorageSnapshotLimits, restore_database_rows
        with self._connection() as connection:
            restore_database_rows(connection, chunks, backend="postgres",
                                  schema_version=self.SCHEMA_VERSION,
                                  limits=limits or StorageSnapshotLimits(),max_target_bytes=max_target_bytes)

    def documents(self) -> dict[str, Any]:
        documents: dict[str, Any] = {}
        records: dict[str, dict[str, Any]] = {}
        record_collections: set[str] = set()
        # A named server cursor keeps one SELECT snapshot without buffering the
        # entire encoded export in libpq and Python alongside the decoded map.
        # Reuse a callback's owning transaction; never commit/return its lease.
        with self._read_connection() as connection:
            with connection.cursor(name="codex_export_" + uuid.uuid4().hex) as cursor:
                cursor.execute(
                    'SELECT namespace, payload FROM codex_state_documents ORDER BY namespace COLLATE "C"'
                )
                while True:
                    # FETCH is a new statement: refresh its remaining timeout,
                    # and cooperate with cancellation during Python decoding.
                    self._operation_deadline(connection)
                    rows = cursor.fetchmany(64)
                    check_operation_budget()
                    if not rows:
                        break
                    for storage_namespace, payload in rows:
                        check_operation_budget()
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
                    del rows
        for namespace in record_collections:
            check_operation_budget()
            if namespace not in documents:
                documents[namespace] = records.get(namespace, {})
        check_operation_budget()
        return documents
