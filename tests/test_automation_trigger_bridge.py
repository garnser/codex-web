from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.automation_definitions import (
    AUTOMATION_KIND,
    AUTOMATION_SCHEMA_VERSION,
)
from codex_web.automation_runs import AutomationRunTriggerKind
from codex_web.canonical_events import CanonicalEventType
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.services.automation_definitions import (
    AutomationEventTriggerService,
    AutomationScheduleMaterializer,
    install_automation_definitions,
)
from codex_web.services.automation_runs import (
    AutomationRunService,
    AutomationTriggerAdmissionBridge,
)
from codex_web.services.canonical_events import (
    CanonicalEventBus,
    CanonicalEventIngestionService,
)
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.scheduler import SchedulerService
from codex_web.storage.automation_runs import AutomationRunStore
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.scheduler import SchedulerStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class AutomationTriggerAdmissionBridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.registry = DefinitionRegistryService(DefinitionRegistryStore(sqlite))
        self.definitions = install_automation_definitions(self.registry)
        self.bus = CanonicalEventBus(CanonicalEventStore(sqlite))
        self.events = CanonicalEventIngestionService(self.bus)
        self.run_store = AutomationRunStore(sqlite)
        self.runs = AutomationRunService(
            self.run_store,
            self.definitions,
            clock=lambda: 100.0,
        )
        self.event_triggers = AutomationEventTriggerService(
            self.registry,
            self.bus,
        )
        self.bridge = AutomationTriggerAdmissionBridge(
            self.runs,
            self.event_triggers,
            self.bus,
        )
        self.scheduler = SchedulerService(
            SchedulerStore(sqlite),
            self.events,
            clock=lambda: 500.0,
        )
        self.materializer = AutomationScheduleMaterializer(
            self.definitions,
            self.scheduler,
            clock=lambda: 100.0,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _publish(
        self,
        automation_id: str,
        *,
        trigger: dict[str, object],
        lifecycle: str = "enabled",
    ):
        draft = self.registry.create_draft(
            DefinitionDraftCreate(
                definition_id=automation_id,
                kind=AUTOMATION_KIND,
                definition_schema_version=AUTOMATION_SCHEMA_VERSION,
                payload={
                    "name": automation_id,
                    "lifecycle": lifecycle,
                    "trigger": trigger,
                    "target": {"kind": "agent_profile", "id": "agent-james"},
                    "instructions": "Perform the bounded Automation task.",
                },
                actor="operator",
            )
        )
        return self.registry.publish(
            draft.record_id,
            DefinitionPublishRequest(actor="operator"),
        )

    async def test_event_trigger_is_admitted_once_with_exact_matched_revision(self) -> None:
        published = self._publish(
            "ci-failure",
            trigger={
                "type": "canonical_event",
                "event_type": CanonicalEventType.CI_PIPELINE.value,
                "event_filter": {"project_id": "project-a"},
            },
        )
        unsubscribe = self.bridge.install()
        try:
            first = await self.events.ingest(
                event_type=CanonicalEventType.CI_PIPELINE,
                source="ci:test",
                idempotency_key="pipeline-42",
                payload={"project_id": "project-a", "status": "failed"},
                tenant_id="local",
                workspace_id="default",
            )
            duplicate = await self.events.ingest(
                event_type=CanonicalEventType.CI_PIPELINE,
                source="ci:test",
                idempotency_key="pipeline-42",
                payload={"project_id": "project-a", "status": "failed"},
                tenant_id="local",
                workspace_id="default",
            )
        finally:
            unsubscribe()

        self.assertTrue(first.inserted)
        self.assertFalse(duplicate.inserted)
        history = self.run_store.list(
            organization_id="local",
            workspace_id="default",
            automation_id="ci-failure",
        )
        self.assertEqual(len(history), 1)
        self.assertEqual(
            history[0].trigger.kind,
            AutomationRunTriggerKind.CANONICAL_EVENT,
        )
        self.assertEqual(history[0].trigger.event_id, first.event.event_id)
        self.assertEqual(history[0].definition_ref.record_id, published.record_id)

    async def test_schedule_due_event_uses_materialized_definition_revision(self) -> None:
        published = self._publish(
            "one-shot",
            trigger={"type": "one_shot_schedule", "due_at": 500.0},
        )
        schedule = self.materializer.reconcile(
            "one-shot",
            actor_id="operator",
            organization_id="local",
            workspace_id="default",
            project_id="project-a",
        )
        self.assertIsNotNone(schedule)

        unsubscribe = self.bridge.install()
        try:
            result = await self.scheduler.run_due()
        finally:
            unsubscribe()

        self.assertEqual(result.emitted, 1)
        history = self.run_store.list(
            organization_id="local",
            workspace_id="default",
            automation_id="one-shot",
        )
        self.assertEqual(len(history), 1)
        run = history[0]
        self.assertEqual(run.trigger.kind, AutomationRunTriggerKind.SCHEDULE)
        self.assertEqual(run.trigger.schedule_id, schedule.id)
        self.assertEqual(run.trigger.scheduled_for, 500.0)
        self.assertEqual(run.project_id, "project-a")
        self.assertEqual(run.definition_ref.record_id, published.record_id)

    async def test_manual_event_and_schedule_paths_share_restart_safe_ledger(self) -> None:
        self._publish("manual", trigger={"type": "manual"})
        first = self.bridge.manual(
            "manual",
            organization_id="local",
            workspace_id="default",
            project_id="project-a",
            actor_id="operator",
            idempotency_key="manual-request-1",
        )
        restarted = AutomationTriggerAdmissionBridge(
            AutomationRunService(
                self.run_store,
                self.definitions,
                clock=lambda: 100.0,
            ),
            self.event_triggers,
            self.bus,
        )
        duplicate = restarted.manual(
            "manual",
            organization_id="local",
            workspace_id="default",
            project_id="project-a",
            actor_id="operator",
            idempotency_key="manual-request-1",
        )

        self.assertTrue(first.inserted)
        self.assertFalse(duplicate.inserted)
        self.assertEqual(first.run.id, duplicate.run.id)
        self.assertEqual(first.run.trigger.kind, AutomationRunTriggerKind.MANUAL)

    async def test_trigger_keeps_old_exact_revision_if_new_revision_publishes_after_match(self) -> None:
        first = self._publish(
            "revision-race",
            trigger={
                "type": "canonical_event",
                "event_type": CanonicalEventType.CI_PIPELINE.value,
            },
        )
        delivery = await self.events.ingest(
            event_type=CanonicalEventType.CI_PIPELINE,
            source="ci:race",
            idempotency_key="race-event",
            payload={"project_id": "project-a"},
            tenant_id="local",
            workspace_id="default",
        )
        matches = self.event_triggers.matches(delivery.event)
        self.assertEqual(len(matches), 1)

        second = self._publish(
            "revision-race",
            trigger={
                "type": "canonical_event",
                "event_type": CanonicalEventType.CI_PIPELINE.value,
            },
        )
        self.assertNotEqual(first.record_id, second.record_id)

        admitted = self.bridge._admit_event_match(delivery.event, matches[0])
        self.assertEqual(admitted.run.definition_ref.record_id, first.record_id)


if __name__ == "__main__":
    unittest.main()
