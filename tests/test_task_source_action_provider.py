from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.action_intents import (
    ActionIntentClaimRequest,
    ActionIntentCreate,
    ActionIntentStatus,
)
from codex_web.action_providers import ActionProviderBindingCreate, ActionRequest
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    PrincipalKind,
    TenantScope,
)
from codex_web.models import Project, TaskSourceConfiguration
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.action_providers import (
    ActionExecutionService,
    ActionProviderRegistry,
)
from codex_web.services.identity import IdentityService
from codex_web.services.reference_task_source import ReferenceTaskSource
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.task_source_action_provider import (
    TASK_SOURCE_ACTION_PROVIDER_INSTANCE,
    TASK_SOURCE_ACTION_PROVIDER_TYPE,
    TASK_SOURCE_CREATE_ACTION_ID,
    TaskSourceActionProvider,
)
from codex_web.services.task_source_runtime import TaskSourceRegistry
from codex_web.services.task_sources import (
    TaskSourceCapabilities,
    TaskSourceCapability,
)
from codex_web.services.work_items import WorkItemService
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.sqlite_state import SQLiteStateStore


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
        return SimpleNamespace(
            ref=f"{source.source_type}:{source.source_instance}:{snapshot.identity.external_id}",
            source_identity=snapshot.identity,
            project_id=project_id,
        )


class _NoCreateSource(ReferenceTaskSource):
    capabilities = TaskSourceCapabilities(
        frozenset(
            capability
            for capability in TaskSourceCapability
            if capability != TaskSourceCapability.CREATE
        )
    )


class TaskSourceActionProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        identity = IdentityService(IdentityStateStore(sqlite))
        identity.bootstrap_local()
        self.actor = identity.local_trusted_actor()
        self.worker = AuthenticationActor(
            identity_id="action-worker",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="local",
            workspace_id="default",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("action-intent:worker",),
        )
        self.scope = TenantScope(
            organization_id="local",
            workspace_id="default",
        )
        self.configuration = TaskSourceConfiguration(
            source_type="reference",
            source_instance="goal-decomposition",
            scope="project-a",
        )
        self.project = Project(
            id="project-a",
            organization_id="local",
            workspace_id="default",
            name="Project A",
            path="/tmp/project-a",
            authoritative_task_source=self.configuration,
        )
        self.source = ReferenceTaskSource("goal-decomposition")
        self.task_sources = TaskSourceRegistry()
        self.task_sources.register_project(
            "reference",
            lambda configuration, project_id, scope: self.source,
        )
        self.projector = _Projector()

        self.work_items = object.__new__(WorkItemService)
        self.work_items.host = _Host(self.project)
        self.work_items.task_source_registry = self.task_sources
        self.work_items.task_source_projector = self.projector

        self.provider = TaskSourceActionProvider(self.work_items)
        self.resources = ResourceCatalogService(ResourceCatalogStore(sqlite))
        self.registry = ActionProviderRegistry(ActionProviderStateStore(sqlite))
        self.registry.register(self.provider)
        self.execution = ActionExecutionService(self.registry, self.resources)
        self.intents = ActionIntentService(
            ActionIntentStore(sqlite),
            self.execution,
        )
        self.binding = self.registry.bind(
            ActionProviderBindingCreate(
                provider_type=TASK_SOURCE_ACTION_PROVIDER_TYPE,
                provider_instance=TASK_SOURCE_ACTION_PROVIDER_INSTANCE,
                project_id="project-a",
            ),
            actor=self.actor,
            resources=self.resources,
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _request(self, **parameters):
        payload = {
            "title": "Implement accepted Goal work",
            "body": "Created only after decomposition review.",
            "owners": ["quinn"],
            "labels": ["goal::generated"],
        }
        payload.update(parameters)
        return ActionRequest(
            action_id=TASK_SOURCE_CREATE_ACTION_ID,
            organization_id="local",
            workspace_id="default",
            project_id="project-a",
            parameters=payload,
        )

    async def _execute(self, intent):
        claimed = self.intents.claim(
            ActionIntentClaimRequest(
                worker_id="worker-1",
                lease_seconds=30,
            ),
            actor=self.worker,
            intent_id=intent.id,
        )
        self.assertIsNotNone(claimed)
        return await self.intents.execute_claimed(
            intent.id,
            "worker-1",
            actor=self.worker,
        )

    async def test_prepare_resolves_create_capability_without_mutating_source(self) -> None:
        preparation = await self.execution.prepare(
            self.binding.id,
            self._request(),
            actor=self.actor,
        )

        self.assertTrue(preparation.ready)
        self.assertEqual(preparation.provider_plan["source_type"], "reference")
        self.assertEqual(
            preparation.provider_plan["source_instance"],
            "goal-decomposition",
        )
        self.assertEqual(await self.source.discover(scope="project-a"), [])
        self.assertEqual(self.projector.calls, [])

    async def test_action_intent_is_durable_before_authoritative_task_creation(self) -> None:
        intent = self.intents.create(
            ActionIntentCreate(
                binding_id=self.binding.id,
                request=self._request(),
                goal_id="goal-a",
            ),
            actor=self.actor,
        )

        self.assertEqual(intent.status, ActionIntentStatus.PENDING)
        self.assertEqual(intent.goal_id, "goal-a")
        self.assertFalse(intent.provider_idempotency_supported)
        self.assertEqual(intent.retry_policy.max_attempts, 1)
        self.assertEqual(await self.source.discover(scope="project-a"), [])

        completed = await self._execute(intent)

        self.assertEqual(completed.status, ActionIntentStatus.SUCCEEDED)
        self.assertEqual(len(self.projector.calls), 1)
        created = await self.source.discover(scope="project-a")
        self.assertEqual(len(created), 1)
        history = self.intents.history(intent.id, self.actor)
        self.assertEqual(len(history["receipts"]), 1)
        result = history["receipts"][0]["result"]
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["output"]["work_item_ref"].startswith("reference:"))
        self.assertEqual(
            result["output"]["external_id"],
            created[0].identity.external_id,
        )

    async def test_binding_project_scope_fails_before_task_source_mutation(self) -> None:
        request = self._request().model_copy(update={"project_id": "project-b"})

        with self.assertRaisesRegex(
            RuntimeError,
            "project does not match provider binding",
        ):
            self.intents.create(
                ActionIntentCreate(
                    binding_id=self.binding.id,
                    request=request,
                    goal_id="goal-a",
                ),
                actor=self.actor,
            )

        self.assertEqual(await self.source.discover(scope="project-a"), [])

    async def test_prepare_fails_closed_when_authoritative_source_lacks_create(self) -> None:
        no_create = _NoCreateSource("goal-decomposition")
        self.task_sources = TaskSourceRegistry()
        self.task_sources.register_project(
            "reference",
            lambda configuration, project_id, scope: no_create,
        )
        self.work_items.task_source_registry = self.task_sources

        with self.assertRaisesRegex(
            RuntimeError,
            "does not support capability: create",
        ):
            await self.execution.prepare(
                self.binding.id,
                self._request(),
                actor=self.actor,
            )

        self.assertEqual(await no_create.discover(scope="project-a"), [])
        self.assertEqual(self.projector.calls, [])

    async def test_request_rejects_provider_native_or_secret_parameters(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported parameters"):
            await self.execution.prepare(
                self.binding.id,
                self._request(private_token="must-not-cross-boundary"),
                actor=self.actor,
            )
        self.assertEqual(await self.source.discover(scope="project-a"), [])


if __name__ == "__main__":
    unittest.main()
