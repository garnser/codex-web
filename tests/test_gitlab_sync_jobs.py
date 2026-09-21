from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.identity import TenantScope
from codex_web.services.task_source_sync_jobs import GitLabSyncJobService
from codex_web.services.work_items import WorkItemService
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.task_source_sync_jobs import GitLabSyncJobStore
from codex_web.task_source_sync_jobs import GitLabSyncJobStatus
from codex_web.services.task_sources import TaskSourceSnapshot
from codex_web.models import TaskSourceIdentity


class _FakeWorkItems:
    def __init__(self) -> None:
        self.calls = 0
        self.release = asyncio.Event()

    async def _sync_from_gitlab_async(
        self,
        scope,
        *,
        progress=None,
        cancelled=None,
    ):
        self.calls += 1
        if progress is not None:
            progress(
                {
                    "discovered": 2,
                    "processed": 0,
                    "synced": 0,
                    "unique_refs": 0,
                    "current_external_id": None,
                }
            )
        await self.release.wait()
        if cancelled is not None and cancelled():
            return {
                "discovered": 2,
                "processed": 1,
                "synced": 1,
                "refs": 1,
                "cancelled": True,
            }
        if progress is not None:
            progress(
                {
                    "discovered": 2,
                    "processed": 2,
                    "synced": 2,
                    "unique_refs": 2,
                    "current_external_id": "group/project#2",
                }
            )
        return {
            "discovered": 2,
            "processed": 2,
            "synced": 2,
            "refs": 2,
            "cancelled": False,
        }


class GitLabSyncJobServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        state = SQLiteStateStore(
            Path(self.temp.name) / "state.sqlite3"
        )
        self.work_items = _FakeWorkItems()
        self.service = GitLabSyncJobService(
            GitLabSyncJobStore(state),
            self.work_items,
        )
        self.scope = TenantScope(
            organization_id="org-a",
            workspace_id="ws-a",
        )

    async def asyncTearDown(self) -> None:
        await self.service.stop()

    async def test_duplicate_trigger_coalesces_to_one_active_job(self) -> None:
        first = self.service.start(
            scope=self.scope,
            actor_id="admin",
        )
        second = self.service.start(
            scope=self.scope,
            actor_id="admin",
        )

        self.assertEqual(first.id, second.id)
        for _ in range(50):
            current = self.service.get(first.id, scope=self.scope)
            if current.status == GitLabSyncJobStatus.RUNNING:
                break
            await asyncio.sleep(0)

        self.assertEqual(self.work_items.calls, 1)
        self.work_items.release.set()
        await asyncio.gather(
            *list(self.service.coordinator._tasks.values())
        )
        completed = self.service.get(first.id, scope=self.scope)
        self.assertEqual(
            completed.status,
            GitLabSyncJobStatus.COMPLETED,
        )
        self.assertEqual(completed.processed, 2)
        self.assertEqual(completed.synced, 2)

    async def test_cancellation_is_durable_and_cooperative(self) -> None:
        job = self.service.start(
            scope=self.scope,
            actor_id="admin",
        )
        for _ in range(50):
            current = self.service.get(job.id, scope=self.scope)
            if current.status == GitLabSyncJobStatus.RUNNING:
                break
            await asyncio.sleep(0)

        cancelled = self.service.cancel(
            job.id,
            scope=self.scope,
        )
        self.assertTrue(cancelled.cancel_requested)
        self.work_items.release.set()
        await asyncio.gather(
            *list(self.service.coordinator._tasks.values())
        )

        final = self.service.get(job.id, scope=self.scope)
        self.assertEqual(
            final.status,
            GitLabSyncJobStatus.CANCELLED,
        )
        self.assertEqual(final.processed, 1)

    async def test_job_listing_is_tenant_scoped(self) -> None:
        first = self.service.start(
            scope=self.scope,
            actor_id="admin",
        )
        other_scope = TenantScope(
            organization_id="org-b",
            workspace_id="ws-b",
        )
        self.service.start(
            scope=other_scope,
            actor_id="admin-b",
        )

        visible = self.service.list(scope=self.scope)
        self.assertEqual([item.id for item in visible], [first.id])
        self.work_items.release.set()
        await asyncio.gather(
            *list(self.service.coordinator._tasks.values())
        )


class GitLabSyncEventLoopIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_projection_does_not_starve_event_loop(self) -> None:
        service = WorkItemService.__new__(WorkItemService)
        service.gitlab_dependencies = SimpleNamespace(
            load_routing_settings=lambda: SimpleNamespace(
                projects={
                    "project-a": SimpleNamespace(
                        enabled=True,
                    )
                }
            ),
            token_for_project=lambda _project_id: "token",
            group_path=lambda _settings: "group",
            api_base_url="https://gitlab.example/api/v4",
        )
        service.work_items = SimpleNamespace(
            load_projects=lambda: [
                SimpleNamespace(
                    id="project-a",
                    organization_id="org-a",
                    workspace_id="ws-a",
                )
            ]
        )

        snapshots = [
            TaskSourceSnapshot(
                identity=TaskSourceIdentity(
                    source_type="gitlab",
                    source_instance="https://gitlab.example/api/v4",
                    external_id=f"group/project#{index}",
                ),
                title=f"Issue {index}",
                source_state="opened",
            )
            for index in range(3)
        ]

        class _Source:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            async def discover(self, *, scope):
                self.scope = scope
                return snapshots

        class _Projector:
            def upsert(self, _source, snapshot, *, project_id):
                del project_id
                time.sleep(0.08)
                return SimpleNamespace(
                    ref=snapshot.identity.external_id
                )

        service.task_source_projector = _Projector()

        ticks = 0
        stop = asyncio.Event()

        async def ticker() -> None:
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0.005)

        tick_task = asyncio.create_task(ticker())
        try:
            with patch(
                "codex_web.services.work_items.GitLabTaskSource",
                _Source,
            ):
                result = await service._sync_from_gitlab_async(
                    TenantScope(
                        organization_id="org-a",
                        workspace_id="ws-a",
                    )
                )
        finally:
            stop.set()
            await tick_task

        self.assertEqual(result["synced"], 3)
        self.assertGreaterEqual(
            ticks,
            10,
            "event loop was starved by synchronous projection work",
        )


if __name__ == "__main__":
    unittest.main()
