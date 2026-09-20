from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable
from urllib.parse import quote, unquote

from pydantic import BaseModel, ConfigDict, Field


_RECORD_STORAGE_ROOT = "__codex_records__/"


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
    def record_get(self, namespace: str, key: str) -> Any | None: ...
    def record_items(self, namespace: str) -> dict[str, Any]: ...
    def record_apply(
        self,
        namespace: str,
        *,
        upserts: dict[str, Any],
        deletes: tuple[str, ...] = (),
    ) -> None: ...
    def record_replace(
        self,
        namespace: str,
        records: dict[str, Any],
    ) -> None: ...
    def contains(self, namespace: str) -> bool: ...
    def delete(self, namespace: str) -> bool: ...
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
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def state_documents_checksum(documents: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for namespace in sorted(documents):
        digest.update(namespace.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(hashlib.sha256(_canonical_bytes(documents[namespace])).digest())
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
) -> StateStore:
    normalized = str(backend or "sqlite").strip().casefold()
    if normalized in {"sqlite", "local"}:
        from codex_web.storage.sqlite_state import SQLiteStateStore

        return SQLiteStateStore(sqlite_path)
    if normalized in {"postgres", "postgresql"}:
        if not postgres_dsn:
            raise RuntimeError(
                "CODEX_WEB_POSTGRES_DSN is required for PostgreSQL state storage"
            )
        from codex_web.storage.postgres_state import PostgresStateStore

        return PostgresStateStore(postgres_dsn)
    raise RuntimeError(f"unsupported state-store backend: {backend}")
