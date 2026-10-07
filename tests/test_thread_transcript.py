from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.agent_runtime import AgentRuntimeEvent
from codex_web.services.thread_transcript import ThreadTranscriptService
from codex_web.storage.sqlite_state import SQLiteStateStore


class ThreadTranscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.now = 100.0
        self.transcript = ThreadTranscriptService(
            self.store,
            clock=lambda: self.now,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_cli_reply_is_durable_across_service_instances(self) -> None:
        self.transcript.record_user("thread-1", "execution-1", "hello")
        self.now = 101.0
        self.transcript.record_event(
            "thread-1",
            AgentRuntimeEvent(
                event_type="turn/started",
                provider_native_session_id="native-1",
                provider_native_turn_id="turn-1",
            ),
        )
        self.transcript.record_event(
            "thread-1",
            AgentRuntimeEvent(
                event_type="item/agentMessage/delta",
                provider_native_session_id="native-1",
                provider_native_turn_id="turn-1",
                payload={"itemId": "message-1", "delta": "complete "},
            ),
        )
        self.transcript.record_event(
            "thread-1",
            AgentRuntimeEvent(
                event_type="item/agentMessage/delta",
                provider_native_session_id="native-1",
                provider_native_turn_id="turn-1",
                payload={"itemId": "message-1", "delta": "reply"},
            ),
        )
        self.now = 102.0
        self.transcript.record_event(
            "thread-1",
            AgentRuntimeEvent(
                event_type="turn/completed",
                provider_native_session_id="native-1",
                provider_native_turn_id="turn-1",
            ),
        )

        restored = ThreadTranscriptService(self.store).read("thread-1")

        self.assertEqual(len(restored["turns"]), 1)
        turn = restored["turns"][0]
        self.assertEqual(turn["status"], "completed")
        self.assertEqual(
            [(item["type"], item.get("text")) for item in turn["items"]],
            [("userMessage", None), ("agentMessage", "complete reply")],
        )
        self.assertEqual(turn["items"][0]["content"][0]["text"], "hello")

    def test_failed_start_does_not_leave_pending_transcript_turn(self) -> None:
        self.transcript.record_user("thread-1", "execution-1", "hello")
        self.now = 101.0
        self.transcript.fail_pending("thread-1", "execution-1")

        turn = self.transcript.read("thread-1")["turns"][0]
        self.assertEqual(turn["status"], "failed")
        self.assertEqual(turn["completedAt"], 101.0)

    def test_lease_expiry_becomes_retryable_terminal_response(self) -> None:
        self.transcript.record_user("thread-1", "execution-1", "hello")
        self.now = 102.0

        self.transcript.reconcile_terminal(
            "thread-1",
            "execution-1",
            "interrupted",
            "assignment_lease_expired",
        )

        turn = self.transcript.read("thread-1")["turns"][0]
        self.assertEqual(turn["status"], "failed")
        self.assertEqual(turn["completedAt"], 102.0)
        self.assertEqual(turn["failure"]["code"], "assignment_lease_expired")
        self.assertTrue(turn["failure"]["retryable"])
        self.assertIn("lease expired", turn["items"][-1]["text"])

        self.transcript.reconcile_terminal(
            "thread-1",
            "execution-1",
            "interrupted",
            "assignment_lease_expired",
        )
        self.assertEqual(
            len(self.transcript.read("thread-1")["turns"][0]["items"]),
            2,
        )

    def test_native_codex_commentary_survives_interrupted_turn(self) -> None:
        self.transcript.record_user("thread-1", "execution-1", "continue")
        self.transcript.record_event(
            "thread-1",
            AgentRuntimeEvent(
                event_type="item/completed",
                provider_native_turn_id="native-turn-1",
                payload={
                    "item": {
                        "id": "message-1",
                        "type": "agentMessage",
                        "text": "I am checking the open work now.",
                    }
                },
            ),
        )
        self.transcript.reconcile_terminal(
            "thread-1",
            "execution-1",
            "interrupted",
            "assignment_lease_expired",
        )

        turn = self.transcript.read("thread-1")["turns"][0]
        self.assertEqual(turn["status"], "failed")
        self.assertEqual(
            [item.get("text") for item in turn["items"] if item["type"] == "agentMessage"],
            [
                "I am checking the open work now.",
                (
                    "This turn was interrupted because its execution lease expired. "
                    "Please retry your message."
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
