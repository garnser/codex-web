from __future__ import annotations

from collections.abc import Callable
from typing import Any

from codex_web.compatibility import MigrationRegistry
from codex_web.organizational_memory import (
    ORGANIZATIONAL_MEMORY_CONTRACT,
    KnowledgeRecord,
    OrganizationalMemoryState,
)
from codex_web.storage.sqlite_state import SQLiteStateStore


ORGANIZATIONAL_MEMORY_MIGRATIONS = MigrationRegistry(
    "organizational-memory-state"
)


class KnowledgeNotFoundError(KeyError):
    pass


class KnowledgeConflictError(RuntimeError):
    pass


class OrganizationalMemoryStore:
    namespace = "organizational_memory"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> OrganizationalMemoryState:
        if payload is None:
            return OrganizationalMemoryState()
        if not isinstance(payload, dict):
            raise ValueError("organizational memory state must be an object")
        version = str(
            payload.get("schema_version")
            or ORGANIZATIONAL_MEMORY_CONTRACT.current
        )
        if version != ORGANIZATIONAL_MEMORY_CONTRACT.current:
            payload = ORGANIZATIONAL_MEMORY_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=ORGANIZATIONAL_MEMORY_CONTRACT.current,
            )
        ORGANIZATIONAL_MEMORY_CONTRACT.require(
            payload.get("schema_version", "")
        )
        return OrganizationalMemoryState.model_validate(payload)

    def default_document(self) -> dict[str, Any]:
        return OrganizationalMemoryState().model_dump(mode="json")

    def load(self) -> OrganizationalMemoryState:
        return self._decode(self.store.get(self.namespace))

    def list(
        self,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> tuple[KnowledgeRecord, ...]:
        rows = [
            item
            for item in self.load().records
            if item.organization_id == organization_id
            and item.workspace_id == workspace_id
        ]
        rows.sort(
            key=lambda item: (
                item.logical_key,
                item.version,
                item.created_at,
            ),
            reverse=True,
        )
        return tuple(rows)

    def get(self, knowledge_id: str) -> KnowledgeRecord:
        item = next(
            (
                row
                for row in self.load().records
                if row.id == knowledge_id
            ),
            None,
        )
        if item is None:
            raise KnowledgeNotFoundError(knowledge_id)
        return item

    def create(self, item: KnowledgeRecord) -> KnowledgeRecord:
        def apply(raw: Any) -> dict[str, Any]:
            state = self._decode(raw)
            if any(row.id == item.id for row in state.records):
                raise KnowledgeConflictError(
                    f"knowledge record already exists: {item.id}"
                )
            state.records.append(item)
            return state.model_dump(mode="json")

        self.store.update(
            self.namespace,
            apply,
            default=self.default_document(),
        )
        return item

    def update(
        self,
        knowledge_id: str,
        updater: Callable[
            [OrganizationalMemoryState, KnowledgeRecord],
            tuple[OrganizationalMemoryState, KnowledgeRecord],
        ],
    ) -> KnowledgeRecord:
        result: dict[str, KnowledgeRecord] = {}

        def apply(raw: Any) -> dict[str, Any]:
            state = self._decode(raw)
            current = next(
                (row for row in state.records if row.id == knowledge_id),
                None,
            )
            if current is None:
                raise KnowledgeNotFoundError(knowledge_id)
            updated_state, updated = updater(state, current)
            if updated.id != current.id:
                raise ValueError("memory updater cannot change knowledge id")
            result["value"] = updated
            return updated_state.model_dump(mode="json")

        self.store.update(
            self.namespace,
            apply,
            default=self.default_document(),
        )
        return result["value"]

    def update_state(
        self,
        updater: Callable[[OrganizationalMemoryState], OrganizationalMemoryState],
    ) -> OrganizationalMemoryState:
        def apply(raw: Any) -> dict[str, Any]:
            return updater(self._decode(raw)).model_dump(mode="json")

        payload = self.store.update(
            self.namespace,
            apply,
            default=self.default_document(),
        )
        return self._decode(payload)
