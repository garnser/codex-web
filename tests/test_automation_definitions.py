from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from codex_web.automation_definitions import (
    AUTOMATION_KIND,
    AUTOMATION_SCHEMA_VERSION,
    AutomationDefinition,
    AutomationLifecycle,
    AutomationTriggerType,
)
from codex_web.canonical_events import CanonicalEventType
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest, DefinitionScope
from codex_web.services.automation_definitions import (
    AutomationEventTriggerService,
    AutomationScheduleMaterializationError,
    AutomationScheduleMaterializer,
    install_automation_definitions,
)
from codex_web.services.canonical_events import (
    CanonicalEventBus,
    CanonicalEventIngestionService,
)
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.services.scheduler import SchedulerService
from codex_web.storage.scheduler import SchedulerStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class AutomationDefinitionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.registry = DefinitionRegistryService(DefinitionRegistryStore(store))
        self.automations = install_automation_definitions(self.registry)
        self.event_bus = CanonicalEventBus(CanonicalEventStore(store))
        self.events = CanonicalEventIngestionService(self.event_bus)
        self.scheduler = SchedulerService(
            SchedulerStore(store),
            self.events,
            clock=lambda: 100.0,
        )
        self.schedule_materializer = AutomationScheduleMaterializer(
            self.automations,
            self.scheduler,
            clock=lambda: 100.0,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _payload(self) -> dict[str, object]:
        return {
            "name": "Nightly repository health",
            "description": "Run canonical repository checks.",
            "lifecycle": "enabled",
            "trigger": {
                "type": "recurring_schedule",
                "cron": "0 2 * * *",
                "timezone": "Europe/Stockholm",
            },
            "target": {"kind": "agent_profile", "id": "agent-james"},
            "instructions": "Inspect repository health and create work only for actionable failures.",
            "budget": {
                "max_input_tokens": 20000,
                "max_output_tokens": 4000,
                "max_cost_usd": 2.5,
                "max_duration_seconds": 900,
                "max_concurrency": 1,
            },
            "retry": {"max_attempts": 3, "backoff_seconds": 60},
            "dedupe_key_template": "repo-health:{project}:{occurrence}",
            "work_item_policy": "reuse_or_create",
            "failure_attention": True,
        }

    def test_first_class_service_creates_and_publishes_project_scoped_draft(self) -> None:
        definition = AutomationDefinition.model_validate(self._payload())
        draft = self.automations.create_draft(
            "project-editor-flow",
            definition,
            actor_id="operator",
            scope_type=DefinitionScope.PROJECT,
            scope_id="project-a",
            reason="configure project automation",
        )
        published = self.automations.publish_draft(
            draft.record_id,
            actor_id="operator",
            reason="activate project automation",
        )

        resolved, reference = self.automations.resolve(
            "project-editor-flow",
            organization_id="local",
            workspace_id="default",
            project_id="project-a",
        )

        self.assertEqual(published.status.value, "published")
        self.assertEqual(resolved.name, definition.name)
        self.assertEqual(reference.record_id, published.record_id)
        self.assertEqual(reference.revision, published.revision)

    def test_definition_registry_versions_and_resolves_automation(self) -> None:
        draft = self.registry.create_draft(
            DefinitionDraftCreate(
                definition_id="nightly-repository-health",
                kind=AUTOMATION_KIND,
                definition_schema_version=AUTOMATION_SCHEMA_VERSION,
                payload=self._payload(),
                actor="operator",
            )
        )
        published = self.registry.publish(
            draft.record_id,
            DefinitionPublishRequest(actor="operator"),
        )

        automation, reference = self.automations.resolve(
            "nightly-repository-health",
        )

        self.assertEqual(automation.lifecycle, AutomationLifecycle.ENABLED)
        self.assertEqual(
            automation.trigger.type,
            AutomationTriggerType.RECURRING_SCHEDULE,
        )
        self.assertEqual(reference.record_id, published.record_id)
        self.assertEqual(reference.revision, published.revision)
        self.assertEqual(reference.checksum, published.checksum)

    def test_trigger_validation_fails_before_scheduler_state_exists(self) -> None:
        payload = self._payload()
        payload["trigger"] = {
            "type": "recurring_schedule",
            "cron": "0 2 * * *",
        }

        with self.assertRaises(ValidationError):
            self.registry.create_draft(
                DefinitionDraftCreate(
                    definition_id="invalid-schedule",
                    kind=AUTOMATION_KIND,
                    definition_schema_version=AUTOMATION_SCHEMA_VERSION,
                    payload=payload,
                    actor="operator",
                )
            )

        self.assertEqual(self.registry.list_records(kind=AUTOMATION_KIND), [])

    def _publish_payload(self, definition_id: str, payload: dict[str, object]) -> None:
        draft = self.registry.create_draft(
            DefinitionDraftCreate(
                definition_id=definition_id,
                kind=AUTOMATION_KIND,
                definition_schema_version=AUTOMATION_SCHEMA_VERSION,
                payload=payload,
                actor="operator",
            )
        )
        self.registry.publish(
            draft.record_id,
            DefinitionPublishRequest(actor="operator"),
        )

    def test_one_shot_schedule_materialization_is_exact_and_idempotent(self) -> None:
        payload = self._payload()
        payload["trigger"] = {"type": "one_shot_schedule", "due_at": 500.0}
        self._publish_payload("one-shot-maintenance", payload)

        first = self.schedule_materializer.reconcile(
            "one-shot-maintenance",
            actor_id="operator",
            organization_id="local",
            workspace_id="default",
            project_id="project-a",
        )
        second = self.schedule_materializer.reconcile(
            "one-shot-maintenance",
            actor_id="operator",
            organization_id="local",
            workspace_id="default",
            project_id="project-a",
        )

        self.assertIsNotNone(first)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.due_at, 500.0)
        self.assertEqual(first.payload["automation_id"], "one-shot-maintenance")
        self.assertEqual(first.payload["definition_revision"], 1)
        self.assertTrue(first.payload["definition_record_id"])
        self.assertEqual(len(self.scheduler.list()), 1)

    def test_daily_cron_materializes_without_second_timer_engine(self) -> None:
        payload = self._payload()
        payload["trigger"] = {
            "type": "recurring_schedule",
            "cron": "30 2 * * *",
            "timezone": "Europe/Stockholm",
        }
        self._publish_payload("daily-health", payload)

        schedule = self.schedule_materializer.reconcile(
            "daily-health",
            actor_id="operator",
            organization_id="local",
            workspace_id="default",
        )

        self.assertIsNotNone(schedule)
        self.assertEqual(schedule.recurrence.kind.value, "daily")
        self.assertEqual(schedule.recurrence.local_time, "02:30")
        self.assertEqual(schedule.recurrence.timezone, "Europe/Stockholm")
        self.assertEqual(schedule.trigger_type, "automation.definition")

    def test_paused_revision_pauses_prior_materialized_schedule(self) -> None:
        payload = self._payload()
        payload["trigger"] = {"type": "one_shot_schedule", "due_at": 500.0}
        self._publish_payload("pause-me", payload)
        schedule = self.schedule_materializer.reconcile(
            "pause-me",
            actor_id="operator",
            organization_id="local",
            workspace_id="default",
        )

        paused_payload = self._payload()
        paused_payload["lifecycle"] = "paused"
        paused_payload["trigger"] = {"type": "one_shot_schedule", "due_at": 700.0}
        self._publish_payload("pause-me", paused_payload)

        result = self.schedule_materializer.reconcile(
            "pause-me",
            actor_id="operator",
            organization_id="local",
            workspace_id="default",
        )

        self.assertIsNone(result)
        self.assertEqual(self.scheduler.get(schedule.id).status.value, "paused")
        self.assertEqual(len(self.scheduler.list()), 1)

    def test_unsupported_cron_fails_closed_before_schedule_creation(self) -> None:
        payload = self._payload()
        payload["trigger"] = {
            "type": "recurring_schedule",
            "cron": "0 2 * * 1",
            "timezone": "Europe/Stockholm",
        }
        self._publish_payload("weekly-unsupported", payload)

        with self.assertRaises(AutomationScheduleMaterializationError):
            self.schedule_materializer.reconcile(
                "weekly-unsupported",
                actor_id="operator",
                organization_id="local",
                workspace_id="default",
            )
        self.assertEqual(self.scheduler.list(), [])

    async def test_canonical_event_trigger_filters_and_dedupes_dispatch(self) -> None:
        payload = self._payload()
        payload["trigger"] = {
            "type": "canonical_event",
            "event_type": CanonicalEventType.CI_PIPELINE.value,
            "event_filter": {"project_id": "project-a"},
        }
        self._publish_payload("ci-remediation", payload)

        matches = []
        triggers = AutomationEventTriggerService(self.registry, self.event_bus)
        unsubscribe = triggers.install(matches.append)
        try:
            first = await self.events.ingest(
                event_type=CanonicalEventType.CI_PIPELINE,
                source="ci:test",
                idempotency_key="pipeline-1",
                payload={"project_id": "project-a", "status": "failed"},
                tenant_id="local",
                workspace_id="default",
            )
            duplicate = await self.events.ingest(
                event_type=CanonicalEventType.CI_PIPELINE,
                source="ci:test",
                idempotency_key="pipeline-1",
                payload={"project_id": "project-a", "status": "failed"},
                tenant_id="local",
                workspace_id="default",
            )
            await self.events.ingest(
                event_type=CanonicalEventType.CI_PIPELINE,
                source="ci:test",
                idempotency_key="pipeline-2",
                payload={"project_id": "project-b", "status": "failed"},
                tenant_id="local",
                workspace_id="default",
            )
        finally:
            unsubscribe()

        self.assertTrue(first.inserted)
        self.assertFalse(duplicate.inserted)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].automation_id, "ci-remediation")
        self.assertEqual(matches[0].event_id, first.event.event_id)
        self.assertEqual(matches[0].definition_ref.revision, 1)
        self.assertEqual(
            matches[0].dedupe_key,
            f"{matches[0].definition_ref.record_id}:{first.event.event_id}",
        )

    async def test_provider_event_trigger_uses_normalized_provider_identity(self) -> None:
        payload = self._payload()
        payload["trigger"] = {
            "type": "provider_event",
            "event_type": "issue.updated",
            "provider_id": "gitlab:primary",
            "event_filter": {"project_id": "project-a"},
        }
        self._publish_payload("gitlab-issue-routing", payload)

        matches = []
        triggers = AutomationEventTriggerService(self.registry, self.event_bus)
        unsubscribe = triggers.install(matches.append)
        try:
            await self.events.ingest(
                event_type=CanonicalEventType.TASK_SOURCE,
                source="task-source:gitlab",
                idempotency_key="issue-42-v2",
                payload={
                    "project_id": "project-a",
                    "provider_event_type": "issue.updated",
                    "identity": {
                        "source_type": "gitlab",
                        "source_instance": "primary",
                    },
                },
                tenant_id="local",
                workspace_id="default",
            )
        finally:
            unsubscribe()

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].automation_id, "gitlab-issue-routing")

    def test_list_effective_respects_project_scope_and_exact_revision(self) -> None:
        global_payload = self._payload()
        global_payload["trigger"] = {"type": "manual"}
        self._publish_payload("global-automation", global_payload)

        project_payload = self._payload()
        project_payload["name"] = "Project A only"
        project_payload["trigger"] = {"type": "manual"}
        draft = self.registry.create_draft(
            DefinitionDraftCreate(
                definition_id="project-automation",
                kind=AUTOMATION_KIND,
                definition_schema_version=AUTOMATION_SCHEMA_VERSION,
                scope_type=DefinitionScope.PROJECT,
                scope_id="project-a",
                payload=project_payload,
                actor="operator",
            )
        )
        published = self.registry.publish(
            draft.record_id,
            DefinitionPublishRequest(actor="operator"),
        )

        visible = self.automations.list_effective(
            organization_id="local",
            workspace_id="default",
            project_id="project-a",
        )
        hidden = self.automations.list_effective(
            organization_id="local",
            workspace_id="default",
            project_id="project-b",
        )

        self.assertEqual(
            [automation_id for automation_id, _definition, _ref in visible],
            ["global-automation", "project-automation"],
        )
        project_item = next(
            item for item in visible if item[0] == "project-automation"
        )
        self.assertEqual(project_item[2].record_id, published.record_id)
        self.assertEqual(
            [automation_id for automation_id, _definition, _ref in hidden],
            ["global-automation"],
        )

    def test_manual_paused_automation_is_valid_without_scheduler_configuration(self) -> None:
        payload = self._payload()
        payload["lifecycle"] = "paused"
        payload["trigger"] = {"type": "manual"}

        draft = self.registry.create_draft(
            DefinitionDraftCreate(
                definition_id="manual-paused",
                kind=AUTOMATION_KIND,
                definition_schema_version=AUTOMATION_SCHEMA_VERSION,
                payload=payload,
                actor="operator",
            )
        )

        self.assertEqual(draft.payload["lifecycle"], "paused")
        self.assertEqual(draft.payload["trigger"]["type"], "manual")


if __name__ == "__main__":
    unittest.main()
