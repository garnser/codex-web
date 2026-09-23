from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.automation_definitions import (
    AUTOMATION_KIND,
    AUTOMATION_SCHEMA_VERSION,
)
from codex_web.automation_runs import (
    AutomationRunStatus,
    AutomationRunTrigger,
    AutomationRunTriggerKind,
)
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.services.automation_definitions import install_automation_definitions
from codex_web.services.automation_runs import AutomationRunService
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.storage.automation_runs import AutomationRunStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class AutomationRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.registry = DefinitionRegistryService(DefinitionRegistryStore(sqlite))
        self.definitions = install_automation_definitions(self.registry)
        self.store = AutomationRunStore(sqlite)
        self.clock = 100.0
        self.service = AutomationRunService(
            self.store,
            self.definitions,
            clock=lambda: self.clock,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _publish(
        self,
        automation_id: str,
        *,
        lifecycle: str = "enabled",
        max_concurrency: int = 1,
    ):
        payload = {
            "name": automation_id,
            "lifecycle": lifecycle,
            "trigger": {"type": "manual"},
            "target": {"kind": "agent_profile", "id": "agent-james"},
            "instructions": "Perform the bounded Automation task.",
            "budget": {"max_concurrency": max_concurrency},
        }
        draft = self.registry.create_draft(
            DefinitionDraftCreate(
                definition_id=automation_id,
                kind=AUTOMATION_KIND,
                definition_schema_version=AUTOMATION_SCHEMA_VERSION,
                payload=payload,
                actor="operator",
            )
        )
        return self.registry.publish(
            draft.record_id,
            DefinitionPublishRequest(actor="operator"),
        )

    @staticmethod
    def _manual(source_id: str) -> AutomationRunTrigger:
        return AutomationRunTrigger(
            kind=AutomationRunTriggerKind.MANUAL,
            source_id=source_id,
        )

    def test_manual_schedule_and_event_share_one_history_model(self) -> None:
        self._publish("shared-history", max_concurrency=3)
        manual = self.service.admit(
            "shared-history",
            self._manual("manual-1"),
            organization_id="local",
            workspace_id="default",
            idempotency_key="manual-1",
        )
        schedule = self.service.admit(
            "shared-history",
            AutomationRunTrigger(
                kind=AutomationRunTriggerKind.SCHEDULE,
                source_id="schedule-1:500",
                schedule_id="schedule-1",
                scheduled_for=500.0,
            ),
            organization_id="local",
            workspace_id="default",
        )
        event = self.service.admit(
            "shared-history",
            AutomationRunTrigger(
                kind=AutomationRunTriggerKind.CANONICAL_EVENT,
                source_id="evt-1",
                event_id="evt-1",
            ),
            organization_id="local",
            workspace_id="default",
        )

        self.assertTrue(manual.launch_allowed)
        self.assertTrue(schedule.launch_allowed)
        self.assertTrue(event.launch_allowed)
        history = self.store.list(
            organization_id="local",
            workspace_id="default",
            automation_id="shared-history",
        )
        self.assertEqual(len(history), 3)
        self.assertEqual(
            {run.trigger.kind for run in history},
            {
                AutomationRunTriggerKind.MANUAL,
                AutomationRunTriggerKind.SCHEDULE,
                AutomationRunTriggerKind.CANONICAL_EVENT,
            },
        )

    def test_duplicate_occurrence_is_deduped_across_service_restart(self) -> None:
        self._publish("dedupe")
        first = self.service.admit(
            "dedupe",
            self._manual("manual-a"),
            organization_id="local",
            workspace_id="default",
            idempotency_key="request-1",
        )

        restarted = AutomationRunService(
            self.store,
            self.definitions,
            clock=lambda: self.clock,
        )
        duplicate = restarted.admit(
            "dedupe",
            self._manual("manual-b"),
            organization_id="local",
            workspace_id="default",
            idempotency_key="request-1",
        )

        self.assertTrue(first.inserted)
        self.assertFalse(duplicate.inserted)
        self.assertEqual(first.run.id, duplicate.run.id)
        self.assertEqual(
            len(
                self.store.list(
                    organization_id="local",
                    workspace_id="default",
                )
            ),
            1,
        )

    def test_paused_automation_records_blocked_trigger_without_launch(self) -> None:
        self._publish("paused", lifecycle="paused")
        result = self.service.admit(
            "paused",
            self._manual("manual-paused"),
            organization_id="local",
            workspace_id="default",
            idempotency_key="paused-1",
        )

        self.assertFalse(result.launch_allowed)
        self.assertEqual(result.run.status, AutomationRunStatus.BLOCKED)
        self.assertEqual(result.run.block_code, "automation_paused")
        self.assertIsNotNone(result.run.completed_at)

    def test_concurrency_budget_blocks_trigger_without_starting_new_work(self) -> None:
        self._publish("serial", max_concurrency=1)
        first = self.service.admit(
            "serial",
            self._manual("manual-first"),
            organization_id="local",
            workspace_id="default",
            idempotency_key="first",
        )
        self.assertTrue(first.launch_allowed)

        second = self.service.admit(
            "serial",
            self._manual("manual-second"),
            organization_id="local",
            workspace_id="default",
            idempotency_key="second",
        )
        self.assertFalse(second.launch_allowed)
        self.assertEqual(
            second.run.block_code,
            "automation_concurrency_exhausted",
        )

        self.clock = 110.0
        self.service.complete(
            first.run.id,
            organization_id="local",
            workspace_id="default",
            succeeded=True,
            evidence_ids=("evidence-1",),
        )
        third = self.service.admit(
            "serial",
            self._manual("manual-third"),
            organization_id="local",
            workspace_id="default",
            idempotency_key="third",
        )
        self.assertTrue(third.launch_allowed)

    def test_run_keeps_exact_definition_revision_and_links_canonical_outcomes(self) -> None:
        first_definition = self._publish("revisioned")
        first = self.service.admit(
            "revisioned",
            self._manual("manual-first"),
            organization_id="local",
            workspace_id="default",
            idempotency_key="first",
        )
        self.assertEqual(
            first.run.definition_ref.record_id,
            first_definition.record_id,
        )

        self.clock = 105.0
        running = self.service.mark_running(
            first.run.id,
            organization_id="local",
            workspace_id="default",
            work_item_ref="group/app#42",
            execution_ids=("exec-1", "exec-1"),
        )
        self.assertEqual(running.execution_ids, ("exec-1",))
        self.clock = 110.0
        completed = self.service.complete(
            running.id,
            organization_id="local",
            workspace_id="default",
            succeeded=True,
            evidence_ids=("evidence-1", "evidence-1"),
        )
        self.assertEqual(completed.status, AutomationRunStatus.SUCCEEDED)
        self.assertEqual(completed.work_item_ref, "group/app#42")
        self.assertEqual(completed.evidence_ids, ("evidence-1",))

        second_definition = self._publish("revisioned")
        second = self.service.admit(
            "revisioned",
            self._manual("manual-second"),
            organization_id="local",
            workspace_id="default",
            idempotency_key="second",
        )
        self.assertNotEqual(
            second.run.definition_ref.record_id,
            first.run.definition_ref.record_id,
        )
        self.assertEqual(
            second.run.definition_ref.record_id,
            second_definition.record_id,
        )


if __name__ == "__main__":
    unittest.main()
