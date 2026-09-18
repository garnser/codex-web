from __future__ import annotations

import unittest

from codex_web.identity import TenantScope
from codex_web.models import Project, TaskSourceConfiguration
from codex_web.services.reference_task_source import ReferenceTaskSource
from codex_web.services.task_source_runtime import (
    TaskSourceRegistry,
    TaskSourceResolutionError,
)
from codex_web.services.task_sources import (
    TaskSourceCapabilities,
    TaskSourceCapability,
    TaskSourceCreateRequest,
    UnsupportedTaskSourceCapability,
)
from codex_web.services.work_items import WorkItemService


class _Host:
    def __init__(self, project: Project) -> None:
        self.project = project

    def _load_projects(self):
        return [self.project]


class _Projector:
    def __init__(self) -> None:
        self.calls = []

    def upsert(self, source, snapshot, *, project_id):
        self.calls.append((source, snapshot, project_id))
        return {
            "project_id": project_id,
            "source_identity": snapshot.identity,
            "title": snapshot.title,
        }


class _NoCreateSource(ReferenceTaskSource):
    capabilities = TaskSourceCapabilities(
        frozenset(
            capability
            for capability in TaskSourceCapability
            if capability != TaskSourceCapability.CREATE
        )
    )


class WorkItemAuthoritativeCreateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.scope = TenantScope(
            organization_id="org-a",
            workspace_id="ws-a",
        )
        self.configuration = TaskSourceConfiguration(
            source_type="reference",
            source_instance="goal-decomposition",
            scope="project-a",
        )
        self.project = Project(
            id="project-a",
            organization_id="org-a",
            workspace_id="ws-a",
            name="Project A",
            path="/tmp/project-a",
            authoritative_task_source=self.configuration,
        )
        self.registry = TaskSourceRegistry()
        self.projector = _Projector()
        self.service = object.__new__(WorkItemService)
        self.service.host = _Host(self.project)
        self.service.task_source_registry = self.registry
        self.service.task_source_projector = self.projector

    async def test_create_authoritative_projects_only_returned_provider_identity(self) -> None:
        source = ReferenceTaskSource("goal-decomposition")
        self.registry.register_project(
            "reference",
            lambda configuration, project_id, scope: source,
        )

        result = await self.service.create_authoritative(
            "project-a",
            TaskSourceCreateRequest(
                title="Implement accepted Goal work",
                body="Canonical task body",
                owners=("quinn",),
                labels=("priority::P1",),
            ),
            scope=self.scope,
        )

        self.assertEqual(result["project_id"], "project-a")
        self.assertEqual(result["title"], "Implement accepted Goal work")
        identity = result["source_identity"]
        self.assertEqual(identity.source_type, "reference")
        self.assertEqual(identity.source_instance, "goal-decomposition")
        self.assertTrue(identity.external_id.startswith("TASK-"))
        self.assertEqual(len(self.projector.calls), 1)
        created = await source.read(identity)
        self.assertEqual(created.title, "Implement accepted Goal work")
        self.assertEqual(created.owners, ("quinn",))
        self.assertEqual(created.labels, ("priority::P1",))

    async def test_missing_authoritative_source_fails_before_projection(self) -> None:
        self.project = self.project.model_copy(
            update={"authoritative_task_source": None}
        )
        self.service.host = _Host(self.project)

        with self.assertRaisesRegex(
            TaskSourceResolutionError,
            "no authoritative task source",
        ):
            await self.service.create_authoritative(
                "project-a",
                TaskSourceCreateRequest(title="Must not become shadow work"),
                scope=self.scope,
            )

        self.assertEqual(self.projector.calls, [])

    async def test_missing_create_capability_fails_before_provider_mutation(self) -> None:
        source = _NoCreateSource("goal-decomposition")
        self.registry.register_project(
            "reference",
            lambda configuration, project_id, scope: source,
        )

        with self.assertRaises(UnsupportedTaskSourceCapability):
            await self.service.create_authoritative(
                "project-a",
                TaskSourceCreateRequest(title="Unsupported creation"),
                scope=self.scope,
            )

        self.assertEqual(self.projector.calls, [])
        self.assertEqual(await source.discover(scope="project-a"), [])

    async def test_cross_tenant_project_is_not_resolved(self) -> None:
        with self.assertRaisesRegex(LookupError, "Project not found"):
            await self.service.create_authoritative(
                "project-a",
                TaskSourceCreateRequest(title="Cross-tenant attempt"),
                scope=TenantScope(
                    organization_id="org-b",
                    workspace_id="ws-b",
                ),
            )

        self.assertEqual(self.projector.calls, [])


if __name__ == "__main__":
    unittest.main()
