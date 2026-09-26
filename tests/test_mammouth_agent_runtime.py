from __future__ import annotations

import asyncio
import unittest

from codex_web.agent_runtime import (
    AgentRuntimeHealth,
    AgentRuntimeSessionRequest,
    AgentRuntimeTurnRequest,
)
from codex_web.cli_runtime import (
    CliRuntimeCommand,
    CliRuntimeOutput,
    CliRuntimeReadiness,
    CliRuntimeReadinessStatus,
    CliRuntimeResult,
)
from codex_web.services.mammouth_agent_runtime import (
    MammouthCliAgentRuntimeAdapter,
    MammouthCliRuntimeError,
)


class _Probe:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.calls = 0

    def evaluate(self, cli):
        self.calls += 1
        return CliRuntimeReadiness(
            status=(
                CliRuntimeReadinessStatus.READY
                if self.ready
                else CliRuntimeReadinessStatus.UNAUTHENTICATED
            ),
            executable=cli.executable,
            resolved_executable="/worker/bin/mammouth",
            message="ready" if self.ready else "not ready",
        )


class _Executor:
    def __init__(self, lines=(), *, exit_code: int = 0) -> None:
        self.lines = list(lines)
        self.exit_code = exit_code
        self.calls = []

    async def __call__(
        self,
        command,
        *,
        binding,
        on_output,
        timeout_seconds,
    ):
        self.calls.append((command, binding, timeout_seconds))
        for line in self.lines:
            await on_output(CliRuntimeOutput(stream="stdout", text=line))
        return CliRuntimeResult(exit_code=self.exit_code)


class _BlockingExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self.binding = None

    async def __call__(
        self,
        command,
        *,
        binding,
        on_output,
        timeout_seconds,
    ):
        self.binding = binding
        await on_output(
            CliRuntimeOutput(
                stream="stdout",
                text='{"type":"step_start","sessionID":"native-1",'
                '"part":{"messageID":"message-1"}}',
            )
        )
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def _pump_until_terminal(
    events,
    *,
    terminal: str = "turn/completed",
    limit: int = 100,
) -> None:
    for _ in range(limit):
        if events and events[-1].event_type == terminal:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"terminal event {terminal} was not emitted")


class MammouthCliAgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_requires_explicit_executor(self) -> None:
        adapter = MammouthCliAgentRuntimeAdapter(probe=_Probe())

        with self.assertRaisesRegex(
            MammouthCliRuntimeError,
            "injected sandbox executor",
        ):
            await adapter.start_turn(
                "logical-session",
                AgentRuntimeTurnRequest(message="Do work"),
            )

    async def test_create_uses_stable_logical_session_and_command_binding(self) -> None:
        probe = _Probe()
        executor = _Executor(
            [
                '{"sessionID":"native-1","type":"step_start",'
                '"part":{"messageID":"message-1"}}',
                '{"type":"text","part":{"messageID":"message-1","text":"hello"}}',
                '{"type":"step_finish","part":{"messageID":"message-1"}}',
            ]
        )
        adapter = MammouthCliAgentRuntimeAdapter(probe=probe, run_command=executor)
        events = []
        adapter.subscribe_events(events.append)
        created = await adapter.create_session(
            AgentRuntimeSessionRequest(
                project_id="project-a",
                assignment_id="assignment-a",
                execution_id="execution-a",
                execution_workspace_id="workspace-a",
                worker_id="worker-a",
                workspace_cwd="/workspace/one",
                model="mammouth-recommended",
            )
        )
        canonical_id = created.provider_native_session_id

        started = await adapter.start_turn(
            canonical_id,
            AgentRuntimeTurnRequest(
                message="Do work",
                workspace_cwd="/workspace/one",
            ),
        )
        await _pump_until_terminal(events)

        self.assertEqual(started.provider_native_session_id, canonical_id)
        command, binding, timeout = executor.calls[0]
        self.assertIsInstance(command, CliRuntimeCommand)
        self.assertEqual(command.argv[0], "/worker/bin/mammouth")
        self.assertEqual(
            command.argv[1:6],
            ("run", "--format", "json", "--dir", "/workspace/one"),
        )
        self.assertEqual(command.argv[-1], "Do work")
        self.assertEqual(binding.canonical_session_id, canonical_id)
        self.assertEqual(binding.assignment_id, "assignment-a")
        self.assertEqual(binding.execution_workspace_id, "workspace-a")
        self.assertEqual(timeout, 1800.0)
        self.assertEqual(
            [event.event_type for event in events],
            [
                "turn/started",
                "item/started",
                "item/agentMessage/delta",
                "item/completed",
                "turn/completed",
            ],
        )
        self.assertEqual(events[0].provider_native_turn_id, "message-1")
        self.assertEqual(events[2].payload["delta"], "hello")
        self.assertTrue(events[-1].payload["synthetic"])
        self.assertEqual(await adapter.health(), AgentRuntimeHealth.HEALTHY)
        self.assertGreaterEqual(probe.calls, 1)

    async def test_idle_exit_without_session_status_synthesizes_terminal_event(self) -> None:
        executor = _Executor(
            [
                '{"type":"step_start","sessionID":"native-2",'
                '"part":{"messageID":"message-2"}}'
            ]
        )
        adapter = MammouthCliAgentRuntimeAdapter(
            probe=_Probe(),
            run_command=executor,
        )
        events = []
        adapter.subscribe_events(events.append)
        created = await adapter.create_session(
            AgentRuntimeSessionRequest(project_id="project-a")
        )

        await adapter.start_turn(
            created.provider_native_session_id,
            AgentRuntimeTurnRequest(message="No terminal in stream"),
        )
        await _pump_until_terminal(events)

        self.assertEqual(events[-1].event_type, "turn/completed")
        self.assertTrue(events[-1].payload["synthetic"])

    async def test_resume_uses_only_explicit_native_session_flag(self) -> None:
        executor = _Executor(
            [
                '{"type":"step_start","sessionID":"native-existing",'
                '"part":{"messageID":"message-2"}}',
                '{"type":"step_finish","part":{"messageID":"message-2"}}',
            ]
        )
        adapter = MammouthCliAgentRuntimeAdapter(
            probe=_Probe(),
            run_command=executor,
        )
        events = []
        adapter.subscribe_events(events.append)
        created = await adapter.create_session(
            AgentRuntimeSessionRequest(project_id="project-a")
        )
        await adapter.start_turn(
            created.provider_native_session_id,
            AgentRuntimeTurnRequest(
                message="Seed",
                workspace_cwd="/worker/project",
            ),
        )
        await _pump_until_terminal(events)

        resumed = await adapter.resume_session(
            "native-existing",
            AgentRuntimeSessionRequest(
                project_id="project-a",
                assignment_id="assignment-b",
                workspace_cwd="/worker/project",
            ),
        )

        await adapter.start_turn(
            resumed.provider_native_session_id,
            AgentRuntimeTurnRequest(
                message="Continue",
                workspace_cwd="/worker/project",
            ),
        )
        await asyncio.sleep(0)

        argv = executor.calls[-1][0].argv
        self.assertIn("--session", argv)
        self.assertEqual(argv[argv.index("--session") + 1], "native-existing")
        self.assertNotIn("--continue", argv)
        self.assertEqual(argv[-1], "Continue")
        self.assertEqual(executor.calls[-1][1].assignment_id, "assignment-b")

    async def test_resume_of_unknown_session_starts_fresh(self) -> None:
        executor = _Executor(
            [
                '{"type":"step_start","sessionID":"native-fresh",'
                '"part":{"messageID":"message-3"}}',
                '{"type":"step_finish","part":{"messageID":"message-3"}}',
            ]
        )
        adapter = MammouthCliAgentRuntimeAdapter(
            probe=_Probe(),
            run_command=executor,
        )
        resumed = await adapter.resume_session(
            "canonical-thread-id",
            AgentRuntimeSessionRequest(
                project_id="project-a",
                assignment_id="assignment-c",
                workspace_cwd="/worker/project",
            ),
        )
        self.assertEqual(resumed.payload["provider_native_session_id"], None)

        await adapter.start_turn(
            resumed.provider_native_session_id,
            AgentRuntimeTurnRequest(
                message="Start over",
                workspace_cwd="/worker/project",
            ),
        )
        await asyncio.sleep(0)

        argv = executor.calls[0][0].argv
        self.assertNotIn("--session", argv)
        self.assertEqual(argv[-1], "Start over")

    async def test_unready_runtime_does_not_invoke_executor(self) -> None:
        executor = _Executor()
        adapter = MammouthCliAgentRuntimeAdapter(
            probe=_Probe(ready=False),
            run_command=executor,
        )
        created = await adapter.create_session(
            AgentRuntimeSessionRequest(project_id="project-a")
        )

        with self.assertRaisesRegex(MammouthCliRuntimeError, "not execution-ready"):
            await adapter.start_turn(
                created.provider_native_session_id,
                AgentRuntimeTurnRequest(message="Do not start"),
            )
        self.assertEqual(executor.calls, [])

    async def test_errors_and_credentials_are_not_exposed(self) -> None:
        executor = _Executor(
            [
                '{"type":"step_start","sessionID":"native-3",'
                '"part":{"messageID":"message-3","api_key":"secret-value",'
                '"nested":{"authorization":"Bearer hidden"}}}'
            ],
            exit_code=9,
        )
        adapter = MammouthCliAgentRuntimeAdapter(
            probe=_Probe(),
            run_command=executor,
        )
        events = []
        adapter.subscribe_events(events.append)
        created = await adapter.create_session(
            AgentRuntimeSessionRequest(project_id="project-a")
        )

        await adapter.start_turn(
            created.provider_native_session_id,
            AgentRuntimeTurnRequest(message="Fail"),
        )
        await _pump_until_terminal(events, terminal="turn/failed")

        self.assertNotIn("api_key", repr(events))
        self.assertNotIn("authorization", repr(events))
        self.assertNotIn("secret-value", repr(events))
        self.assertNotIn("hidden", repr(events))
        self.assertEqual(events[-1].event_type, "turn/failed")
        self.assertNotIn("stderr", repr(events))

    async def test_start_waits_for_session_event_and_keeps_fast_turn_active(self) -> None:
        class _GatedExecutor:
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def __call__(
                self,
                command,
                *,
                binding,
                on_output,
                timeout_seconds,
            ):
                self.started.set()
                await self.release.wait()
                await on_output(
                    CliRuntimeOutput(
                        stream="stdout",
                        text=(
                            '{"type":"step_start","sessionID":"native-fast",'
                            '"part":{"messageID":"message-fast"}}'
                        ),
                    )
                )
                await on_output(
                    CliRuntimeOutput(
                        stream="stdout",
                        text=(
                            '{"type":"step_finish",'
                            '"part":{"messageID":"message-fast"}}'
                        ),
                    )
                )
                return CliRuntimeResult(exit_code=0)

        executor = _GatedExecutor()
        adapter = MammouthCliAgentRuntimeAdapter(
            probe=_Probe(),
            run_command=executor,
        )
        events = []
        adapter.subscribe_events(events.append)
        created = await adapter.create_session(
            AgentRuntimeSessionRequest(project_id="project-a")
        )
        start_task = asyncio.create_task(
            adapter.start_turn(
                created.provider_native_session_id,
                AgentRuntimeTurnRequest(message="Fast completion"),
            )
        )
        await executor.started.wait()
        await asyncio.sleep(0)
        self.assertFalse(start_task.done())
        self.assertEqual(events, [])

        executor.release.set()
        result = await start_task

        self.assertEqual(
            result.provider_native_session_id,
            created.provider_native_session_id,
        )
        self.assertEqual(events[0].event_type, "turn/started")
        self.assertIn(created.provider_native_session_id, adapter._active_tasks)
        for _ in range(5):
            if events and events[-1].event_type == "turn/completed":
                break
            await asyncio.sleep(0)
        self.assertEqual(events[-1].event_type, "turn/completed")

    async def test_exit_before_json_session_handshake_fails_start(self) -> None:
        adapter = MammouthCliAgentRuntimeAdapter(
            probe=_Probe(),
            run_command=_Executor(exit_code=0),
        )
        created = await adapter.create_session(
            AgentRuntimeSessionRequest(project_id="project-a")
        )

        with self.assertRaisesRegex(MammouthCliRuntimeError, "before emitting"):
            await adapter.start_turn(
                created.provider_native_session_id,
                AgentRuntimeTurnRequest(message="No JSON event"),
            )
        self.assertEqual(adapter._active_tasks, {})

    async def test_executor_exceptions_are_safe_failure_events(self) -> None:
        async def fail(command, *, binding, on_output, timeout_seconds):
            await on_output(
                CliRuntimeOutput(
                    stream="stdout",
                    text=(
                        '{"type":"step_start","sessionID":"native-exception",'
                        '"part":{"messageID":"message-exception"}}'
                    ),
                )
            )
            raise RuntimeError("MAMMOUTH_API_KEY=hidden provider detail")

        adapter = MammouthCliAgentRuntimeAdapter(
            probe=_Probe(),
            run_command=fail,
        )
        events = []
        adapter.subscribe_events(events.append)
        created = await adapter.create_session(
            AgentRuntimeSessionRequest(project_id="project-a")
        )

        await adapter.start_turn(
            created.provider_native_session_id,
            AgentRuntimeTurnRequest(message="Fail safely"),
        )
        await _pump_until_terminal(events, terminal="turn/failed")

        self.assertEqual(events[-1].event_type, "turn/failed")
        self.assertNotIn("API_KEY", repr(events))
        self.assertNotIn("hidden", repr(events))
        self.assertNotIn("hidden provider detail", repr(events))
        self.assertNotIn("RuntimeError", repr(events[-1].payload))

    async def test_interrupt_cancels_injected_executor(self) -> None:
        executor = _BlockingExecutor()
        adapter = MammouthCliAgentRuntimeAdapter(
            probe=_Probe(),
            run_command=executor,
        )
        events = []
        adapter.subscribe_events(events.append)
        created = await adapter.create_session(
            AgentRuntimeSessionRequest(
                project_id="project-a",
                assignment_id="assignment-c",
            )
        )
        await adapter.start_turn(
            created.provider_native_session_id,
            AgentRuntimeTurnRequest(message="Long-running"),
        )
        await executor.started.wait()

        result = await adapter.interrupt(created.provider_native_session_id)

        self.assertTrue(result.payload["interrupted"])
        self.assertTrue(executor.cancelled)
        self.assertEqual(events[-1].event_type, "turn/interrupted")
        self.assertEqual(executor.binding.assignment_id, "assignment-c")

    async def test_provider_error_event_is_safe_failure(self) -> None:
        executor = _Executor(
            [
                '{"type":"error","sessionID":"native-error",'
                '"part":{"messageID":"message-error","error":"API_KEY=hidden"}}'
            ]
        )
        adapter = MammouthCliAgentRuntimeAdapter(
            probe=_Probe(),
            run_command=executor,
        )
        events = []
        adapter.subscribe_events(events.append)
        resumed = await adapter.resume_session(
            "native-error",
            AgentRuntimeSessionRequest(project_id="project-a"),
        )

        await adapter.start_turn(
            resumed.provider_native_session_id,
            AgentRuntimeTurnRequest(message="Error"),
        )
        await _pump_until_terminal(events, terminal="turn/failed")

        self.assertEqual(events[-1].event_type, "turn/failed")
        self.assertNotIn("API_KEY", repr(events))
        self.assertNotIn("hidden", repr(events))

    async def test_health_fails_closed_without_probe_or_executor(self) -> None:
        adapter = MammouthCliAgentRuntimeAdapter()
        self.assertEqual(await adapter.health(), AgentRuntimeHealth.UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
