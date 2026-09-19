from __future__ import annotations

import asyncio
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from codex_web.runtime.claude import ClaudeCodeRuntime


class _Hub:
    def __init__(self) -> None:
        self.events = []

    async def publish(self, event):
        self.events.append(event)


class _Proc:
    def __init__(self) -> None:
        self.returncode = None
        self.stdin = SimpleNamespace(write=lambda _value: None, flush=lambda: None)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class _Host:
    def __init__(self) -> None:
        self.hub = _Hub()
        self.approvals = []
        self.registered = []

    async def _record_bot_approval_request(self, message):
        self.approvals.append(message)

    async def _register_canonical_approval_request(self, message):
        self.registered.append(message)


class ClaudeCodeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.session_id = str(uuid.uuid4())
        self.host = _Host()
        self.runtime = ClaudeCodeRuntime(
            self.host,
            command=(
                "claude",
                "--print",
                "--session-id",
                self.session_id,
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
            ),
        )
        self.runtime.proc = _Proc()
        self.runtime.ready.set()

    async def test_session_create_returns_assignment_bound_native_id(self) -> None:
        result = await self.runtime.request("session/create", {"project_id": "p"})

        self.assertEqual(result["session_id"], self.session_id)
        self.assertEqual(result["subtype"], "init")

    async def test_turn_writes_stream_user_message_and_resolves_on_result(self) -> None:
        self.runtime._send = AsyncMock()
        pending = asyncio.create_task(
            self.runtime.request(
                "turn/start",
                {"session_id": self.session_id, "message": "Implement it"},
            )
        )
        await asyncio.sleep(0)

        self.runtime._send.assert_awaited_once_with(
            {
                "type": "user",
                "message": {"role": "user", "content": "Implement it"},
                "parent_tool_use_id": None,
                "session_id": self.session_id,
            }
        )
        native = {
            "type": "result",
            "subtype": "success",
            "session_id": self.session_id,
            "usage": {"input_tokens": 12, "output_tokens": 4},
        }
        await self.runtime._handle_message(native)

        self.assertEqual(await pending, native)
        self.assertEqual(
            self.host.hub.events[-1],
            {"type": "claude.event", "message": native},
        )

    async def test_interrupt_uses_control_protocol_and_waits_for_matching_response(self) -> None:
        self.runtime._send = AsyncMock()
        pending = asyncio.create_task(
            self.runtime.request(
                "turn/interrupt",
                {"session_id": self.session_id},
            )
        )
        await asyncio.sleep(0)
        sent = self.runtime._send.await_args.args[0]
        request_id = sent["request_id"]
        self.assertEqual(sent["request"], {"subtype": "interrupt"})

        response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": {},
            },
        }
        await self.runtime._handle_message(response)

        self.assertEqual(await pending, response)

    async def test_tool_permission_becomes_namespaced_canonical_approval(self) -> None:
        self.runtime.approval_namespace = "assignment-1"
        message = {
            "type": "control_request",
            "request_id": "req-1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "tool_use_id": "toolu-1",
                "input": {"command": "git status"},
            },
        }

        await self.runtime._handle_message(message)

        public_id = "assignment-1:req-1"
        self.assertIn(public_id, self.runtime.pending_approvals)
        self.assertEqual(self.runtime.pending_approval_rpc_ids[public_id], "req-1")
        self.assertEqual(self.host.registered[0]["id"], public_id)
        self.assertEqual(
            self.host.registered[0]["method"],
            "claude/tool/canUse",
        )
        self.assertEqual(self.host.approvals[0]["id"], public_id)
        self.assertEqual(self.host.hub.events[-1]["type"], "approval.request")

    async def test_approval_response_maps_canonical_decision_to_claude_control_shape(self) -> None:
        self.runtime.approval_namespace = "assignment-1"
        self.runtime._send = AsyncMock()
        await self.runtime._handle_permission_request(
            {
                "type": "control_request",
                "request_id": "req-1",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "Bash",
                    "tool_use_id": "toolu-1",
                    "input": {"command": "git status"},
                },
            }
        )
        self.runtime._send.reset_mock()

        await self.runtime.respond_to_server_request(
            "assignment-1:req-1",
            {"decision": "acceptForSession"},
        )

        sent = self.runtime._send.await_args.args[0]
        self.assertEqual(sent["type"], "control_response")
        self.assertEqual(sent["response"]["request_id"], "req-1")
        self.assertEqual(
            sent["response"]["response"],
            {
                "behavior": "allow",
                "updatedInput": {"command": "git status"},
            },
        )
        self.assertNotIn("assignment-1:req-1", self.runtime.pending_approvals)

    async def test_denied_approval_fails_closed(self) -> None:
        self.runtime.approval_namespace = "assignment-1"
        self.runtime._send = AsyncMock()
        await self.runtime._handle_permission_request(
            {
                "type": "control_request",
                "request_id": "req-2",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "Write",
                    "tool_use_id": "toolu-2",
                    "input": {"file_path": "x.txt", "content": "x"},
                },
            }
        )
        self.runtime._send.reset_mock()

        await self.runtime.respond_to_server_request(
            "assignment-1:req-2",
            {"decision": "decline", "reason": "not approved"},
        )

        response = self.runtime._send.await_args.args[0]["response"]["response"]
        self.assertEqual(response["behavior"], "deny")
        self.assertEqual(response["message"], "not approved")

    async def test_resume_mismatch_fails_closed_instead_of_switching_native_identity(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "relaunched"):
            await self.runtime.request(
                "session/resume",
                {"session_id": str(uuid.uuid4())},
            )


if __name__ == "__main__":
    unittest.main()
