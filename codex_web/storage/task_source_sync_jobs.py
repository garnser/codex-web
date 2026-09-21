from __future__ import annotations

from collections.abc import Callable

from codex_web.storage.state_store import StateStore
from codex_web.task_source_sync_jobs import (
    GitLabSyncJob,
    GitLabSyncJobState,
    GitLabSyncJobStatus,
)


class GitLabSyncJobStore:
    NAMESPACE = "gitlab_sync_jobs"

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def load(self) -> GitLabSyncJobState:
        return GitLabSyncJobState.model_validate(
            self.store.get(self.NAMESPACE) or {}
        )

    def update(
        self,
        updater: Callable[[GitLabSyncJobState], GitLabSyncJobState],
    ) -> GitLabSyncJobState:
        raw = self.store.update(
            self.NAMESPACE,
            lambda current: updater(
                GitLabSyncJobState.model_validate(current or {})
            ).model_dump(mode="json"),
            default={},
        )
        return GitLabSyncJobState.model_validate(raw)

    def get(self, job_id: str) -> GitLabSyncJob | None:
        return next(
            (
                item
                for item in self.load().jobs
                if item.id == job_id
            ),
            None,
        )

    def active_for_scope(self, scope_key: str) -> GitLabSyncJob | None:
        candidates = [
            item
            for item in self.load().jobs
            if item.scope_key == scope_key
            and item.status in {
                GitLabSyncJobStatus.QUEUED,
                GitLabSyncJobStatus.RUNNING,
            }
        ]
        return max(
            candidates,
            key=lambda item: (item.updated_at, item.id),
            default=None,
        )
