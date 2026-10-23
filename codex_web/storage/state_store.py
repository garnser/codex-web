from __future__ import annotations

import hashlib
import threading
import time
import contextvars
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable
from urllib.parse import quote, unquote

from pydantic import BaseModel, ConfigDict, Field
from codex_web.canonical_json import canonical_json_bytes, canonical_json_chunks


_RECORD_STORAGE_ROOT = "__codex_records__/"


def record_projection_fields(fields: tuple[str, ...]) -> tuple[str, ...]:
    """Bound code-owned top-level JSON field selection; never accept SQL paths."""
    if (not isinstance(fields, tuple) or not 1 <= len(fields) <= 32
            or any(not isinstance(name, str) or not name.isascii()
                or not name.isidentifier() or len(name) > 64 for name in fields)
            or len(set(fields)) != len(fields)):
        raise ValueError("record projection requires 1–32 distinct field names")
    return fields


def project_record_fields(value: Any, fields: tuple[str, ...]) -> Any:
    # Preserve missing versus null and non-object refusal semantics. A partial
    # read is not validation or an authority/cache decision.
    return {name: value[name] for name in fields if name in value} if isinstance(value, dict) else value


class StorageOperationBudget:
    """Cooperative deadline for existing store/validation work, not a lease.

    Cancelling an asyncio waiter cannot stop a running Python thread. The same
    budget follows that thread and refuses later writes after retirement.
    """
    def __init__(self, seconds: float):
        self.deadline = time.monotonic() + seconds
        self.cancelled = threading.Event()

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if self.cancelled.is_set() or remaining <= 0:
            raise TimeoutError("storage operation deadline exceeded")
        return remaining


storage_operation_budget = contextvars.ContextVar("storage_operation_budget", default=None)


def check_operation_budget():
    budget = storage_operation_budget.get()
    if budget is not None:
        budget.remaining()


@contextmanager
def operation_lock(lock):
    budget = storage_operation_budget.get()
    if budget is None:
        with lock:
            yield
        return
    while not lock.acquire(timeout=min(.05, budget.remaining())):
        pass
    try:
        check_operation_budget()
        yield
        check_operation_budget()
    finally:
        lock.release()


class OperationTimingMetrics:
    """Small process-local timing accumulator for storage hot paths."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0
        self._failures = 0
        self._total_seconds = 0.0
        self._last_seconds = 0.0
        self._max_seconds = 0.0
        self._last_at: float | None = None

    def observe(self, seconds: float, *, success: bool = True) -> None:
        duration = max(0.0, float(seconds))
        with self._lock:
            self._count += 1
            if not success:
                self._failures += 1
            self._total_seconds += duration
            self._last_seconds = duration
            self._max_seconds = max(self._max_seconds, duration)
            self._last_at = time.time()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            average = (
                self._total_seconds / self._count
                if self._count
                else 0.0
            )
            return {
                "count": self._count,
                "failures": self._failures,
                "lastSeconds": self._last_seconds,
                "averageSeconds": average,
                "maxSeconds": self._max_seconds,
                "lastAt": self._last_at,
            }



def state_record_prefix(namespace: str) -> str:
    return f"{_RECORD_STORAGE_ROOT}{quote(str(namespace), safe='')}/"


def state_record_marker(namespace: str) -> str:
    return f"{state_record_prefix(namespace)}__meta__"


def state_record_storage_key(namespace: str, key: str) -> str:
    return f"{state_record_prefix(namespace)}k/{quote(str(key), safe='')}"


def parse_state_record_storage_key(
    storage_namespace: str,
) -> tuple[str, str | None] | None:
    if not storage_namespace.startswith(_RECORD_STORAGE_ROOT):
        return None
    encoded_namespace, separator, suffix = storage_namespace[
        len(_RECORD_STORAGE_ROOT):
    ].partition("/")
    if not separator:
        return None
    namespace = unquote(encoded_namespace)
    if suffix == "__meta__":
        return namespace, None
    if suffix.startswith("k/"):
        return namespace, unquote(suffix[2:])
    return None


@runtime_checkable
class StateStore(Protocol):
    """Transactional document-store contract used by canonical repositories."""

    def get(self, namespace: str) -> Any | None: ...
    def put(self, namespace: str, payload: Any) -> None: ...
    def update(
        self,
        namespace: str,
        updater: Callable[[Any], Any],
        *,
        default: Any,
    ) -> Any: ...
    def update_many(
        self,
        defaults: dict[str, Any],
        updater: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]: ...
    def record_collection_exists(self, namespace: str) -> bool: ...
    def record_collections_exist(self, namespaces: tuple[str, ...]) -> dict[str, bool]: ...
    def record_get(self, namespace: str, key: str) -> Any | None: ...
    def record_get_many(self, namespace: str, keys: tuple[str, ...]) -> dict[str, Any]: ...
    def record_items(self, namespace: str) -> dict[str, Any]: ...
    def record_count(self, namespace: str) -> int: ...
    def home_intent_recovery_rows(self, namespace: str, query) -> list[Any]: ...
    def record_page(
        self,
        namespace: str,
        *,
        key_prefix: str | None = None,
        after: str | None = None,
        limit: int = 100,
    ) -> tuple[dict[str, Any], str | None]: ...
    def record_apply(
        self,
        namespace: str,
        *,
        upserts: dict[str, Any],
        deletes: tuple[str, ...] = (),
    ) -> None: ...
    def record_update(
        self,
        namespace: str,
        key: str,
        updater: Callable[[Any], Any],
        *,
        default: Any,
    ) -> Any: ...
    def record_mutate_many(
        self,
        selected: dict[str, tuple[str | None, ...]],
        updater: Callable[[dict[str, dict[str | None, Any]]], dict[str, dict[str | None, Any]]],
    ) -> dict[str, dict[str | None, Any]]:
        """Mutate selected rows; sole selector None denotes an existing document.

        Document selection preserves its storage shape and never migrates it to
        keyed records. A document delta must contain exactly the None row.
        """
        ...

    def record_mutate(
        self,
        namespace: str,
        keys: tuple[str, ...],
        updater: Callable[[dict[str, Any | None]], dict[str, Any | None]],
    ) -> dict[str, Any | None]: ...
    def record_replace(
        self,
        namespace: str,
        records: dict[str, Any],
    ) -> None: ...
    def namespace_revision(self, namespace: str) -> float | None: ...
    def contains(self, namespace: str) -> bool: ...
    def delete(self, namespace: str) -> bool: ...
    def inspect_storage_pages(self, namespace, *, max_pages=10): ...
    def inspect_storage_page(self, namespace, *, after=None, limit=100): ...
    def iter_storage_snapshot(self, *, limits=None): ...
    def restore_storage_snapshot(self, chunks, *, limits=None) -> None: ...
    def documents(self) -> dict[str, Any]: ...
    def status(self) -> dict[str, Any]: ...


class StateStoreMigrationConflict(RuntimeError):
    pass


class StateStoreMigrationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_backend: str
    target_backend: str
    source_documents: int = Field(ge=0)
    target_documents_before: int = Field(ge=0)
    copied_documents: int = Field(ge=0)
    existing_equal_documents: int = Field(ge=0)
    source_checksum: str
    target_checksum: str
    inserted_namespaces: tuple[str, ...] = ()
    verified: bool = False


def _canonical_bytes(value: Any) -> bytes:
    return canonical_json_bytes(value, default=str)


def state_documents_checksum(documents: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for namespace in sorted(documents):
        digest.update(namespace.encode("utf-8"))
        digest.update(b"\x00")
        document_hash = hashlib.sha256()
        for chunk in canonical_json_chunks(documents[namespace], default=str):
            document_hash.update(chunk)
        digest.update(document_hash.digest())
        digest.update(b"\x00")
    return digest.hexdigest()


class StateStoreMigrator:
    """Idempotent verified document migration.

    The source is never mutated, so rollback is always possible by switching
    runtime configuration back to the source backend. Destination cleanup is
    optional and removes only namespaces inserted by the recorded migration
    whose values still match the source snapshot.
    """

    def migrate(
        self,
        source: StateStore,
        target: StateStore,
    ) -> StateStoreMigrationResult:
        source_docs = source.documents()
        before = target.documents()
        inserted: list[str] = []
        existing_equal = 0
        unexpected = sorted(set(before) - set(source_docs))
        if unexpected:
            raise StateStoreMigrationConflict(
                "destination contains namespaces absent from source: "
                + ", ".join(unexpected[:20])
            )

        for namespace in sorted(source_docs):
            incoming = source_docs[namespace]
            if namespace in before:
                if _canonical_bytes(before[namespace]) != _canonical_bytes(incoming):
                    raise StateStoreMigrationConflict(
                        f"destination namespace differs from source: {namespace}"
                    )
                existing_equal += 1
                continue
            target.put(namespace, incoming)
            inserted.append(namespace)

        after = target.documents()
        for namespace, incoming in source_docs.items():
            if namespace not in after:
                raise StateStoreMigrationConflict(
                    f"destination is missing migrated namespace: {namespace}"
                )
            if _canonical_bytes(after[namespace]) != _canonical_bytes(incoming):
                raise StateStoreMigrationConflict(
                    f"destination verification mismatch: {namespace}"
                )

        source_checksum = state_documents_checksum(source_docs)
        migrated_subset = {
            namespace: after[namespace]
            for namespace in source_docs
        }
        target_checksum = state_documents_checksum(migrated_subset)
        return StateStoreMigrationResult(
            source_backend=str(source.status().get("backend") or "unknown"),
            target_backend=str(target.status().get("backend") or "unknown"),
            source_documents=len(source_docs),
            target_documents_before=len(before),
            copied_documents=len(inserted),
            existing_equal_documents=existing_equal,
            source_checksum=source_checksum,
            target_checksum=target_checksum,
            inserted_namespaces=tuple(inserted),
            verified=source_checksum == target_checksum,
        )

    def rollback_destination(
        self,
        source: StateStore,
        target: StateStore,
        result: StateStoreMigrationResult,
    ) -> tuple[str, ...]:
        source_docs = source.documents()
        removed: list[str] = []
        for namespace in result.inserted_namespaces:
            expected = source_docs.get(namespace)
            current = target.get(namespace)
            if expected is None or current is None:
                continue
            if _canonical_bytes(current) != _canonical_bytes(expected):
                raise StateStoreMigrationConflict(
                    f"refusing rollback because destination changed: {namespace}"
                )
            if target.delete(namespace):
                removed.append(namespace)
        return tuple(removed)


def build_state_store(
    *,
    sqlite_path: Path,
    backend: str,
    postgres_dsn: str | None = None,
    initialize: bool = True,
) -> StateStore:
    normalized = str(backend or "sqlite").strip().casefold()
    if normalized in {"sqlite", "local"}:
        from codex_web.storage.sqlite_state import SQLiteStateStore

        return SQLiteStateStore(sqlite_path, initialize=initialize)
    if normalized in {"postgres", "postgresql"}:
        if not postgres_dsn:
            raise RuntimeError(
                "CODEX_WEB_POSTGRES_DSN is required for PostgreSQL state storage"
            )
        from codex_web.storage.postgres_state import PostgresStateStore

        return PostgresStateStore(postgres_dsn, initialize=initialize)
    raise RuntimeError(f"unsupported state-store backend: {backend}")
