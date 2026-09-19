from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.services.autonomy import AutonomyService
from codex_web.services.autonomy_controller import AutonomyController
from codex_web.services.canonical_events import CanonicalEventBus, CanonicalEventIngestionService
from codex_web.storage.autonomy import AutonomyStateStore
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _Host:
    def __init__(self) -> None:
        self.calls = []

    async def _dispatch_event_to_binding(self, binding, text, source):
        self.calls.append((binding, text, source))
        return {"ok": True, "threadId": "thread-1"}


class AutonomyWatchdogBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.controller = AutonomyController(AutonomyStateStore(sqlite))
        self.ingestion = CanonicalEventIngestionService(
            CanonicalEventBus(CanonicalEventStore(sqlite))
        )
        self.host = _Host()
        self.service = AutonomyService(
            self.host,
            controller=self.controller,
            canonical_events=self.ingestion,
        )
        self.binding = SimpleNamespace(thread_id="thread-1")

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_watchdog_reasoning_is_canonical_bounded_and_idempotent(self) -> None:
        first = await self.service._bounded_reasoning_dispatch(
            self.binding,
            "reason about unresolved work",
            "work-item-sla",
            cycle_key="work-item-sla:TASK-1",
            payload={"ref": "TASK-1", "stage": "implementation_active"},
        )
        second = await self.service._bounded_reasoning_dispatch(
            self.binding,
            "reason about unresolved work",
            "work-item-sla",
            cycle_key="work-item-sla:TASK-1",
            payload={"ref": "TASK-1", "stage": "implementation_active"},
        )

        self.assertTrue(first["ok"])
        self.assertIn("autonomyCycleId", first)
        self.assertIn("canonicalEventId", first)
        self.assertEqual(second["reason"], "canonical_duplicate")
        self.assertEqual(len(self.host.calls), 1)
        self.assertEqual(len(self.controller.status()["recent_cycles"]), 1)

    async def test_pause_stops_watchdog_before_agent_reasoning(self) -> None:
        self.controller.pause(actor_id="operator")

        result = await self.service._bounded_reasoning_dispatch(
            self.binding,
            "reason about unresolved work",
            "orchestrator-watchdog",
            cycle_key="orchestrator:project-a",
            payload={"project_id": "project-a", "item_refs": ["TASK-1"]},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "autonomy_skipped")
        self.assertEqual(result["autonomyReason"], "autonomy_paused")
        self.assertEqual(self.host.calls, [])


if __name__ == "__main__":
    unittest.main()
