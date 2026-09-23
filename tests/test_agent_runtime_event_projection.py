from __future__ import annotations

import unittest

from codex_web.agent_runtime import AgentRuntimeEvent
from codex_web.runtime.execution import TurnExecutionService


class AgentRuntimeEventProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = TurnExecutionService(object())
        self.messages = []
        self.service._publish_agent_runtime_message = self.messages.append

    def test_agent_message_completion_projects_existing_delta_and_item_events(self) -> None:
        self.service.record_agent_runtime_event(
            "thread-web",
            AgentRuntimeEvent(
                event_type="item.completed",
                provider_native_session_id="native-1",
                provider_native_turn_id="turn-1",
                payload={
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "Completed through the CLI.",
                    },
                },
            ),
        )

        self.assertEqual(
            [message["method"] for message in self.messages],
            ["item/agentMessage/delta", "item/completed"],
        )
        self.assertEqual(
            self.messages[0]["params"],
            {
                "threadId": "thread-web",
                "delta": "Completed through the CLI.",
            },
        )
        completed = self.messages[1]
        self.assertEqual(completed["params"]["threadId"], "thread-web")
        self.assertEqual(completed["params"]["turnId"], "turn-1")
        self.assertEqual(
            completed["params"]["item"]["type"],
            "agentMessage",
        )

    def test_command_execution_uses_existing_frontend_field_names(self) -> None:
        self.service.record_agent_runtime_event(
            "thread-web",
            AgentRuntimeEvent(
                event_type="item.completed",
                provider_native_session_id="native-1",
                payload={
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "pytest -q",
                        "aggregated_output": "10 passed",
                    },
                },
            ),
        )

        item = self.messages[-1]["params"]["item"]
        self.assertEqual(item["type"], "commandExecution")
        self.assertEqual(item["aggregatedOutput"], "10 passed")

    def test_interrupted_cli_turn_projects_terminal_failure_contract(self) -> None:
        self.service.record_agent_runtime_event(
            "thread-web",
            AgentRuntimeEvent(
                event_type="turn.interrupted",
                provider_native_session_id="native-1",
                provider_native_turn_id="turn-2",
                payload={"type": "turn.interrupted"},
            ),
        )

        message = self.messages[-1]
        self.assertEqual(message["method"], "turn/failed")
        self.assertEqual(message["params"]["threadId"], "thread-web")
        self.assertEqual(message["params"]["turn"]["id"], "turn-2")
        self.assertEqual(
            message["params"]["error"],
            "agent runtime turn failed",
        )


if __name__ == "__main__":
    unittest.main()
