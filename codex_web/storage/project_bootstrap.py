from __future__ import annotations

from typing import Callable

from codex_web.bootstrap_engine import (
    ProjectBootstrapExecution,
    ProjectBootstrapState,
)
from codex_web.storage.state_store import StateStore


class ProjectBootstrapStore:
    NAMESPACE = "project_bootstrap"

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def load(self) -> ProjectBootstrapState:
        return ProjectBootstrapState.model_validate(
            self.store.get(self.NAMESPACE) or {}
        )

    def update(
        self,
        updater: Callable[
            [ProjectBootstrapState],
            ProjectBootstrapState,
        ],
    ) -> ProjectBootstrapState:
        raw = self.store.update(
            self.NAMESPACE,
            lambda current: updater(
                ProjectBootstrapState.model_validate(current or {})
            ).model_dump(mode="json"),
            default={},
        )
        return ProjectBootstrapState.model_validate(raw)

    def executions_for_project(
        self,
        project_id: str,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> tuple[ProjectBootstrapExecution, ...]:
        values = [
            item
            for item in self.load().executions
            if item.project_id == project_id
            and item.organization_id == organization_id
            and item.workspace_id == workspace_id
        ]
        return tuple(
            sorted(
                values,
                key=lambda item: (item.updated_at, item.id),
                reverse=True,
            )
        )
