from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from codex_web.agent_providers import AgentProviderCapability
from codex_web.agent_runtime import (
    AgentRuntimeSessionRequest,
    AgentRuntimeTurnRequest,
)
from codex_web.services.claude_agent_runtime import ClaudeAgentRuntimeAdapter


class _Hub:
    def __init__(self) -> None:
        self.listeners = set()

    def subscribe(self, listener):
        self.listeners.add(listener)

    def unsubscribe(self, listener):
        self.listeners.discard(listener)

    def emit(self, event):
        for listener in list(self.listeners):
            listener(event)


class ClaudeAgentRuntimeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_canonical_session_and_turn_requests_hide_provider_transport(self) -> None:
        transport = SimpleNamespace(
            request=AsyncMock(
                side_effect=[
                    {"type": "system", "subtype": "init", "session_id": "claude-session-1"},
                    {
                        "type": "result",
                        "subtype": "success",
                        "session_id": "claude-session-1",
                        "turn_id": "turn-1",
                    },
                    {"type": "control_response", "session_id": "claude-session-1"},
                ]
            )
        )
        adapter = ClaudeAgentRuntimeAdapter(transport)

        created = await adapter.create_session(
            AgentRuntimeSessionRequest(
                project_id="project-a",
                execution_id="execution-a",
                assignment_id="assignment-a",
                execution_workspace_id="workspace-a",
                worker_id="worker-a",
                workspace_cwd="/workspace/project-a",
                sandbox="workspace-write",
                approval_policy="on-request",
                model="claude-test",
            )
        )
        turn = await adapter.start_turn(
            "claude-session-1",
            AgentRuntimeTurnRequest(
                message="Implement the change",
                model="claude-test",
                reasoning_effort="high",
            ),
        )
        interrupted = await adapter.interrupt("claude-session-1")

        self.assertEqual(created.provider_native_session_id, "claude-session-1")
        self.assertEqual(turn.provider_native_turn_id, "turn-1")
        self.assertEqual(
            transport.request.await_args_list[0].args[0],
            "session/create",
        )
        create_params = transport.request.await_args_list[0].args[1]
        self.assertEqual(create_params["assignment_id"], "assignment-a")
        self.assertEqual(create_params["cwd"], "/workspace/project-a")
        self.assertEqual(create_params["approval_policy"], "on-request")
        self.assertEqual(
            transport.request.await_args_list[1].args,
            (
                "turn/start",
                {
                    "session_id": "claude-session-1",
                    "message": "Implement the change",
                    "model": "claude-test",
                    "reasoning_effort": "high",
                    "cwd": None,
                    "approval_policy": None,
                    "approval_reviewer": None,
                    "sandbox_policy": None,
                    "developer_instructions": None,
                },
            ),
        )
        self.assertEqual(interrupted.provider_native_session_id, "claude-session-1")

    async def test_resume_keeps_canonical_mapping_when_transport_omits_session_id(self) -> None:
        transport = SimpleNamespace(request=AsyncMock(return_value={"type": "system"}))
        adapter = ClaudeAgentRuntimeAdapter(transport)

        result = await adapter.resume_session(
            "claude-session-1",
            AgentRuntimeSessionRequest(project_id="project-a"),
        )

        self.assertEqual(result.provider_native_session_id, "claude-session-1")
        self.assertEqual(transport.request.await_args.args[0], "session/resume")
        self.assertEqual(
            transport.request.await_args.args[1]["session_id"],
            "claude-session-1",
        )

    async def test_stream_messages_are_normalized_without_becoming_canonical_identity(self) -> None:
        hub = _Hub()
        transport = SimpleNamespace(host=SimpleNamespace(hub=hub))
        adapter = ClaudeAgentRuntimeAdapter(transport)
        events = []
        unsubscribe = adapter.subscribe_events(events.append)

        hub.emit(
            {
                "type": "claude.event",
                "message": {
                    "type": "assistant",
                    "session_id": "native-1",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu-1",
                                "name": "Bash",
                                "input": {"command": "git status"},
                            }
                        ]
                    },
                },
            }
        )
        hub.emit(
            {
                "type": "claude.event",
                "message": {
                    "type": "result",
                    "subtype": "success",
                    "session_id": "native-1",
                    "turn_id": "turn-1",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        )
        unsubscribe()

        self.assertEqual(events[0].event_type, "tool/requested")
        self.assertEqual(events[0].provider_native_session_id, "native-1")
        self.assertEqual(events[1].event_type, "result/success")
        self.assertEqual(events[1].provider_native_turn_id, "turn-1")
        self.assertEqual(events[1].payload["usage"]["input_tokens"], 10)
        self.assertEqual(hub.listeners, set())

    async def test_approval_response_uses_transport_only_after_canonical_decision(self) -> None:
        transport = SimpleNamespace(respond_to_server_request=AsyncMock())
        adapter = ClaudeAgentRuntimeAdapter(transport)

        await adapter.respond_approval(
            "assignment-a:req-1",
            {"behavior": "deny", "message": "Approval rejected"},
        )

        transport.respond_to_server_request.assert_awaited_once_with(
            "assignment-a:req-1",
            {"behavior": "deny", "message": "Approval rejected"},
        )

    async def test_capabilities_do_not_claim_codex_native_compaction(self) -> None:
        adapter = ClaudeAgentRuntimeAdapter(SimpleNamespace())
        self.assertIn(AgentProviderCapability.AGENT_EXECUTION, adapter.capabilities)
        self.assertIn(AgentProviderCapability.PERSISTENT_SESSIONS, adapter.capabilities)
        self.assertIn(AgentProviderCapability.INTERACTIVE_APPROVALS, adapter.capabilities)
        self.assertNotIn(
            AgentProviderCapability.NATIVE_CONTEXT_COMPACTION,
            adapter.capabilities,
        )

        with self.assertRaisesRegex(RuntimeError, "native context compaction"):
            await adapter.compact_session("claude-session-1")


if __name__ == "__main__":
    unittest.main()
