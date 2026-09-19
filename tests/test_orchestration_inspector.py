from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.autonomy import AutonomyObservation
from codex_web.canonical_events import CanonicalEventType
from codex_web.services.autonomy_controller import AutonomyController
from codex_web.services.canonical_events import CanonicalEventBus, CanonicalEventIngestionService
from codex_web.scheduler import ScheduleCreate, ScheduleRecord
from codex_web.services.orchestration_inspector import OrchestrationInspectorService
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.agent_runtime_usage import AgentRuntimeUsageStore
from codex_web.storage.agent_sessions import AgentSessionStore
from codex_web.storage.approval_requests import ApprovalRequestStore
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.attention import AttentionStore
from codex_web.storage.autonomy import AutonomyStateStore
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.evaluations import EvaluationStore
from codex_web.storage.model_gateway import ModelGatewayStore
from codex_web.storage.scheduler import SchedulerStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class OrchestrationInspectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.event_store = CanonicalEventStore(sqlite)
        self.ingestion = CanonicalEventIngestionService(
            CanonicalEventBus(self.event_store)
        )
        self.controller = AutonomyController(AutonomyStateStore(sqlite))
        self.scheduler = SchedulerStore(sqlite)
        self.evaluations = EvaluationStore(sqlite)
        self.attention = AttentionStore(sqlite)
        self.approvals = ApprovalRequestStore(sqlite)
        self.action_intents = ActionIntentStore(sqlite)
        self.agent_sessions = AgentSessionStore(sqlite)
        self.runtime_usage = AgentRuntimeUsageStore(sqlite)
        self.model_gateway = ModelGatewayStore(sqlite)
        self.artifact_evidence = ArtifactEvidenceStore(sqlite)
        self.service = OrchestrationInspectorService(
            self.event_store,
            self.controller,
            scheduler=self.scheduler,
            evaluations=self.evaluations,
            attention=self.attention,
            approvals=self.approvals,
            action_intents=self.action_intents,
            agent_sessions=self.agent_sessions,
            runtime_usage=self.runtime_usage,
            model_gateway=self.model_gateway,
            artifact_evidence=self.artifact_evidence,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_snapshot_correlates_event_with_deterministic_cycle(self) -> None:
        delivery = await self.ingestion.ingest(
            event_type=CanonicalEventType.WORK_TRANSITION,
            source="fixture:work-item",
            idempotency_key="work-1",
            payload={"ref": "TASK-1", "stage": "closed"},
            occurred_at=100.0,
        )
        cycle = await self.controller.process(
            delivery.event,
            AutonomyObservation(
                deterministic_resolved=True,
                reasoning_score=0.0,
                reason="closed transition resolved deterministically",
            ),
            cycle_key="work-item:TASK-1",
        )

        snapshot = self.service.snapshot()

        self.assertEqual(snapshot["event_count"], 1)
        item = snapshot["timeline"][0]
        self.assertEqual(item["event"]["event_id"], delivery.event.event_id)
        self.assertEqual(item["filtering_result"], "deterministic")
        self.assertFalse(item["reasoning"]["invoked"])
        self.assertIn(
            "closed transition resolved deterministically",
            item["reasoning"]["reasons"],
        )
        self.assertEqual(item["cycles"][0]["id"], cycle.id)
        self.assertEqual(item["resulting_action_intent_ids"], [])
        self.assertEqual(
            [stage["stage"] for stage in item["pipeline"]],
            [
                "event",
                "deterministic_filter",
                "reasoning_gate",
                "routing",
                "authority_approval",
                "execution",
                "verification",
                "human_attention",
            ],
        )

    async def test_snapshot_explains_reasoning_and_supports_event_filters(self) -> None:
        first = await self.ingestion.ingest(
            event_type=CanonicalEventType.CI_PIPELINE,
            source="gitlab:https://gitlab.example",
            idempotency_key="pipeline-1",
            payload={"status": "failed"},
            occurred_at=101.0,
        )
        await self.controller.process(
            first.event,
            AutonomyObservation(
                deterministic_resolved=False,
                reasoning_score=1.0,
                reason="pipeline failure requires diagnosis",
            ),
            cycle_key="pipeline:project-a",
            reasoner=lambda *_args: self._reasoned(),
        )
        await self.ingestion.ingest(
            event_type=CanonicalEventType.DEPLOYMENT,
            source="fixture:deployment",
            idempotency_key="deployment-1",
            payload={"status": "success"},
            occurred_at=102.0,
        )

        snapshot = self.service.snapshot(
            event_type=CanonicalEventType.CI_PIPELINE.value,
            source="gitlab",
        )

        self.assertEqual(snapshot["event_count"], 1)
        item = snapshot["timeline"][0]
        self.assertTrue(item["reasoning"]["invoked"])
        self.assertEqual(item["filtering_result"], "completed")
        self.assertIn("reasoned diagnosis", item["reasoning"]["reasons"])

    @staticmethod
    async def _reasoned():
        from codex_web.autonomy import AutonomyReasoningResult

        return AutonomyReasoningResult(summary="reasoned diagnosis")

    async def test_canonical_foundations_are_projected_not_shadow_state(self) -> None:
        snapshot = self.service.snapshot()

        self.assertTrue(snapshot["dependencies"]["scheduler"]["available"])
        self.assertEqual(snapshot["dependencies"]["scheduler"]["issue"], 158)
        self.assertTrue(snapshot["dependencies"]["evaluation_replay"]["available"])
        self.assertEqual(snapshot["dependencies"]["evaluation_replay"]["issue"], 159)
        self.assertTrue(snapshot["dependencies"]["attention_queue"]["available"])
        self.assertEqual(snapshot["dependencies"]["attention_queue"]["issue"], 160)
        self.assertEqual(snapshot["evaluations"], {
            "runs": [],
            "comparisons": [],
            "suite_runs": [],
        })
        self.assertEqual(snapshot["attention_items"], [])
        self.assertEqual(snapshot["approval_requests"], [])

    async def test_schedule_projection_links_canonical_firing_event_and_tenant_scope(self) -> None:
        local = ScheduleRecord.from_create(
            ScheduleCreate(
                name="Local review",
                tenant_id="org-a",
                workspace_id="workspace-a",
                trigger_type="review.due",
                due_at=100.0,
                payload={"goal_id": "goal-a"},
            ),
            actor_id="admin-a",
            now=50.0,
        )
        self.scheduler.create(local)
        other = ScheduleRecord.from_create(
            ScheduleCreate(
                name="Other tenant",
                tenant_id="org-b",
                workspace_id="workspace-b",
                trigger_type="review.due",
                due_at=100.0,
            ),
            actor_id="admin-b",
            now=50.0,
        )
        self.scheduler.create(other)

        delivery = await self.ingestion.ingest(
            event_type=CanonicalEventType.SCHEDULE,
            source=f"scheduler:{local.id}",
            idempotency_key=f"{local.id}:100",
            payload={
                "schedule_id": local.id,
                "trigger_type": "review.due",
                "scheduled_for": 100.0,
            },
            occurred_at=101.0,
            tenant_id="org-a",
            workspace_id="workspace-a",
        )

        snapshot = self.service.snapshot(
            organization_id="org-a",
            workspace_id="workspace-a",
        )

        self.assertEqual([item["id"] for item in snapshot["schedules"]], [local.id])
        firing = snapshot["schedules"][0]["recent_firings"][0]
        self.assertEqual(firing["event_id"], delivery.event.event_id)
        self.assertEqual(firing["scheduled_for"], 100.0)


if __name__ == "__main__":
    unittest.main()
