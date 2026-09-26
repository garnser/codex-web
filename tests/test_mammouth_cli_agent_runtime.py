from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from codex_web.agent_runtime import (
    AgentRuntimeHealth,
    AgentRuntimeSessionRequest,
    AgentRuntimeTurnRequest,
)
from codex_web.cli_runtime import (
    CliRuntimeOutput,
    CliRuntimeReadiness,
    CliRuntimeReadinessStatus,
    CliRuntimeResult,
)
from codex_web.mammouth_cli_runtime import MammouthCliAdapter
from codex_web.services.mammouth_cli_agent_runtime import (
    MammouthCliAgentRuntimeAdapter,
    MammouthCliRuntimeError,
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

    async def run(self, command, *, on_output=None, timeout_seconds=None):
        del command, timeout_seconds
        if on_output is not None:
            await on_output(
                CliRuntimeOutput(
                    stream="stdout",
                    text='{"type":"turn.started","sessionID":"native-1","turnID":"turn-1"}',
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
        executable="mammouth",
        resolved_executable="/usr/bin/mammouth",
        message="ready",
    )


def _contained(_cwd: Path) -> bool:
    return True


class MammouthCliAgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_cli_is_degraded_until_containment_is_configured(self) -> None:
        adapter = MammouthCliAgentRuntimeAdapter(
            probe=_Probe(_ready()),
            runner=_Runner([]),
        )

        self.assertEqual(await adapter.health(), AgentRuntimeHealth.DEGRADED)

    async def test_start_turn_fails_closed_without_containment_authorizer(self) -> None:
        runner = _Runner([])
        adapter = MammouthCliAgentRuntimeAdapter(
            probe=_Probe(_ready()),
            runner=runner,
        )

        with self.assertRaisesRegex(
            MammouthCliRuntimeError,
            "containment authorizer",
        ):
            await adapter.start_turn(
                "logical-1",
                AgentRuntimeTurnRequest(
                    message="Do not escape the worker",
                    workspace_cwd="/workspace/repo",
                ),
            )

        self.assertEqual(runner.commands, [])

    async def test_start_turn_streams_events_and_selects_model(self) -> None:
        runner = _Runner(
            [
                '{"type":"turn.started","sessionID":"native-1","turnID":"turn-9"}',
                '{"type":"message","sessionID":"native-1","turnID":"turn-9","text":"done"}',
                '{"type":"turn.completed","sessionID":"native-1","turnID":"turn-9"}',
            ]
        )
        adapter = MammouthCliAgentRuntimeAdapter(
            cli=MammouthCliAdapter(),
            probe=_Probe(_ready()),
            runner=runner,
            execution_authorizer=_contained,
        )
        events = []
        adapter.subscribe_events(events.append)

        result = await adapter.start_turn(
            "logical-1",
            AgentRuntimeTurnRequest(
                message="Implement the fix",
                model="gpt-5.6-sol",
                workspace_cwd="/workspace/repo",
            ),
        )
        await asyncio.sleep(0)

        self.assertEqual(result.provider_native_session_id, "native-1")
        self.assertEqual(
            [event.event_type for event in events],
            ["turn.started", "message", "turn.completed"],
        )
        command, timeout = runner.commands[0]
        self.assertEqual(timeout, 1800.0)
        self.assertEqual(command.argv[0], "/usr/bin/mammouth")
        self.assertEqual(command.cwd.as_posix(), "/workspace/repo")
        self.assertIn("--model", command.argv)
        model_index = command.argv.index("--model")
        self.assertEqual(command.argv[model_index + 1], "mammouth-ai/gpt-5.6-sol")

    async def test_known_session_uses_explicit_mammouth_session_argument(self) -> None:
        runner = _Runner(
            ['{"type":"turn.started","sessionID":"native-1","turnID":"turn-1"}']
        )
        adapter = MammouthCliAgentRuntimeAdapter(
            probe=_Probe(_ready()),
            runner=runner,
            execution_authorizer=_contained,
        )

        await adapter.start_turn(
            "logical-1",
            AgentRuntimeTurnRequest(
                message="First",
                workspace_cwd="/workspace/repo",
            ),
        )
        await asyncio.sleep(0)

        runner.lines = [
            '{"type":"turn.started","sessionID":"native-1","turnID":"turn-2"}'
        ]
        await adapter.start_turn(
            "logical-1",
            AgentRuntimeTurnRequest(
                message="Continue",
                workspace_cwd="/workspace/repo",
            ),
        )
        await asyncio.sleep(0)

        second = runner.commands[1][0].argv
        self.assertIn("--session", second)
        self.assertEqual(second[second.index("--session") + 1], "native-1")

    async def test_explicit_resume_on_fresh_adapter_uses_native_session(self) -> None:
        runner = _Runner(
            ['{"type":"turn.started","sessionID":"native-existing","turnID":"turn-2"}']
        )
        adapter = MammouthCliAgentRuntimeAdapter(
            probe=_Probe(_ready()),
            runner=runner,
            execution_authorizer=_contained,
        )
        resumed = await adapter.resume_session(
            "native-existing",
            AgentRuntimeSessionRequest(
                project_id="project-1",
                workspace_cwd="/workspace/repo",
            ),
        )
        self.assertTrue(resumed.payload["resumed"])

        await adapter.start_turn(
            "native-existing",
            AgentRuntimeTurnRequest(
                message="Continue existing session",
                workspace_cwd="/workspace/repo",
            ),
        )
        await asyncio.sleep(0)

        argv = runner.commands[0][0].argv
        self.assertIn("--session", argv)
        self.assertEqual(argv[argv.index("--session") + 1], "native-existing")

    async def test_nonzero_exit_after_session_start_emits_safe_failure(self) -> None:
        runner = _Runner(
            ['{"type":"turn.started","sessionID":"native-1","turnID":"turn-1"}'],
            exit_code=7,
        )
        adapter = MammouthCliAgentRuntimeAdapter(
            probe=_Probe(_ready()),
            runner=runner,
            execution_authorizer=_contained,
        )
        events = []
        adapter.subscribe_events(events.append)

        result = await adapter.start_turn(
            "logical-1",
            AgentRuntimeTurnRequest(
                message="Fail",
                workspace_cwd="/workspace/repo",
            ),
        )
        self.assertEqual(result.provider_native_session_id, "native-1")
        await asyncio.sleep(0)

        self.assertEqual(events[-1].event_type, "turn.failed")
        self.assertEqual(
            events[-1].payload["error"],
            "MammouthCliRuntimeError",
        )
        self.assertNotIn("stderr", events[-1].payload)

    async def test_interrupt_cancels_owned_runner(self) -> None:
        runner = _BlockingRunner()
        adapter = MammouthCliAgentRuntimeAdapter(
            probe=_Probe(_ready()),
            runner=runner,
            execution_authorizer=_contained,
        )
        events = []
        adapter.subscribe_events(events.append)

        result = await adapter.start_turn(
            "logical-1",
            AgentRuntimeTurnRequest(
                message="Long turn",
                workspace_cwd="/workspace/repo",
            ),
        )
        self.assertEqual(result.provider_native_session_id, "native-1")
        await runner.started.wait()

        interrupted = await adapter.interrupt("logical-1")

        self.assertTrue(interrupted.payload["interrupted"])
        self.assertTrue(runner.cancelled)
        self.assertIn("turn.interrupted", [event.event_type for event in events])


if __name__ == "__main__":
    unittest.main()
