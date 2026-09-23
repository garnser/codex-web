from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

from codex_web.agent_runtime import (
    AgentRuntimeHealth,
    AgentRuntimeSessionRequest,
    AgentRuntimeTurnRequest,
)
from codex_web.cli_runtime import (
    CliRuntimeOutput,
    CliRuntimeProbe,
    CliRuntimeReadiness,
    CliRuntimeReadinessStatus,
    CliRuntimeResult,
    CliRuntimeRunner,
)
from codex_web.codex_cli_runtime import CodexCliAdapter
from codex_web.services.codex_cli_agent_runtime import (
    CodexCliAgentRuntimeAdapter,
    CodexCliRuntimeError,
)


class _Probe:
    def __init__(self, readiness: CliRuntimeReadiness) -> None:
        self.readiness = readiness
        self.calls = 0

    def evaluate(self, adapter):
        self.calls += 1
        return self.readiness


class _Runner:
    def __init__(self, lines, *, exit_code: int = 0) -> None:
        self.lines = list(lines)
        self.exit_code = exit_code
        self.commands = []

    async def run(self, command, *, on_output=None, timeout_seconds=None):
        self.commands.append((command, timeout_seconds))
        for line in self.lines:
            if on_output is not None:
                await on_output(CliRuntimeOutput(stream="stdout", text=line))
        return CliRuntimeResult(exit_code=self.exit_code)


class _BlockingRunner:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self.commands = []

    async def run(self, command, *, on_output=None, timeout_seconds=None):
        self.commands.append((command, timeout_seconds))
        if on_output is not None:
            await on_output(
                CliRuntimeOutput(
                    stream="stdout",
                    text='{"type":"thread.started","thread_id":"native-1"}',
                )
            )
            await on_output(
                CliRuntimeOutput(
                    stream="stdout",
                    text='{"type":"turn.started","turn_id":"turn-1"}',
                )
            )
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _ready() -> CliRuntimeReadiness:
    return CliRuntimeReadiness(
        status=CliRuntimeReadinessStatus.READY,
        executable="codex",
        resolved_executable="/usr/bin/codex",
        message="ready",
    )


class CodexCliAgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_uses_cli_readiness_without_exposing_credentials(self) -> None:
        probe = _Probe(_ready())
        adapter = CodexCliAgentRuntimeAdapter(probe=probe, runner=_Runner([]))

        self.assertEqual(await adapter.health(), AgentRuntimeHealth.HEALTHY)
        self.assertEqual(probe.calls, 1)

    async def test_start_turn_streams_structured_events_and_captures_native_session(self) -> None:
        runner = _Runner(
            [
                '{"type":"thread.started","thread_id":"native-1"}',
                '{"type":"turn.started","turn_id":"turn-9"}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
                '{"type":"turn.completed","turn_id":"turn-9","usage":{"input_tokens":12}}',
            ]
        )
        adapter = CodexCliAgentRuntimeAdapter(
            cli=CodexCliAdapter(
                executable="codex",
                sandbox="workspace-write",
                approval_policy="never",
            ),
            probe=_Probe(_ready()),
            runner=runner,
        )
        events = []
        adapter.subscribe_events(events.append)

        result = await adapter.start_turn(
            "thread-local",
            AgentRuntimeTurnRequest(
                message="Implement the fix",
                model="gpt-5.6-sol",
                workspace_cwd="/workspace/repo",
                approval_policy="never",
            ),
        )
        await asyncio.sleep(0)

        self.assertEqual(result.provider_native_session_id, "native-1")
        self.assertEqual(
            [event.event_type for event in events],
            [
                "thread.started",
                "turn.started",
                "item.completed",
                "turn.completed",
            ],
        )
        command, timeout = runner.commands[0]
        self.assertEqual(timeout, 1800.0)
        self.assertEqual(command.argv[0], "/usr/bin/codex")
        self.assertIn("--json", command.argv)
        self.assertEqual(command.cwd.as_posix(), "/workspace/repo")
        self.assertNotIn("OPENAI_API_KEY", command.environment)

    async def test_resume_session_carries_canonical_sandbox_into_cli_command(self) -> None:
        runner = _Runner(
            [
                '{"type":"thread.started","thread_id":"native-readonly"}',
                '{"type":"turn.completed","turn_id":"turn-readonly"}',
            ]
        )
        adapter = CodexCliAgentRuntimeAdapter(
            probe=_Probe(_ready()),
            runner=runner,
        )
        await adapter.resume_session(
            "thread-local",
            AgentRuntimeSessionRequest(
                project_id="home",
                sandbox="read-only",
                approval_policy="never",
                workspace_cwd="/workspace/repo",
            ),
        )

        await adapter.start_turn(
            "thread-local",
            AgentRuntimeTurnRequest(
                message="Inspect only",
                workspace_cwd="/workspace/repo",
            ),
        )
        await asyncio.sleep(0)

        argv = runner.commands[0][0].argv
        sandbox_index = argv.index("--sandbox")
        self.assertEqual(argv[sandbox_index + 1], "read-only")

    async def test_known_provider_session_uses_codex_resume_command(self) -> None:
        runner = _Runner(
            [
                '{"type":"thread.started","thread_id":"native-1"}',
                '{"type":"turn.completed","turn_id":"turn-1"}',
            ]
        )
        adapter = CodexCliAgentRuntimeAdapter(
            cli=CodexCliAdapter(approval_policy="never"),
            probe=_Probe(_ready()),
            runner=runner,
        )

        await adapter.start_turn(
            "thread-local",
            AgentRuntimeTurnRequest(
                message="First turn",
                workspace_cwd="/workspace/repo",
                approval_policy="never",
            ),
        )
        await asyncio.sleep(0)

        runner.lines = [
            '{"type":"thread.started","thread_id":"native-1"}',
            '{"type":"turn.completed","turn_id":"turn-2"}',
        ]
        await adapter.start_turn(
            "thread-local",
            AgentRuntimeTurnRequest(
                message="Continue",
                workspace_cwd="/workspace/repo",
                approval_policy="never",
            ),
        )
        await asyncio.sleep(0)

        second = runner.commands[1][0].argv
        self.assertIn("resume", second)
        self.assertIn("native-1", second)
        self.assertEqual(second[-1], "Continue")

    async def test_interrupt_cancels_runner_and_emits_interrupted_event(self) -> None:
        runner = _BlockingRunner()
        adapter = CodexCliAgentRuntimeAdapter(
            probe=_Probe(_ready()),
            runner=runner,
        )
        events = []
        adapter.subscribe_events(events.append)

        result = await adapter.start_turn(
            "thread-local",
            AgentRuntimeTurnRequest(
                message="Long running turn",
                workspace_cwd="/workspace/repo",
                approval_policy="never",
            ),
        )
        self.assertEqual(result.provider_native_session_id, "native-1")
        await runner.started.wait()

        interrupted = await adapter.interrupt("thread-local")

        self.assertTrue(interrupted.payload["interrupted"])
        self.assertTrue(runner.cancelled)
        self.assertIn(
            "turn.interrupted",
            [event.event_type for event in events],
        )

    async def test_nonzero_exit_after_start_emits_safe_failure_event(self) -> None:
        runner = _Runner(
            ['{"type":"thread.started","thread_id":"native-1"}'],
            exit_code=7,
        )
        adapter = CodexCliAgentRuntimeAdapter(
            probe=_Probe(_ready()),
            runner=runner,
        )
        events = []
        adapter.subscribe_events(events.append)

        result = await adapter.start_turn(
            "thread-local",
            AgentRuntimeTurnRequest(
                message="Fail",
                workspace_cwd="/workspace/repo",
            ),
        )
        self.assertEqual(result.provider_native_session_id, "native-1")
        await asyncio.sleep(0)

        failure = events[-1]
        self.assertEqual(failure.event_type, "turn.failed")
        self.assertEqual(failure.payload["error"], "CodexCliRuntimeError")
        self.assertNotIn("stderr", failure.payload)

    async def test_real_process_uses_local_cli_auth_without_direct_api_key(self) -> None:
        if os.name != "posix":
            self.skipTest("executable fixture requires POSIX chmod semantics")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            workspace.mkdir()
            codex_home = root / ".codex"
            codex_home.mkdir()
            executable = root / "fake-codex"
            executable.write_text(
                "#!" + sys.executable + "\n"
                "import json, os, sys\n"
                "args = sys.argv[1:]\n"
                "if args == ['login', 'status']:\n"
                "    if os.environ.get('CODEX_HOME') and not os.environ.get('OPENAI_API_KEY'):\n"
                "        raise SystemExit(0)\n"
                "    raise SystemExit(7)\n"
                "if 'exec' in args:\n"
                "    if os.environ.get('OPENAI_API_KEY'):\n"
                "        raise SystemExit(9)\n"
                "    print(json.dumps({'type':'thread.started','thread_id':'native-local-auth'}), flush=True)\n"
                "    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'local auth ok'}}), flush=True)\n"
                "    print(json.dumps({'type':'turn.completed','turn_id':'turn-local-auth'}), flush=True)\n"
                "    raise SystemExit(0)\n"
                "raise SystemExit(11)\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)

            environment = {
                "HOME": str(root),
                "CODEX_HOME": str(codex_home),
                "PATH": os.environ.get("PATH", ""),
                "UNRELATED_PARENT_SECRET": "must-not-pass",
            }
            adapter = CodexCliAgentRuntimeAdapter(
                cli=CodexCliAdapter(
                    executable=str(executable),
                    sandbox="workspace-write",
                    approval_policy="never",
                ),
                probe=CliRuntimeProbe(
                    environment_allowlist=("HOME", "CODEX_HOME", "PATH"),
                    environ=environment,
                ),
                runner=CliRuntimeRunner(
                    environment_allowlist=("HOME", "CODEX_HOME", "PATH"),
                    environ=environment,
                ),
                start_timeout_seconds=5,
            )
            events = []
            adapter.subscribe_events(events.append)

            self.assertEqual(await adapter.health(), AgentRuntimeHealth.HEALTHY)
            result = await adapter.start_turn(
                "thread-local",
                AgentRuntimeTurnRequest(
                    message="Use the existing CLI login",
                    workspace_cwd=str(workspace),
                    approval_policy="never",
                ),
            )
            for _ in range(50):
                if any(event.event_type == "turn.completed" for event in events):
                    break
                await asyncio.sleep(0.01)

            self.assertEqual(
                result.provider_native_session_id,
                "native-local-auth",
            )
            self.assertIn(
                "item.completed",
                [event.event_type for event in events],
            )
            self.assertIn(
                "turn.completed",
                [event.event_type for event in events],
            )
            self.assertNotIn("OPENAI_API_KEY", environment)
            self.assertNotIn(
                "UNRELATED_PARENT_SECRET",
                adapter.runner.environment_allowlist,
            )

    async def test_unready_cli_fails_before_process_launch(self) -> None:
        runner = _Runner([])
        adapter = CodexCliAgentRuntimeAdapter(
            probe=_Probe(
                CliRuntimeReadiness(
                    status=CliRuntimeReadinessStatus.UNAUTHENTICATED,
                    executable="codex",
                    resolved_executable="/usr/bin/codex",
                    message="Codex CLI is not authenticated.",
                )
            ),
            runner=runner,
        )

        with self.assertRaisesRegex(CodexCliRuntimeError, "not authenticated"):
            await adapter.start_turn(
                "thread-local",
                AgentRuntimeTurnRequest(
                    message="Do not run",
                    workspace_cwd="/workspace/repo",
                ),
            )

        self.assertEqual(runner.commands, [])


if __name__ == "__main__":
    unittest.main()
