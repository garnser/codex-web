from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from codex_web.agent_providers import AgentProviderCapability
from codex_web.agent_runtime import (
    AgentRuntimeHealth,
    AgentRuntimeSessionRequest,
    AgentRuntimeTurnRequest,
)
from codex_web.services.claude_agent_runtime import ClaudeAgentRuntimeAdapter
from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter


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


class _RuntimeConformanceMixin:
    adapter = None
    transport = None
    native_session_id = ""
    native_turn_id = ""

    async def _make(self):
        raise NotImplementedError

    def _emit_terminal(self):
        raise NotImplementedError

    async def asyncSetUp(self):
        self.adapter, self.transport = await self._make()

    async def test_conformance_capabilities_are_unique_and_execution_is_claimed(self):
        self.assertIn(
            AgentProviderCapability.AGENT_EXECUTION,
            self.adapter.capabilities,
        )
        self.assertEqual(
            len(self.adapter.capabilities),
            len(set(self.adapter.capabilities)),
        )

    async def test_conformance_create_resume_execute_interrupt_close(self):
        created = await self.adapter.create_session(
            AgentRuntimeSessionRequest(
                project_id="project-a",
                execution_id="exec-a",
                assignment_id="assignment-a",
                workspace_cwd="/workspace/project-a",
                sandbox="workspace-write",
                approval_policy="on-request",
            )
        )
        self.assertEqual(
            created.provider_native_session_id,
            self.native_session_id,
        )

        if AgentProviderCapability.PERSISTENT_SESSIONS in self.adapter.capabilities:
            resumed = await self.adapter.resume_session(
                self.native_session_id,
                AgentRuntimeSessionRequest(project_id="project-a"),
            )
            self.assertEqual(
                resumed.provider_native_session_id,
                self.native_session_id,
            )

        turn = await self.adapter.start_turn(
            self.native_session_id,
            AgentRuntimeTurnRequest(message="implement it"),
        )
        self.assertEqual(turn.provider_native_turn_id, self.native_turn_id)

        if AgentProviderCapability.INTERRUPT_CANCEL in self.adapter.capabilities:
            interrupted = await self.adapter.interrupt(self.native_session_id)
            self.assertEqual(
                interrupted.provider_native_session_id,
                self.native_session_id,
            )

        closed = await self.adapter.close_session(self.native_session_id)
        self.assertEqual(
            closed.provider_native_session_id,
            self.native_session_id,
        )

    async def test_conformance_stream_terminal_event_and_unsubscribe(self):
        events = []
        unsubscribe = self.adapter.subscribe_events(events.append)

        self._emit_terminal()

        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].provider_native_session_id,
            self.native_session_id,
        )
        self.assertEqual(
            events[0].provider_native_turn_id,
            self.native_turn_id,
        )
        unsubscribe()
        self.assertEqual(self.transport.host.hub.listeners, set())

    async def test_conformance_approval_denial_is_transport_only(self):
        await self.adapter.respond_approval(
            "approval-1",
            {"decision": "decline", "reason": "not approved"},
        )

        self.transport.respond_to_server_request.assert_awaited_once_with(
            "approval-1",
            {"decision": "decline", "reason": "not approved"},
        )

    async def test_conformance_recovery_and_shutdown_use_runtime_lifecycle(self):
        health = await self.adapter.recover()
        self.assertEqual(health, AgentRuntimeHealth.HEALTHY)
        self.transport.ensure_started.assert_awaited_once()

        await self.adapter.shutdown()
        self.transport.stop.assert_awaited_once()

    async def test_conformance_unsupported_native_compaction_fails_closed(self):
        if (
            AgentProviderCapability.NATIVE_CONTEXT_COMPACTION
            in self.adapter.capabilities
        ):
            result = await self.adapter.compact_session(self.native_session_id)
            self.assertEqual(
                result.provider_native_session_id,
                self.native_session_id,
            )
        else:
            with self.assertRaises(RuntimeError):
                await self.adapter.compact_session(self.native_session_id)


class CodexAgentRuntimeConformanceTests(
    _RuntimeConformanceMixin,
    unittest.IsolatedAsyncioTestCase,
):
    native_session_id = "codex-session-1"
    native_turn_id = "codex-turn-1"

    async def _make(self):
        hub = _Hub()

        async def request(method, params=None):
            if method == "thread/start":
                return {"thread": {"id": self.native_session_id}}
            if method == "thread/resume":
                return {"thread": {"id": self.native_session_id}}
            if method == "turn/start":
                return {
                    "thread": {"id": self.native_session_id},
                    "turn": {"id": self.native_turn_id},
                }
            if method in {
                "turn/interrupt",
                "thread/archive",
                "thread/compact/start",
            }:
                return {"ok": True}
            raise RuntimeError(f"unexpected method: {method}")

        transport = SimpleNamespace(
            host=SimpleNamespace(hub=hub),
            request=AsyncMock(side_effect=request),
            respond_to_server_request=AsyncMock(),
            ensure_started=AsyncMock(),
            stop=AsyncMock(),
            status=lambda: SimpleNamespace(ready=True, last_error=None),
            proc=None,
        )
        return CodexAgentRuntimeAdapter(transport), transport

    def _emit_terminal(self):
        self.transport.host.hub.emit(
            {
                "type": "codex.event",
                "message": {
                    "method": "turn/completed",
                    "params": {
                        "threadId": self.native_session_id,
                        "turnId": self.native_turn_id,
                        "turn": {"id": self.native_turn_id},
                    },
                },
            }
        )


class ClaudeAgentRuntimeConformanceTests(
    _RuntimeConformanceMixin,
    unittest.IsolatedAsyncioTestCase,
):
    native_session_id = "claude-session-1"
    native_turn_id = "claude-turn-1"

    async def _make(self):
        hub = _Hub()

        async def request(method, params=None):
            if method in {"session/create", "session/resume"}:
                return {
                    "type": "system",
                    "session_id": self.native_session_id,
                }
            if method == "turn/start":
                return {
                    "type": "result",
                    "subtype": "success",
                    "session_id": self.native_session_id,
                    "turn_id": self.native_turn_id,
                }
            if method in {"turn/interrupt", "session/close"}:
                return {
                    "type": "control_response",
                    "session_id": self.native_session_id,
                }
            raise RuntimeError(f"unexpected method: {method}")

        transport = SimpleNamespace(
            host=SimpleNamespace(hub=hub),
            request=AsyncMock(side_effect=request),
            respond_to_server_request=AsyncMock(),
            ensure_started=AsyncMock(),
            stop=AsyncMock(),
            status=lambda: SimpleNamespace(ready=True, last_error=None),
            proc=None,
        )
        return ClaudeAgentRuntimeAdapter(transport), transport

    def _emit_terminal(self):
        self.transport.host.hub.emit(
            {
                "type": "claude.event",
                "message": {
                    "type": "result",
                    "subtype": "success",
                    "session_id": self.native_session_id,
                    "turn_id": self.native_turn_id,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        )


if __name__ == "__main__":
    unittest.main()
