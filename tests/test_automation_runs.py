from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.automation_definitions import (
    AUTOMATION_KIND,
    AUTOMATION_SCHEMA_VERSION,
)
from codex_web.automation_runs import (
    AutomationRunStatus,
    AutomationRunTrigger,
    AutomationRunTriggerKind,
)
from codex_web.canonical_events import CanonicalEventType
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.services.automation_definitions import install_automation_definitions
from codex_web.services.automation_runs import (
    AutomationRunService,
    AutomationTriggerAdmissionBridge,
)
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
        dedupe_key_template: str | None = None,
    ):
        payload = {
            "name": automation_id,
            "lifecycle": lifecycle,
            "trigger": {"type": "manual"},
            "target": {"kind": "agent_profile", "id": "agent-james"},
            "instructions": "Perform the bounded Automation task.",
            "budget": {"max_concurrency": max_concurrency},
            "dedupe_key_template": dedupe_key_template,
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
        self.assertFalse(duplicate.launch_allowed)
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

    def test_definition_dedupe_policy_separates_project_scope(self) -> None:
        self._publish(
            "project-dedupe",
            max_concurrency=3,
            dedupe_key_template="{project}:{occurrence}",
        )
        first = self.service.admit(
            "project-dedupe",
            self._manual("manual-a"),
            organization_id="local",
            workspace_id="default",
            project_id="project-a",
            idempotency_key="same-request",
        )
        second = self.service.admit(
            "project-dedupe",
            self._manual("manual-b"),
            organization_id="local",
            workspace_id="default",
            project_id="project-b",
            idempotency_key="same-request",
        )
        duplicate = self.service.admit(
            "project-dedupe",
            self._manual("manual-c"),
            organization_id="local",
            workspace_id="default",
            project_id="project-a",
            idempotency_key="same-request",
        )

        self.assertTrue(first.inserted)
        self.assertTrue(second.inserted)
        self.assertFalse(duplicate.inserted)
        self.assertEqual(first.run.id, duplicate.run.id)
        self.assertNotEqual(first.run.id, second.run.id)
        self.assertIn(":policy:project-a:same-request", first.run.dedupe_key)

    def test_invalid_dedupe_placeholder_is_rejected_at_definition_time(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "unsupported placeholder",
        ):
            self._publish(
                "invalid-dedupe",
                dedupe_key_template="{unknown}:{occurrence}",
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


    def test_work_item_creation_wait_state_is_durable_and_resumable(self) -> None:
        self._publish("work-item-wait", max_concurrency=2)
        admitted = self.service.admit(
            "work-item-wait",
            self._manual("manual-wait"),
            organization_id="local",
            workspace_id="default",
            idempotency_key="wait-1",
        )

        self.clock = 105.0
        waiting = self.service.wait_for_work_item(
            admitted.run.id,
            organization_id="local",
            workspace_id="default",
            action_intent_id="action-intent-1",
        )
        self.assertEqual(
            waiting.status,
            AutomationRunStatus.WAITING_FOR_WORK_ITEM,
        )
        self.assertEqual(
            waiting.work_item_action_intent_id,
            "action-intent-1",
        )
        self.assertIsNone(waiting.work_item_ref)

        restarted = AutomationRunService(
            self.store,
            self.definitions,
            clock=lambda: self.clock,
        )
        persisted = self.store.get(
            waiting.id,
            organization_id="local",
            workspace_id="default",
        )
        self.assertEqual(
            persisted.status,
            AutomationRunStatus.WAITING_FOR_WORK_ITEM,
        )

        self.clock = 110.0
        resumed = restarted.resume_with_work_item(
            waiting.id,
            organization_id="local",
            workspace_id="default",
            work_item_ref="group/app#42",
        )
        self.assertEqual(resumed.status, AutomationRunStatus.ADMITTED)
        self.assertEqual(resumed.work_item_ref, "group/app#42")
        self.assertEqual(
            resumed.work_item_action_intent_id,
            "action-intent-1",
        )

    def test_waiting_for_work_item_consumes_concurrency_budget(self) -> None:
        self._publish("wait-concurrency", max_concurrency=1)
        first = self.service.admit(
            "wait-concurrency",
            self._manual("manual-first"),
            organization_id="local",
            workspace_id="default",
            idempotency_key="first",
        )
        self.service.wait_for_work_item(
            first.run.id,
            organization_id="local",
            workspace_id="default",
            action_intent_id="action-intent-1",
        )

        second = self.service.admit(
            "wait-concurrency",
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


    @staticmethod
    def _retry_event(run, *, schedule_id="schedule-retry-1", scheduled_for=150.0):
        reference = run.definition_ref
        return SimpleNamespace(
            event_type=CanonicalEventType.SCHEDULE.value,
            payload={
                "trigger_type": "automation.retry",
                "schedule_id": schedule_id,
                "scheduled_for": scheduled_for,
                "payload": {
                    "automation_id": run.automation_id,
                    "definition_record_id": reference.record_id,
                    "definition_revision": reference.revision,
                    "definition_checksum": reference.checksum,
                    "project_id": run.project_id,
                    "retry_of_run_id": run.id,
                    "retry_attempt": run.attempt + 1,
                    "work_item_ref": run.work_item_ref,
                },
            },
            tenant_id=run.organization_id,
            workspace_id=run.workspace_id,
            occurred_at=scheduled_for,
            correlation_id="corr-retry",
            causation_id="cause-retry",
        )

    def _failed_run(self, automation_id="retryable"):
        self._publish(automation_id, max_concurrency=2)
        admitted = self.service.admit(
            automation_id,
            self._manual("manual-original"),
            organization_id="local",
            workspace_id="default",
            project_id=None,
            idempotency_key="original",
        )
        running = self.service.mark_running(
            admitted.run.id,
            organization_id="local",
            workspace_id="default",
            work_item_ref="group/app#42",
            execution_ids=("exec-original",),
        )
        return self.service.complete(
            running.id,
            organization_id="local",
            workspace_id="default",
            succeeded=False,
            result_code="automation_execution_process_failure",
            result_reason="transient failure",
        )

    def test_retry_schedule_admits_next_attempt_with_same_work_item(self) -> None:
        failed = self._failed_run()
        bridge = AutomationTriggerAdmissionBridge(
            self.service,
            SimpleNamespace(matches=lambda event: ()),
            SimpleNamespace(),
        )
        event = self._retry_event(failed)

        first = bridge._admit_schedule(event)
        duplicate = bridge._admit_schedule(event)

        self.assertIsNotNone(first)
        self.assertTrue(first.launch_allowed)
        self.assertEqual(first.run.attempt, 2)
        self.assertEqual(first.run.work_item_ref, "group/app#42")
        self.assertEqual(first.run.definition_ref, failed.definition_ref)
        self.assertFalse(duplicate.inserted)
        self.assertFalse(duplicate.launch_allowed)
        self.assertEqual(duplicate.run.id, first.run.id)

    def test_retry_schedule_blocks_when_definition_revision_changed(self) -> None:
        failed = self._failed_run("revision-retry")
        self._publish("revision-retry", max_concurrency=2)
        bridge = AutomationTriggerAdmissionBridge(
            self.service,
            SimpleNamespace(matches=lambda event: ()),
            SimpleNamespace(),
        )

        result = bridge._admit_schedule(self._retry_event(failed))

        self.assertIsNotNone(result)
        self.assertFalse(result.launch_allowed)
        self.assertEqual(result.run.status, AutomationRunStatus.BLOCKED)
        self.assertEqual(
            result.run.block_code,
            "automation_retry_revision_changed",
        )
        self.assertEqual(result.run.attempt, 2)
        self.assertEqual(result.run.work_item_ref, "group/app#42")

    def test_retry_schedule_respects_newly_paused_definition(self) -> None:
        failed = self._failed_run("paused-retry")
        self._publish(
            "paused-retry",
            lifecycle="paused",
            max_concurrency=2,
        )
        bridge = AutomationTriggerAdmissionBridge(
            self.service,
            SimpleNamespace(matches=lambda event: ()),
            SimpleNamespace(),
        )

        result = bridge._admit_schedule(self._retry_event(failed))

        self.assertIsNotNone(result)
        self.assertFalse(result.launch_allowed)
        self.assertEqual(result.run.status, AutomationRunStatus.BLOCKED)
        self.assertEqual(result.run.block_code, "automation_paused")

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
