from __future__ import annotations

import asyncio
import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException

from codex_web.runtime.codex import (
    SENSITIVE_CODEX_DIAGNOSTIC_ENV_KEYS,
    TRUSTED_LOCAL_CHILD_HOME,
    CodexRuntime,
    install_codex_runtime,
    redact_codex_diagnostic,
    request_timeout,
    trusted_local_codex_command,
)


class _Hub:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish(self, event: dict) -> None:
        self.events.append(event)


class _Host:
    def __init__(self) -> None:
        self.hub = _Hub()
        self.bot_events: list[dict] = []
        self.activity: list[dict] = []
        self.outbound: list[dict] = []
        self.queue_drains: list[str | None] = []
        self.approval_policy = "on-request"
        self.approval_requests: list[dict] = []

    def _approval_thread_id(self, message: dict) -> str:
        return "thread-1"

    def _approval_run_settings(self, message: dict) -> SimpleNamespace:
        return SimpleNamespace(approval_policy=self.approval_policy)

    def _approval_result(self, method: str, decision: str) -> dict:
        return {"decision": decision, "method": method}

    def _append_bot_event(self, event: dict) -> None:
        self.bot_events.append(event)

    async def _record_bot_approval_request(self, message: dict) -> None:
        self.approval_requests.append(message)

    def _record_thread_activity(self, message: dict) -> None:
        self.activity.append(message)

    def _record_terminal_turn_result(self, message: dict) -> bool:
        return False

    async def _restore_bot_thread_name(self, thread_id: str | None) -> None:
        return None

    def _schedule_queue_drain(self, thread_id: str | None) -> None:
        self.queue_drains.append(thread_id)

    async def _record_bot_outbound(self, message: dict) -> None:
        self.outbound.append(message)


class TrustedLocalCodexSecurityPolicyTests(unittest.TestCase):
    def test_default_command_filters_repository_child_credentials(self) -> None:
        command = trusted_local_codex_command()
        joined = " ".join(command)

        self.assertEqual(command[0], "codex")
        self.assertEqual(command[-1], "app-server")
        self.assertIn('shell_environment_policy.inherit="none"', joined)
        self.assertIn(
            "shell_environment_policy.ignore_default_excludes=false",
            joined,
        )
        self.assertIn(
            'shell_environment_policy.filters.CODEX_ACCESS_TOKEN="exclude"',
            joined,
        )
        self.assertIn(
            'shell_environment_policy.filters.CODEX_API_KEY="exclude"',
            joined,
        )
        self.assertIn(
            'shell_environment_policy.filters.OPENAI_API_KEY="exclude"',
            joined,
        )
        self.assertIn("allow_login_shell=false", joined)
        self.assertIn(f'HOME="{TRUSTED_LOCAL_CHILD_HOME}"', joined)
        self.assertIn('shell_environment_policy.filters.CODEX_HOME="exclude"', joined)
        self.assertIn('shell_environment_policy.filters.XDG_RUNTIME_DIR="exclude"', joined)
        self.assertIn('shell_environment_policy.filters.DBUS_SESSION_BUS_ADDRESS="exclude"', joined)
        self.assertIn('shell_environment_policy.filters.SSH_AUTH_SOCK="exclude"', joined)
        self.assertIn('shell_environment_policy.filters.GNOME_KEYRING_CONTROL="exclude"', joined)
        self.assertNotIn("auth.json", joined)

    def test_runtime_uses_hardened_command_by_default(self) -> None:
        runtime = CodexRuntime(_Host())
        self.assertEqual(runtime.command, trusted_local_codex_command())

    def test_explicit_command_is_preserved(self) -> None:
        command = ("custom-codex", "app-server")
        runtime = CodexRuntime(_Host(), command=command)
        self.assertEqual(runtime.command, command)

    def test_diagnostics_redact_tokens_and_auth_paths(self) -> None:
        environment = {
            "CODEX_ACCESS_TOKEN": "codex-super-secret-token",
            "OPENAI_API_KEY": "sk-supersecretapikey",
            "CODEX_HOME": "/home/operator/.codex-private",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus-secret",
        }
        with patch.dict(os.environ, environment, clear=False):
            text = redact_codex_diagnostic(
                "token=codex-super-secret-token "
                "key=sk-supersecretapikey "
                "home=/home/operator/.codex-private "
                "bus=unix:path=/run/user/1000/bus-secret "
                "Authorization: Bearer bearer-super-secret"
            )

        self.assertNotIn("codex-super-secret-token", text)
        self.assertNotIn("sk-supersecretapikey", text)
        self.assertNotIn("/home/operator/.codex-private", text)
        self.assertNotIn("/run/user/1000/bus-secret", text)
        self.assertNotIn("bearer-super-secret", text)
        self.assertIn("[REDACTED]", text)

    def test_sensitive_diagnostic_carriers_cover_auth_and_keyring_paths(self) -> None:
        self.assertTrue({
            "CODEX_ACCESS_TOKEN",
            "OPENAI_API_KEY",
            "CODEX_HOME",
            "XDG_RUNTIME_DIR",
            "DBUS_SESSION_BUS_ADDRESS",
            "SSH_AUTH_SOCK",
            "GNOME_KEYRING_CONTROL",
        }.issubset(set(SENSITIVE_CODEX_DIAGNOSTIC_ENV_KEYS)))


class CodexRuntimeInstallationTests(unittest.TestCase):
    def test_install_replaces_legacy_runtime_and_is_idempotent(self) -> None:
        app = FastAPI()
        host = _Host()
        host.codex = object()
        host.CodexAppServer = object

        first = install_codex_runtime(app, host)
        second = install_codex_runtime(app, host)

        self.assertIs(first, second)
        self.assertIs(host.codex, first)
        self.assertIs(app.state.codex_runtime, first)
        self.assertIs(host.CodexAppServer, CodexRuntime)
        self.assertIs(host._codex_request_timeout, request_timeout)

    def test_request_timeout_contract_matches_runtime_expectations(self) -> None:
        self.assertEqual(request_timeout("initialize"), 15)
        self.assertEqual(request_timeout("thread/read"), 10)
        self.assertEqual(request_timeout("turn/start"), 60)
        self.assertEqual(request_timeout("turn/interrupt"), 10)
        self.assertEqual(request_timeout("unknown/method"), 20)


class CodexRuntimeProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.host = _Host()
        self.runtime = CodexRuntime(self.host)

    async def test_process_launch_closes_unrelated_file_descriptors(self) -> None:
        captured = {}

        def fail_popen(*args, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop after capture")

        runtime = CodexRuntime(self.host, popen=fail_popen)
        with self.assertRaisesRegex(RuntimeError, "stop after capture"):
            await runtime.start()

        self.assertIs(captured.get("close_fds"), True)

    async def test_stderr_is_redacted_before_runtime_state_and_events(self) -> None:
        secret = "codex-stderr-secret-token"
        runtime = CodexRuntime(self.host)
        runtime.proc = SimpleNamespace(
            stderr=io.StringIO(f"failure token={secret} Bearer bearer-stderr-secret\\n"),
        )
        with patch.dict(os.environ, {"CODEX_ACCESS_TOKEN": secret}, clear=False):
            await runtime._stderr_loop()

        self.assertNotIn(secret, runtime.last_error or "")
        self.assertNotIn("bearer-stderr-secret", runtime.last_error or "")
        event = self.host.hub.events[-1]
        self.assertEqual(event["type"], "codex.stderr")
        self.assertNotIn(secret, event["text"])
        self.assertNotIn("bearer-stderr-secret", event["text"])

    async def test_rpc_response_resolves_matching_future(self) -> None:
        future = asyncio.get_running_loop().create_future()
        self.runtime.pending[7] = future

        await self.runtime._handle_message({"id": 7, "result": {"ok": True}})

        self.assertEqual(await future, {"ok": True})
        self.assertNotIn(7, self.runtime.pending)
        self.assertEqual(self.host.hub.events[-1]["type"], "rpc.response")

    async def test_rpc_error_rejects_matching_future(self) -> None:
        future = asyncio.get_running_loop().create_future()
        self.runtime.pending[9] = future

        await self.runtime._handle_message({"id": 9, "error": {"message": "boom"}})

        with self.assertRaises(RuntimeError):
            await future
        self.assertNotIn(9, self.runtime.pending)

    async def test_message_projection_failure_does_not_kill_response_reader(self) -> None:
        def fail_activity(_message):
            raise AttributeError("projection state is unavailable")

        self.host._record_thread_activity = fail_activity
        response = asyncio.get_running_loop().create_future()
        self.runtime.pending[7] = response
        await self.runtime._handle_message_safely(
            {"method": "thread/status/changed", "params": {}}
        )
        await self.runtime._handle_message_safely(
            {"id": 7, "result": {"ok": True}}
        )

        self.assertEqual(await response, {"ok": True})
        self.assertIn("projection state is unavailable", self.runtime.last_error)
        self.assertTrue(
            any(event.get("type") == "codex.error" for event in self.host.hub.events)
        )

    async def test_rpc_timeout_invalidates_live_process_generation(self) -> None:
        process = SimpleNamespace(poll=lambda: None)
        self.runtime.proc = process
        self.runtime.ready.set()
        self.runtime.ensure_started = AsyncMock()
        self.runtime._send = AsyncMock()
        self.runtime.stop = AsyncMock()

        with patch("codex_web.runtime.codex.request_timeout", return_value=0.001):
            with self.assertRaises(HTTPException) as caught:
                await self.runtime.request("thread/read", {})

        self.assertEqual(caught.exception.status_code, 504)
        self.assertFalse(self.runtime.ready.is_set())
        self.runtime.stop.assert_awaited_once()

    async def test_dead_reader_restarts_even_if_stale_ready_flag_is_set(self) -> None:
        async def complete():
            return None

        self.runtime.proc = SimpleNamespace(poll=lambda: None)
        self.runtime.reader_task = asyncio.create_task(complete())
        await self.runtime.reader_task
        self.runtime.ready.set()
        self.runtime.start = AsyncMock()

        await self.runtime.ensure_started()

        self.runtime.start.assert_awaited_once()

    async def test_approval_policy_never_auto_resolves_without_queueing(self) -> None:
        self.host.approval_policy = "never"
        self.runtime._send = AsyncMock()
        message = {
            "id": 11,
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-1"},
        }

        await self.runtime._handle_message(message)

        self.runtime._send.assert_awaited_once_with(
            {
                "id": 11,
                "result": {
                    "decision": "acceptForSession",
                    "method": "item/commandExecution/requestApproval",
                },
            }
        )
        self.assertNotIn(11, self.runtime.pending_approvals)
        self.assertEqual(self.host.bot_events[-1]["type"], "approval_auto_resolved")
        self.assertEqual(self.host.hub.events[-1]["type"], "approval.auto_resolved")

    async def test_interactive_approval_is_retained_and_published(self) -> None:
        message = {
            "id": 12,
            "method": "item/fileChange/requestApproval",
            "params": {"threadId": "thread-1"},
        }

        await self.runtime._handle_message(message)

        self.assertEqual(self.runtime.pending_approvals[12], message)
        self.assertEqual(self.host.approval_requests, [message])
        self.assertEqual(self.host.hub.events[-1]["type"], "approval.request")

    async def test_assignment_bound_approval_ids_are_namespaced_but_rpc_response_uses_raw_id(self) -> None:
        self.runtime.approval_namespace = "assignment-1"
        self.runtime._send = AsyncMock()
        message = {
            "id": 12,
            "method": "item/fileChange/requestApproval",
            "params": {"threadId": "thread-1"},
        }

        await self.runtime._handle_message(message)

        public_id = "assignment-1:12"
        self.assertIn(public_id, self.runtime.pending_approvals)
        self.assertEqual(
            self.runtime.pending_approvals[public_id]["id"],
            public_id,
        )
        self.assertEqual(
            self.runtime.pending_approval_rpc_ids[public_id],
            12,
        )
        self.assertEqual(self.host.approval_requests[0]["id"], public_id)

        await self.runtime.respond_to_server_request(
            public_id,
            {"decision": "accept"},
        )

        self.runtime._send.assert_awaited_once_with(
            {"id": 12, "result": {"decision": "accept"}}
        )
        self.assertNotIn(public_id, self.runtime.pending_approvals)
        self.assertNotIn(public_id, self.runtime.pending_approval_rpc_ids)
        self.assertEqual(
            self.host.hub.events[-1]["id"],
            public_id,
        )

    async def test_terminal_turn_event_schedules_queue_drain_and_projects_event(self) -> None:
        message = {
            "method": "turn/completed",
            "params": {"threadId": "thread-7", "turn": {"threadId": "thread-7"}},
        }

        await self.runtime._handle_message(message)

        self.assertEqual(self.host.activity, [message])
        self.assertEqual(self.host.queue_drains, ["thread-7"])
        self.assertEqual(self.host.outbound, [message])
        self.assertEqual(self.host.hub.events[-1], {"type": "codex.event", "message": message})


if __name__ == "__main__":
    unittest.main()
