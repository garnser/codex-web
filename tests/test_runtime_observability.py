from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

from codex_web.services.runtime import RuntimeService


class RuntimeObservabilityTests(unittest.TestCase):
    def test_operations_reports_queue_work_item_provider_and_activity_metrics(self) -> None:
        now = time.time()
        open_item = SimpleNamespace(
            current_stage="implementation_active",
            closed_at=None,
            current_owner="james",
            next_owner="james",
            handoff=None,
            release_gate=False,
            split_brain=False,
        )
        pending_handoff = SimpleNamespace(status="pending", requested_at=now - 120)
        validation_item = SimpleNamespace(
            current_stage="ready_for_validation",
            closed_at=None,
            current_owner="quinn",
            next_owner="quinn",
            handoff=pending_handoff,
            release_gate=True,
            split_brain=True,
        )
        closed_item = SimpleNamespace(
            current_stage="closed",
            closed_at=now - 50,
            current_owner="james",
            next_owner=None,
            handoff=None,
            release_gate=False,
            split_brain=False,
        )
        host = SimpleNamespace(
            codex=SimpleNamespace(pending_approvals={"1": {"id": 1}}),
            app=SimpleNamespace(state=SimpleNamespace()),
            _load_turn_queues=lambda: {
                "thread-1": [SimpleNamespace(created_at=now - 90)],
                "thread-2": [],
            },
            _load_active_turns=lambda: {"thread-active": object()},
            _load_work_item_states=lambda: {
                "one": open_item,
                "two": validation_item,
                "closed": closed_item,
            },
            _work_item_split_brain_findings=lambda state: ["drift"] if state.split_brain else [],
            _recent_bot_events=lambda _limit: [
                {
                    "created_at": now - 10,
                    "type": "outbound_ready",
                    "delivery": {"sent": False},
                },
                {"created_at": now - 5, "type": "native_recovery_scheduled"},
                {"created_at": now - 3, "type": "executive_delegate_completed"},
                {"created_at": now - 5000, "type": "old_event"},
            ],
            _daemon_health=lambda: {
                "ok": True,
                "problems": [],
                "runtimeConnections": 2,
                "slackBackfillCooldownRemainingSeconds": 12.5,
                "gitlabSyncConsecutiveFailures": 1,
                "gitlabSyncLastError": "temporary",
                "gitlabSyncLastSuccessAt": now - 30,
            },
        )

        snapshot = RuntimeService(host).operations(window_seconds=900)

        self.assertTrue(snapshot["runtime"]["healthy"])
        self.assertEqual(snapshot["runtime"]["activeTurns"], 1)
        self.assertEqual(snapshot["runtime"]["queuedTurns"], 1)
        self.assertGreaterEqual(snapshot["runtime"]["oldestQueueAgeSeconds"], 89)
        self.assertEqual(snapshot["runtime"]["pendingApprovals"], 1)

        self.assertEqual(snapshot["workItems"]["open"], 2)
        self.assertEqual(snapshot["workItems"]["pendingHandoffs"], 1)
        self.assertEqual(snapshot["workItems"]["releaseGates"], 1)
        self.assertEqual(snapshot["workItems"]["splitBrain"], 1)
        self.assertEqual(snapshot["workItems"]["stages"]["implementation_active"], 1)
        self.assertEqual(snapshot["workItems"]["owners"]["quinn"], 1)

        self.assertEqual(snapshot["providers"]["runtimeConnections"], 2)
        self.assertEqual(snapshot["providers"]["recentDeliveryFailures"], 1)
        self.assertEqual(snapshot["activity"]["events"], 3)
        self.assertEqual(snapshot["activity"]["recoveryEvents"], 1)
        self.assertEqual(snapshot["activity"]["executiveEvents"], 1)
        self.assertNotIn("old_event", snapshot["activity"]["eventTypes"])

    def test_operations_clamps_observation_window(self) -> None:
        host = SimpleNamespace(
            codex=SimpleNamespace(pending_approvals={}),
            app=SimpleNamespace(state=SimpleNamespace()),
            _load_turn_queues=lambda: {},
            _load_active_turns=lambda: {},
            _load_work_item_states=lambda: {},
            _work_item_split_brain_findings=lambda _state: [],
            _recent_bot_events=lambda _limit: [],
            _daemon_health=lambda: {"ok": True, "problems": []},
        )
        service = RuntimeService(host)

        self.assertEqual(service.operations(window_seconds=1)["windowSeconds"], 60.0)
        self.assertEqual(service.operations(window_seconds=999999)["windowSeconds"], 86400.0)


if __name__ == "__main__":
    unittest.main()
