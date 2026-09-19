from __future__ import annotations

import unittest
from types import SimpleNamespace

from codex_web.agent_providers import AgentProviderCapability
from codex_web.agent_runtime import (
    AgentRuntimeEvent,
    AgentRuntimeHealth,
    AgentRuntimeListRequest,
    AgentRuntimeSessionRequest,
    AgentRuntimeTurnRequest,
    AgentRuntimeUnsupportedCapability,
)
from codex_web.services.claude_agent_runtime import ClaudeAgentRuntimeAdapter


class _Hub:
    def __init__(self) -> None:
        self.listeners = []

    def subscribe(self, listener) -> None:
        self.listeners.append(listener)

    def unsubscribe(self, listener) -> None:
        self.listeners.remove(listener)

    def publish(self, event) -> None:
        for listener in list(self.listeners):
            listener(event)


class _Transport:
    def __init__(self) -> None:
        self.calls = []
        self.responses = {}
        self.host = SimpleNamespace(hub=_Hub())
        self.proc = None
        self.ready = True
        self.last_error = None
        self.approvals = []
        self.ensure_started_calls = 0
        self.stop_calls = 0

    def status(self):
        return SimpleNamespace(ready=self.ready, last_error=self.last_error)

    async def request(self, method, params):
        self.calls.append((method, params))
        return self.responses.get(method, {})

    async def respond_to_server_request(self, request_id, result):
        self.approvals.append((request_id, result))

    async def ensure_started(self):
        self.ensure_started_calls += 1

    async def stop(self):
        self.stop_calls += 1


class ClaudeAgentRuntimeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.transport = _Transport()
        self.adapter = ClaudeAgentRuntimeAdapter(self.transport)

    async def test_capabilities_exclude_native_compaction(self) -> None:
        self.assertIn(
            AgentProviderCapability.AGENT_EXECUTION,
            self.adapter.capabilities,
        )
        self.assertIn(
            AgentProviderCapability.INTERACTIVE_APPROVALS,
            self.adapter.capabilities,
        )
        self.assertNotIn(
            AgentProviderCapability.NATIVE_CONTEXT_COMPACTION,
            self.adapter.capabilities,
        )
        with self.assertRaises(AgentRuntimeUnsupportedCapability):
            await self.adapter.compact_session("claude-session-1")

    async def test_create_and_resume_keep_provider_native_identity_separate(self) -> None:
        self.transport.responses["session/create"] = {
            "session_id": "claude-session-1",
            "runtime_version": "0.2.156",
        }
        created = await self.adapter.create_session(
            AgentRuntimeSessionRequest(
                project_id="project-a",
                workspace_cwd="/workspace/a",
                sandbox="workspace-write",
                approval_policy="on-request",
                execution_id="exec-a",
                assignment_id="assignment-a",
                execution_workspace_id="workspace-lease-a",
                worker_id="worker-a",
                model="claude-sonnet-5",
                resource_ids=("resource-a",),
            )
        )
        self.assertEqual(created.provider_native_session_id, "claude-session-1")
        method, params = self.transport.calls[-1]
        self.assertEqual(method, "session/create")
        self.assertEqual(params["assignment_id"], "assignment-a")
        self.assertEqual(params["cwd"], "/workspace/a")

        self.transport.responses["session/resume"] = {}
        resumed = await self.adapter.resume_session(
            "claude-session-1",
            AgentRuntimeSessionRequest(
                project_id="project-a",
                workspace_cwd="/workspace/a",
            ),
        )
        self.assertEqual(resumed.provider_native_session_id, "claude-session-1")

    async def test_turn_interrupt_and_approval_use_bridge_transport(self) -> None:
        self.transport.responses["turn/start"] = {
            "turn_id": "turn-1",
            "model": "claude-sonnet-5",
            "usage": {"input_tokens": 4, "output_tokens": 8},
        }
        result = await self.adapter.start_turn(
            "claude-session-1",
            AgentRuntimeTurnRequest(
                message="change the file",
                workspace_cwd="/workspace/a",
                approval_policy="on-request",
            ),
        )
        self.assertEqual(result.provider_native_session_id, "claude-session-1")
        self.assertEqual(result.provider_native_turn_id, "turn-1")
        self.assertEqual(self.transport.calls[-1][0], "turn/start")

        await self.adapter.respond_approval(
            "approval-1",
            {"decision": "decline"},
        )
        self.assertEqual(
            self.transport.approvals,
            [("approval-1", {"decision": "decline"})],
        )

        self.transport.responses["turn/interrupt"] = {"interrupted": True}
        interrupted = await self.adapter.interrupt("claude-session-1")
        self.assertEqual(
            interrupted.provider_native_session_id,
            "claude-session-1",
        )

    async def test_event_projection_normalizes_runtime_events(self) -> None:
        observed: list[AgentRuntimeEvent] = []
        unsubscribe = self.adapter.subscribe_events(observed.append)
        self.transport.host.hub.publish(
            {
                "type": "claude.event",
                "message": {
                    "method": "runtime/event",
                    "params": {
                        "event_type": "tool.shell",
                        "session_id": "claude-session-1",
                        "turn_id": "turn-1",
                        "payload": {
                            "tool_name": "Bash",
                            "tool_use_id": "tool-1",
                        },
                    },
                },
            }
        )
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0].event_type, "tool.shell")
        self.assertEqual(
            observed[0].provider_native_session_id,
            "claude-session-1",
        )
        self.assertEqual(observed[0].provider_native_turn_id, "turn-1")
        unsubscribe()
        self.assertEqual(self.transport.host.hub.listeners, [])

    async def test_health_recovery_and_shutdown_use_common_runtime_lifecycle(self) -> None:
        self.assertEqual(await self.adapter.health(), AgentRuntimeHealth.HEALTHY)
        self.transport.last_error = "provider failed"
        self.assertEqual(await self.adapter.health(), AgentRuntimeHealth.DEGRADED)
        self.transport.last_error = None
        self.transport.ready = False
        self.assertEqual(
            await self.adapter.health(),
            AgentRuntimeHealth.UNAVAILABLE,
        )
        self.transport.ready = True
        self.assertEqual(await self.adapter.recover(), AgentRuntimeHealth.HEALTHY)
        self.assertEqual(self.transport.ensure_started_calls, 1)
        await self.adapter.shutdown()
        self.assertEqual(self.transport.stop_calls, 1)

    async def test_list_read_close_restore_are_provider_native_only(self) -> None:
        self.transport.responses["session/list"] = {
            "sessions": [{"session_id": "claude-session-1"}]
        }
        listed = await self.adapter.list_sessions(
            AgentRuntimeListRequest(workspace_cwd="/workspace/a", limit=10)
        )
        self.assertEqual(
            listed.payload["sessions"][0]["session_id"],
            "claude-session-1",
        )

        for method_name, operation in (
            ("session/read", self.adapter.read_session),
            ("session/close", self.adapter.close_session),
            ("session/restore", self.adapter.restore_session),
        ):
            self.transport.responses[method_name] = {"ok": True}
            result = await operation("claude-session-1")
            self.assertEqual(
                result.provider_native_session_id,
                "claude-session-1",
            )
            self.assertEqual(self.transport.calls[-1][0], method_name)


if __name__ == "__main__":
    unittest.main()
