from __future__ import annotations

from typing import Callable

from codex_web.project_readiness import (
    ProjectReadinessRecord,
    ProjectReadinessSnapshot,
    ProjectReadinessState,
)
from codex_web.storage.state_store import StateStore


class ProjectReadinessStore:
    NAMESPACE = "project_readiness"

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def load(self) -> ProjectReadinessState:
        return ProjectReadinessState.model_validate(
            self.store.get(self.NAMESPACE) or {}
        )

    def update(
        self,
        updater: Callable[
            [ProjectReadinessState],
            ProjectReadinessState,
        ],
    ) -> ProjectReadinessState:
        raw = self.store.update(
            self.NAMESPACE,
            lambda current: updater(
                ProjectReadinessState.model_validate(current or {})
            ).model_dump(mode="json"),
            default={},
        )
        return ProjectReadinessState.model_validate(raw)

    def get(
        self,
        project_id: str,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> ProjectReadinessRecord | None:
        return next(
            (
                item
                for item in self.load().records
                if item.project_id == project_id
                and item.organization_id == organization_id
                and item.workspace_id == workspace_id
            ),
            None,
        )

    def record(
        self,
        snapshot: ProjectReadinessSnapshot,
    ) -> ProjectReadinessRecord:
        result: list[ProjectReadinessRecord] = []

        def mutate(state: ProjectReadinessState) -> ProjectReadinessState:
            existing = next(
                (
                    item
                    for item in state.records
                    if item.project_id == snapshot.project_id
                    and item.organization_id == snapshot.organization_id
                    and item.workspace_id == snapshot.workspace_id
                ),
                None,
            )
            last_success = (
                snapshot.generated_at
                if snapshot.execution_ready
                else (
                    existing.last_successful_verification_at
                    if existing is not None
                    else None
                )
            )
            record = ProjectReadinessRecord(
                project_id=snapshot.project_id,
                organization_id=snapshot.organization_id,
                workspace_id=snapshot.workspace_id,
                last_successful_verification_at=last_success,
                last_status=snapshot.status,
                last_correlation_id=snapshot.correlation_id,
                updated_at=snapshot.generated_at,
            )
            state.records = [
                item
                for item in state.records
                if not (
                    item.project_id == snapshot.project_id
                    and item.organization_id == snapshot.organization_id
                    and item.workspace_id == snapshot.workspace_id
                )
            ]
            state.records.append(record)
            result.append(record)
            return state

        self.update(mutate)
        return result[0]
