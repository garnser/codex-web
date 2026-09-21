from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from codex_web.identity import TenantScope
from codex_web.services.keyed_background_tasks import KeyedTaskCoordinator
from codex_web.storage.task_source_sync_jobs import GitLabSyncJobStore
from codex_web.task_source_sync_jobs import (
    GitLabSyncJob,
    GitLabSyncJobStatus,
)


class GitLabSyncJobError(RuntimeError):
    pass


class GitLabSyncJobNotFound(GitLabSyncJobError):
    pass


class GitLabSyncJobService:
    """Durable state + bounded in-process execution for GitLab bulk sync."""

    def __init__(
        self,
        store: GitLabSyncJobStore,
        work_items: Any,
        *,
        coordinator: KeyedTaskCoordinator | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.work_items = work_items
        self.coordinator = coordinator or KeyedTaskCoordinator(
            max_concurrency=2,
            per_scope_concurrency=1,
        )
        self.clock = clock

    @staticmethod
    def scope_key(scope: TenantScope) -> str:
        return (
            f"gitlab-sync:{scope.organization_id}:"
            f"{scope.workspace_id}"
        )

    def _save(self, job: GitLabSyncJob) -> GitLabSyncJob:
        saved: list[GitLabSyncJob] = []

        def mutate(state):
            state.jobs = [
                item for item in state.jobs if item.id != job.id
            ]
            state.jobs.append(job)
            saved.append(job)
            return state

        self.store.update(mutate)
        return saved[0]

    def get(
        self,
        job_id: str,
        *,
        scope: TenantScope,
    ) -> GitLabSyncJob:
        job = self.store.get(job_id)
        if (
            job is None
            or job.organization_id != scope.organization_id
            or job.workspace_id != scope.workspace_id
        ):
            raise GitLabSyncJobNotFound("GitLab sync job not found")
        return job

    def list(
        self,
        *,
        scope: TenantScope,
        limit: int = 20,
    ) -> tuple[GitLabSyncJob, ...]:
        rows = [
            item
            for item in self.store.load().jobs
            if item.organization_id == scope.organization_id
            and item.workspace_id == scope.workspace_id
        ]
        rows.sort(
            key=lambda item: (item.updated_at, item.id),
            reverse=True,
        )
        return tuple(rows[: max(1, min(int(limit), 100))])

    def _cancel_requested(self, job_id: str) -> bool:
        job = self.store.get(job_id)
        return bool(
            job is None
            or job.cancel_requested
            or job.status == GitLabSyncJobStatus.CANCELLED
        )

    def _progress(
        self,
        job_id: str,
        update: dict[str, Any],
    ) -> None:
        job = self.store.get(job_id)
        if job is None:
            return
        for field in (
            "discovered",
            "processed",
            "synced",
            "unique_refs",
            "current_external_id",
        ):
            if field in update:
                setattr(job, field, update[field])
        job.updated_at = self.clock()
        self._save(job)

    async def _run(
        self,
        job_id: str,
        scope: TenantScope,
    ) -> None:
        job = self.store.get(job_id)
        if job is None:
            return
        if job.cancel_requested:
            job.status = GitLabSyncJobStatus.CANCELLED
            job.completed_at = self.clock()
            job.updated_at = job.completed_at
            self._save(job)
            return

        job.status = GitLabSyncJobStatus.RUNNING
        job.started_at = job.started_at or self.clock()
        job.updated_at = self.clock()
        job.last_error = None
        self._save(job)

        try:
            result = await self.work_items.sync_from_gitlab(
                scope,
                progress=lambda update: self._progress(
                    job_id,
                    update,
                ),
                cancelled=lambda: self._cancel_requested(job_id),
            )
        except BaseException as exc:
            latest = self.store.get(job_id) or job
            if latest.cancel_requested or isinstance(
                exc,
                __import__("asyncio").CancelledError,
            ):
                latest.status = GitLabSyncJobStatus.CANCELLED
                latest.last_error = None
            else:
                latest.status = GitLabSyncJobStatus.FAILED
                latest.last_error = str(exc)[:500]
            latest.completed_at = self.clock()
            latest.updated_at = latest.completed_at
            self._save(latest)
            raise

        latest = self.store.get(job_id) or job
        latest.discovered = int(result.get("discovered") or 0)
        latest.processed = int(result.get("processed") or 0)
        latest.synced = int(result.get("synced") or 0)
        latest.unique_refs = int(result.get("refs") or 0)
        latest.current_external_id = None
        latest.status = (
            GitLabSyncJobStatus.CANCELLED
            if bool(result.get("cancelled"))
            else GitLabSyncJobStatus.COMPLETED
        )
        latest.completed_at = self.clock()
        latest.updated_at = latest.completed_at
        self._save(latest)

    def start(
        self,
        *,
        scope: TenantScope,
        actor_id: str,
    ) -> GitLabSyncJob:
        del actor_id
        key = self.scope_key(scope)
        active = self.store.active_for_scope(key)
        if active is None:
            active = GitLabSyncJob(
                organization_id=scope.organization_id,
                workspace_id=scope.workspace_id,
                scope_key=key,
                updated_at=self.clock(),
            )
            active = self._save(active)

        self.coordinator.schedule(
            key,
            lambda: self._run(active.id, scope),
            revision=active.id,
            scope=key,
        )
        return self.store.get(active.id) or active

    def cancel(
        self,
        job_id: str,
        *,
        scope: TenantScope,
    ) -> GitLabSyncJob:
        job = self.get(job_id, scope=scope)
        if job.status in {
            GitLabSyncJobStatus.COMPLETED,
            GitLabSyncJobStatus.FAILED,
            GitLabSyncJobStatus.CANCELLED,
        }:
            return job
        job.cancel_requested = True
        job.updated_at = self.clock()
        job = self._save(job)
        # User cancellation is cooperative. The currently executing
        # projection is allowed to reach its persistence boundary; the sync
        # loop observes this durable flag before starting another snapshot.
        return self.store.get(job.id) or job

    async def stop(self) -> None:
        await self.coordinator.stop()
