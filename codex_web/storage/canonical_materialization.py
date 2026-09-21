from __future__ import annotations

from codex_web.canonical_materialization import (
    CanonicalMaterializationExecution,
    CanonicalMaterializationState,
)
from codex_web.storage.state_store import StateStore


class CanonicalMaterializationStore:
    NAMESPACE = "canonical_materialization"

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def load(self) -> CanonicalMaterializationState:
        raw = self.store.get(self.NAMESPACE)
        return CanonicalMaterializationState.model_validate(
            raw or {}
        )

    def update(self, updater) -> CanonicalMaterializationState:
        raw = self.store.update(
            self.NAMESPACE,
            lambda current: updater(
                CanonicalMaterializationState.model_validate(
                    current or {}
                )
            ).model_dump(mode="json"),
            default={},
        )
        return CanonicalMaterializationState.model_validate(raw)

    def executions_for_project(
        self,
        project_id: str,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> tuple[CanonicalMaterializationExecution, ...]:
        return tuple(
            item
            for item in self.load().executions
            if item.project_id == project_id
            and item.organization_id == organization_id
            and item.workspace_id == workspace_id
        )
