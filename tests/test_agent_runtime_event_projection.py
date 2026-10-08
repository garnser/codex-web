from __future__ import annotations

import unittest
from types import SimpleNamespace
import asyncio
from unittest.mock import AsyncMock, Mock

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

    def test_history_projection_failure_does_not_block_live_event_handling(self) -> None:
        events = []

        class FailingHistory:
            @staticmethod
            def project_message(_thread_id, _message):
                raise RuntimeError("storage unavailable")

        service = TurnExecutionService(
            SimpleNamespace(_append_bot_event=events.append, _load_active_turns=lambda: {}),
            thread_history=FailingHistory(),
        )

        service._publish_agent_runtime_message(
            {
                "method": "item/agentMessage/delta",
                "params": {"threadId": "thread-web", "delta": "visible"},
            }
        )

        self.assertEqual(events[0]["type"], "thread_history_projection_failed")
        self.assertEqual(events[0]["thread_id"], "thread-web")
        self.assertEqual(events[0]["error_type"], "RuntimeError")


class AgentRuntimeDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def service(self, recovery=False):
        self.host = SimpleNamespace(
            hub=SimpleNamespace(publish=AsyncMock()),
            _record_bot_outbound=AsyncMock(),
            _record_terminal_turn_result=Mock(return_value=recovery),
            _schedule_queue_drain=Mock(),
            _append_bot_event=Mock(),
        )
        service = TurnExecutionService(self.host)
        service.record_thread_activity = Mock()
        return service

    async def settle(self):
        for task in list(asyncio.all_tasks()):
            if task.get_name().startswith("agent-runtime-notification-"):
                await task

    async def test_adapter_completion_reaches_existing_slack_relay_once(self):
        service = self.service()
        service.record_agent_runtime_event("orchestrator", AgentRuntimeEvent(
            event_type="item.completed", provider_native_session_id="session",
            payload={"item": {"type": "agent_message", "text": "Owner progress"}},
        ))
        await self.settle()
        self.host._record_bot_outbound.assert_awaited_once()
        message = self.host._record_bot_outbound.await_args.args[0]
        self.assertEqual(message["params"]["item"]["type"], "agentMessage")
        self.assertEqual(message["params"]["threadId"], "orchestrator")
        self.assertEqual(self.host.hub.publish.await_count, 2)

    async def test_terminal_adapter_event_advances_existing_owner_queue(self):
        service = self.service()
        service.record_agent_runtime_event("owner", AgentRuntimeEvent(
            event_type="turn.completed", provider_native_session_id="session",
            provider_native_turn_id="turn", payload={},
        ))
        await self.settle()
        self.host._record_terminal_turn_result.assert_called_once()
        self.host._schedule_queue_drain.assert_called_once_with("owner")
        self.host._record_bot_outbound.assert_not_awaited()

    async def test_terminal_recovery_retains_control_of_queue(self):
        service = self.service(recovery=True)
        service.record_agent_runtime_event("owner", AgentRuntimeEvent(
            event_type="turn.failed", provider_native_session_id="session", payload={},
        ))
        await self.settle()
        self.host._schedule_queue_drain.assert_not_called()


if __name__ == "__main__":
    unittest.main()
