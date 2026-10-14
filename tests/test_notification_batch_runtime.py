import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError

from codex_web.agent_profiles import (
    AgentProfileCreate,
    AgentProfileUpdate,
)
from codex_web.authority import AUTHORITY_ROLE_CATALOG_ID, AUTHORITY_ROLE_CATALOG_KIND
from codex_web.configuration import (
    ConfigurationDraftCreate,
    ConfigurationPublishRequest,
    ConfigurationScope,
)
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.models import Project, QueuedTurn, WorkItemState
from codex_web.resources import (
    ResourceCreate,
    ResourceType,
    ResourceUpdate,
    ResourceLifecycle,
)
from codex_web.services.agent_profiles import AgentProfileService
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.configuration import ConfigurationService
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.execution_profile_definitions import (
    install_execution_profile_definitions,
)
from codex_web.services.notification_batch_runtime import (
    ENABLED,
    MAX_BYTES,
    MAX_MEMBERS,
    NotificationBatchRuntimeControls,
    install_notification_batch_configuration,
)
from codex_web.services.resources import ResourceCatalogService
from codex_web.storage.agent_profiles import AgentProfileStore
from codex_web.storage.configuration_registry import ConfigurationRegistryStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.notification_batches import (
    NotificationBatchPlan,
    select_notification_batch,
)
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.storage.execution_preflight import ExecutionPreflightStore


class NotificationBatchRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = SQLiteStateStore(Path(self.temp.name) / "state.sqlite")
        self.configuration = ConfigurationService(
            ConfigurationRegistryStore(self.state)
        )
        install_notification_batch_configuration(self.configuration)
        self.definitions = DefinitionRegistryService(
            DefinitionRegistryStore(self.state)
        )
        self.resources = ResourceCatalogService(ResourceCatalogStore(self.state))
        self.authority = install_authority_roles(self.definitions, self.resources)
        self.execution_profiles = install_execution_profile_definitions(
            self.definitions
        )
        self.profiles = AgentProfileService(
            AgentProfileStore(self.state),
            definitions=self.definitions,
            authority=self.authority,
            execution_profiles=self.execution_profiles,
        )
        self.actor = AuthenticationActor(
            identity_id="local-admin",
            organization_id="local",
            workspace_id="default",
            principal_kind=PrincipalKind.HUMAN,
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA,
        )
        self.project = Project(
            id="project-a", name="Synthetic fixture", path="/fixture"
        )
        self.repo = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY, name="Synthetic repository"
            ),
            actor=self.actor,
        )
        self.resources.bind_project(
            project=self.project, resource_id=self.repo.id, actor=self.actor
        )
        self.profile = self.profiles.create(
            AgentProfileCreate(
                profile_id="fixture-agent",
                name="Fixture agent",
                execution_profile_id="repository-write",
            ),
            actor=self.actor,
        )
        self.items = [
            QueuedTurn(
                id="original-" + str(i),
                execution_id="requested-" + str(i),
                thread_id="thread-a",
                project_id=self.project.id,
                message="Full synthetic original " + str(i),
                created_at=i,
                source="gitlab",
                work_item_ref="fixture/repo#" + str(i),
                repository_resource_id=self.repo.id,
                writable_repository_resource_ids=(self.repo.id,),
                execution_profile_id="repository-write",
                agent_profile_id=self.profile.profile_id,
                agent_profile_revision=self.profile.revision,
                agent_profile_actor_id=self.actor.identity_id,
            )
            for i in (1, 2, 3)
        ]
        self.work = {
            item.work_item_ref: WorkItemState(
                ref=item.work_item_ref,
                project_id=self.project.id,
                resource_ids=[self.repo.id],
                last_meaningful_update_at=1,
                updated_at=1,
                created_at=1,
            )
            for item in self.items
        }
        self.controls = NotificationBatchRuntimeControls(
            configuration=self.configuration,
            projects=SimpleNamespace(get=lambda project_id: self.project),
            profiles=self.profiles,
            execution_profiles=self.execution_profiles,
            authority=self.authority,
            resources=self.resources,
            actor_resolver=lambda identity, project: self.actor,
            work_item=lambda ref: self.work[ref],
        )
        self.preflight = ExecutionPreflightService(ExecutionPreflightStore(self.state))
        self.attempts = {
            item.id: self.preflight.retain_queued(
                item, organization_id="local", workspace_id="default"
            )
            for item in self.items
        }

    def publish(self, key=ENABLED, value=True, **changes):
        values = dict(
            key=key,
            value=value,
            scope_type=ConfigurationScope.PROJECT,
            scope_id=self.project.id,
            actor="fixture-admin",
        )
        values.update(changes)
        draft = self.configuration.create_draft(ConfigurationDraftCreate(**values))
        return self.configuration.publish(
            draft.id, ConfigurationPublishRequest(actor="fixture-admin")
        )

    def plan(self, control):
        return select_notification_batch(
            self.items,
            anchor_id=self.items[0].id,
            attempts_by_original_id=self.attempts,
            verified_notification_ids=frozenset(self.attempts),
            organization_id="local",
            workspace_id="default",
            notification_source_origin="gitlab",
            authority_scope_sha256_by_original_id={
                key: control.fingerprint() for key in self.attempts
            },
            max_members=control.max_members,
            max_context_bytes=control.max_context_bytes,
            control_snapshot=control,
        )

    def test_default_disabled_and_published_values_have_exact_attribution(self):
        self.assertFalse(self.controls.policy_for(self.actor, self.project)["enabled"])
        self.assertIsNone(self.controls.resolve(self.items[0], self.project))
        record = self.publish()
        control = self.controls.resolve(self.items[0], self.project)
        self.assertIsNotNone(control)
        self.assertEqual(
            json.loads(control.configuration_pins_json[0])["record_id"], record.id
        )
        self.assertEqual(control.agent_profile_record_id, self.profile.record_id)
        self.assertEqual(
            control.execution_definition_ref.definition_id, "execution.profiles.default"
        )
        self.assertEqual(
            control.authority_definition_ref.definition_id, "authority.roles.default"
        )
        self.assertEqual(control, self.controls.resolve(self.items[1], self.project))

    def test_new_plans_roundtrip_controls_and_old_shapes_remain_compatible(self):
        self.publish()
        control = self.controls.resolve(self.items[0], self.project)
        plan = self.plan(control)
        self.assertEqual(plan.schema_version, "1.1")
        self.assertEqual(
            NotificationBatchPlan.model_validate_json(plan.model_dump_json()), plan
        )
        with self.assertRaises(ValidationError):
            NotificationBatchPlan.model_validate(
                {**plan.model_dump(mode="json"), "schema_version": "1.0"}
            )
        old = select_notification_batch(
            self.items,
            anchor_id=self.items[0].id,
            attempts_by_original_id=self.attempts,
            verified_notification_ids=frozenset(self.attempts),
            organization_id="local",
            workspace_id="default",
            notification_source_origin="gitlab",
            authority_scope_sha256_by_original_id={
                key: "a" * 64 for key in self.attempts
            },
            max_members=3,
            max_context_bytes=65536,
        )
        self.assertEqual(old.schema_version, "1.0")
        self.assertNotIn("control_snapshot", old.model_dump(mode="json"))

    def test_configuration_revision_and_profile_access_changes_fence_captured_controls(
        self,
    ):
        self.publish()
        original = self.controls.resolve(self.items[0], self.project)
        self.publish(MAX_MEMBERS, 3)
        changed = self.controls.resolve(self.items[0], self.project)
        self.assertNotEqual(original.fingerprint(), changed.fingerprint())
        self.profiles.update(
            self.profile.profile_id,
            AgentProfileUpdate(name="Revised fixture", reason="Synthetic revision"),
            actor=self.actor,
        )
        updated = self.controls.resolve(self.items[0], self.project)
        self.assertEqual(updated.agent_profile_revision, 1)
        self.assertNotEqual(changed.profile_access_json, updated.profile_access_json)

    def test_scope_conflicts_and_disabled_repository_refuse_grouping(self):
        self.publish()
        for changes in (
            {"project_id": "foreign"},
            {"workspace_id": "foreign"},
            {"organization_id": "foreign"},
        ):
            original = self.work[self.items[0].work_item_ref]
            self.work[original.ref] = original.model_copy(update=changes)
            self.assertIsNone(self.controls.resolve(self.items[0], self.project))
            self.work[original.ref] = original
        self.resources.update(
            self.repo.id,
            ResourceUpdate(lifecycle=ResourceLifecycle.DISABLED),
            actor=self.actor,
        )
        self.assertIsNone(self.controls.resolve(self.items[0], self.project))

    def test_current_authority_revocation_refuses_new_batches(self):
        self.publish()
        active = self.authority.catalog_record(
            actor=self.actor, project_id=self.project.id
        )
        payload = {**active.payload, "bindings": []}
        draft = self.definitions.create_draft(
            DefinitionDraftCreate(
                definition_id=AUTHORITY_ROLE_CATALOG_ID,
                kind=AUTHORITY_ROLE_CATALOG_KIND,
                definition_schema_version="1.0",
                payload=payload,
                actor="fixture-admin",
            )
        )
        self.definitions.approve_publication(
            draft.record_id,
            actor="fixture-reviewer",
            reference="Synthetic review",
            reason="Fixture authority revocation",
        )
        self.definitions.publish(
            draft.record_id,
            DefinitionPublishRequest(
                actor="fixture-admin", expected_active_revision=active.revision
            ),
        )
        self.assertIsNone(self.controls.resolve(self.items[0], self.project))

    def test_bounded_profile_input_budget_constrains_batch_context(self):
        self.publish()
        revised = self.profiles.update(
            self.profile.profile_id,
            AgentProfileUpdate(
                budgets={"max_input_tokens": 8192}, reason="Synthetic budget"
            ),
            actor=self.actor,
        )
        item = self.items[0].model_copy(
            update={"agent_profile_revision": revised.revision}
        )
        self.assertEqual(
            self.controls.resolve(item, self.project).max_context_bytes, 8192
        )

    def test_foreign_project_configuration_cannot_enable_this_project(self):
        self.publish(scope_id="foreign-project")
        self.assertFalse(self.controls.policy_for(self.actor, self.project)["enabled"])

    def test_deployment_kill_switch_overrides_project_enablement(self):
        self.publish()
        self.assertIsNotNone(self.controls.resolve(self.items[0], self.project))
        self.publish(
            value=False,
            scope_type=ConfigurationScope.DEPLOYMENT,
            scope_id=None,
            force_disabled=True,
        )
        self.assertFalse(self.controls.policy_for(self.actor, self.project)["enabled"])
        self.assertIsNone(self.controls.resolve(self.items[0], self.project))

    def test_coordinated_scope_cannot_absorb_an_unrequested_work_item_repository(self):
        self.publish()
        repos = []
        for name in ("second", "unrequested"):
            resource = self.resources.create(
                ResourceCreate(
                    resource_type=ResourceType.REPOSITORY, name="Synthetic " + name
                ),
                actor=self.actor,
            )
            self.resources.bind_project(
                project=self.project, resource_id=resource.id, actor=self.actor
            )
            repos.append(resource)
        item = self.items[0].model_copy(
            update={"writable_repository_resource_ids": (self.repo.id, repos[0].id)}
        )
        work = self.work[item.work_item_ref]
        self.work[work.ref] = work.model_copy(update={"resource_ids": [repos[1].id]})
        self.assertIsNone(self.controls.resolve(item, self.project))

    def test_invalid_bounds_cannot_publish(self):
        for key, value in (
            (MAX_MEMBERS, 1),
            (MAX_MEMBERS, 17),
            (MAX_BYTES, 255),
            (MAX_BYTES, 262145),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.publish(key, value)

    def test_owned_drain_uses_real_configuration_definitions_and_authority_for_one_fixture_turn(
        self,
    ):
        from tests.test_turn_execution import TurnExecutionStartTests
        from codex_web.agent_profiles import AgentProfileExecutionBinding
        from codex_web.execution_workers import ExecutionRuntimeBinding
        from codex_web.compatibility import CanonicalEventEnvelope
        from codex_web.notification_provenance import ProviderNotificationPin
        from codex_web.services.coordination import StateStoreCoordinationBackend
        from codex_web.services.replicated_ownership import ReplicatedOwnershipService
        from codex_web.services.notification_batch_drain import NotificationBatchDrain
        from codex_web.services.notification_batches import NotificationBatchCoordinator
        from codex_web.storage.canonical_events import CanonicalEventStore
        from codex_web.storage.turn_queue import TurnQueueRepository
        from unittest.mock import AsyncMock, MagicMock

        self.publish()
        events = CanonicalEventStore(self.state)
        originals = []
        for index, item in enumerate(self.items):
            event = CanonicalEventEnvelope(
                event_id="fixture-event-" + str(index),
                event_type="task_source.event",
                occurred_at=1,
                source="gitlab:fixture",
                tenant_id="local",
                workspace_id="default",
                payload={
                    "project_id": self.project.id,
                    "provider_event_type": "issue.updated",
                },
            )
            events.record_if_new(event, idempotency_key=event.event_id)
            original = item.model_copy(
                update={
                    "id": "pinned-" + item.id,
                    "provider_notification_pin": ProviderNotificationPin.from_event(
                        event,
                        source_origin="gitlab",
                        provider_event_kind="issue",
                        work_item_ref=item.work_item_ref,
                        message=item.message,
                    ),
                }
            )
            originals.append(original)
        queues = TurnQueueRepository(self.state, Path(self.temp.name) / "queues.json")
        queues.put("thread-a", originals)
        host, binding, sessions, runtime = TurnExecutionStartTests()._service()
        runtime.queued_preflight = self.preflight
        runtime.actor_resolver = self.controls.actor_resolver
        runtime.ownership = ReplicatedOwnershipService(
            StateStoreCoordinationBackend(self.state), instance_id="fixture-owner"
        )
        host._thread_queue = queues.get
        host._thread_queue_record = queues.get
        host._update_thread_queue_record = queues.update
        host._project = lambda project_id: self.project
        profile = AgentProfileExecutionBinding(
            profile_id=self.profile.profile_id,
            profile_revision=self.profile.revision,
            profile_record_id=self.profile.record_id,
            selected_provider_id="openai",
            selected_runtime_id="codex",
        )
        route = ExecutionRuntimeBinding(
            provider_id="openai", runtime_id="codex", capability_revision=1
        )
        runtime._select_runtime_binding = AsyncMock(return_value=(route, profile))
        assignment = sessions.session.validate_current()
        assignment.agent_profile = profile
        assignment.runtime_binding = route
        sessions.session.validate_current = MagicMock(return_value=assignment)
        runtime.notification_batch_handler = NotificationBatchDrain(
            coordinator=NotificationBatchCoordinator(
                queue=queues, preflight=self.preflight.store
            ),
            event_lookup=events.event,
            policy_for=self.controls.policy_for,
            control_for=self.controls.resolve,
        )
        asyncio.run(runtime.drain_thread_queue("thread-a"))
        self.assertEqual(
            [method for method, _ in sessions.session.requests].count("turn/start"), 1
        )
        self.assertEqual(queues.get("thread-a"), [])
        first = self.preflight.store.get("preflight-queued-" + originals[0].id)
        batch = self.preflight.store.get_notification_batch(first.notification_batch_id)
        self.assertEqual(batch.plan.schema_version, "1.1")
        self.assertEqual(
            batch.plan.control_snapshot.agent_profile_record_id, self.profile.record_id
        )
        self.assertEqual(
            [item.id for item in batch.plan.members], [item.id for item in originals]
        )
        self.assertEqual(batch.phase, "native_input_started")
        for attempt_id in batch.attempt_ids:
            retained = self.preflight.store.get(attempt_id)
            self.assertEqual(retained.status, "reconciliation_required")
            self.assertIsNone(retained.queued_start_receipt)
