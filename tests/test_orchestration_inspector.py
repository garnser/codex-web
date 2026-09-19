from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.autonomy import AutonomyObservation
from codex_web.canonical_events import CanonicalEventType
from codex_web.services.autonomy_controller import AutonomyController
from codex_web.services.canonical_events import CanonicalEventBus, CanonicalEventIngestionService
from codex_web.services.orchestration_inspector import OrchestrationInspectorService
from codex_web.storage.autonomy import AutonomyStateStore
from codex_web.storage.canonical_events import CanonicalEventStore
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
        self.service = OrchestrationInspectorService(
            self.event_store,
            self.controller,
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

    async def test_pending_foundations_are_explicit_not_shadow_state(self) -> None:
        snapshot = self.service.snapshot()

        self.assertFalse(snapshot["dependencies"]["scheduler"]["available"])
        self.assertEqual(snapshot["dependencies"]["scheduler"]["issue"], 158)
        self.assertFalse(snapshot["dependencies"]["evaluation_replay"]["available"])
        self.assertEqual(snapshot["dependencies"]["evaluation_replay"]["issue"], 159)
        self.assertFalse(snapshot["dependencies"]["attention_queue"]["available"])
        self.assertEqual(snapshot["dependencies"]["attention_queue"]["issue"], 160)


if __name__ == "__main__":
    unittest.main()
