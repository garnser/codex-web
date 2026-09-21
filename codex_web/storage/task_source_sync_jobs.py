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

    def get_or_create_active(
        self,
        candidate: GitLabSyncJob,
    ) -> GitLabSyncJob:
        selected: list[GitLabSyncJob] = []

        def mutate(state):
            active = [
                item
                for item in state.jobs
                if item.scope_key == candidate.scope_key
                and item.status in {
                    GitLabSyncJobStatus.QUEUED,
                    GitLabSyncJobStatus.RUNNING,
                }
            ]
            if active:
                current = max(
                    active,
                    key=lambda item: (item.updated_at, item.id),
                )
                selected.append(current)
                return state
            state.jobs.append(candidate)
            selected.append(candidate)
            return state

        self.update(mutate)
        return selected[0]

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
