from __future__ import annotations

from collections.abc import Callable
from typing import Any

from codex_web.compatibility import MigrationRegistry
from codex_web.organizational_memory import (
    ORGANIZATIONAL_MEMORY_CONTRACT,
    KnowledgeRecord,
    KnowledgeRetrievalRun,
    OrganizationalMemoryState,
)
from codex_web.storage.sqlite_state import SQLiteStateStore


ORGANIZATIONAL_MEMORY_MIGRATIONS = MigrationRegistry(
    "organizational-memory-state"
)


def _origin_metadata_v11(payload: dict[str, Any]) -> dict[str, Any]:
    # Legacy source kinds/authorship cannot establish participant observation.
    return {**payload, "schema_version": "1.1"}


ORGANIZATIONAL_MEMORY_MIGRATIONS.register("1.0", "1.1", _origin_metadata_v11)


def _typed_briefs_v12(payload: dict[str, Any]) -> dict[str, Any]:
    # Existing content, source attribution and lifecycle remain unchanged.
    return {**payload, "schema_version": "1.2"}


ORGANIZATIONAL_MEMORY_MIGRATIONS.register("1.1", "1.2", _typed_briefs_v12)


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

    def append_retrieval_run(self, run: KnowledgeRetrievalRun) -> None:
        """Append to the current locked document without decoding our unused result.

        The complete current document is still validated inside the canonical
        atomic update. Concurrent Knowledge/audit changes and retention survive.
        """
        def apply(raw: Any) -> dict[str, Any]:
            state = self._decode(raw)
            state.retrieval_runs.append(run)
            if len(state.retrieval_runs) > 5000:
                state.retrieval_runs = state.retrieval_runs[-5000:]
            return state.model_dump(mode='json')
        self.store.update(self.namespace, apply, default=self.default_document())

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

    def get_for_context(self, knowledge_id: str, *, organization_id: str,
                        workspace_id: str) -> KnowledgeRecord:
        """Current complete point record, without unrelated content/audit history.

        First global ID wins before tenant filtering, as ordinary get() followed
        by its service scope check. Legacy/custom backends retain full reads.
        """
        reader = getattr(self.store, 'document_array_items_envelope', None)
        if not callable(reader) or getattr(self.store, 'supports_document_array_scope_after_first', False) is not True:
            return self.get(knowledge_id)
        raw = reader(self.namespace, 'records', (knowledge_id,),
            organization_id=organization_id, workspace_id=workspace_id,
            collection_names=('records', 'relationships', 'embeddings', 'retrieval_runs'),
            first_per_id=True, scope_after_first=True)
        # The generic envelope denotes a missing version as 0.0. Let the full
        # original document retain this domain's exact legacy/refusal behavior.
        if isinstance(raw, dict) and raw.get('schema_version') == '0.0':
            return self.get(knowledge_id)
        state = self._decode(raw)
        item = next((row for row in state.records if row.id == knowledge_id), None)
        if item is None:
            # Missing scope fields or a foreign first duplicate must retain the
            # ordinary decoder/refusal, never substitute a later local row.
            return self.get(knowledge_id)
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
