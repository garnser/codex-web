from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codex_web.cli_runtime import CliRuntimeCommand, CliRuntimeOutput
from codex_web.cli_runtime import CliRuntimeTimeoutError
from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.services.agent_model_egress import (
    AGENT_MODEL_EGRESS_RELAY_SCRIPT,
    AgentRuntimeModelEgressEndpoint,
)
from codex_web.services.mammouth_worker_session import (
    MAMMOUTH_SANDBOX_WORKER_HOME,
    AssignmentBoundMammouthSessionManager,
    MammouthCliSandboxTurnExecutor,
    MammouthTurnExecutorError,
    MammouthWorkerHomeRegistry,
)


class FakeStdReader:
    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    def readline(self) -> str:
        if self._lines:
            return self._lines.pop(0)
        return ""

    def read(self, size: int) -> str:
        return ""


class FakeProcess:
    def __init__(
        self,
        lines: list[str],
        *,
        stderr_lines: list[str] | None = None,
        exit_code: int = 0,
        block_until_terminate: bool = False,
    ) -> None:
        self.stdout = FakeStdReader(lines)
        self.stderr = FakeStdReader(stderr_lines or [])
        self.exit_code = exit_code
        self.block_until_terminate = block_until_terminate
        self.terminated = threading.Event()
        self.exited = threading.Event()

    def wait(self) -> int:
        if self.block_until_terminate:
            self.terminated.wait(timeout=5)
        self.exited.set()
        return -9 if self.terminated.is_set() else self.exit_code

    def poll(self):
        return self.exit_code if self.exited.is_set() else None


class FakeBackend:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.calls: list[dict] = []

    def spawn_interactive(self, assignment, **kwargs):
        self.calls.append(kwargs)
        return self.process

    def terminate_process(self, process) -> None:
        process.terminated.set()


class FakeLocalWorker:
    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend
        self.worker_actor = SimpleNamespace(identity_id="worker-actor")

    def repository_mounts(self, assignment):
        return ((), ())


class FakeSession:
    def __init__(self, assignment, *, workspace_path="/workspace/repo") -> None:
        self.assignment = assignment
        self.workspace_path = workspace_path
        self.worker_id = "worker-a"
        self.stopped = asyncio.Event()

    def validate_current(self):
        return self.assignment

    async def stop(self) -> None:
        self.stopped.set()


class FakeSessionManager:
    def __init__(self, session: FakeSession | None) -> None:
        self.session = session

    def get(self, assignment_id):
        return self.session


class FakeDelegation:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def use(self, assignment, *, worker_id, fence, actor, consumer):
        self.calls.append(
            {
                "worker_id": worker_id,
                "fence": fence,
            }
        )
        launch = SimpleNamespace(
            delegation=SimpleNamespace(assignment_id=assignment.id),
            command=("mammouth",),
            environment={
                "HOME": "/tmp/mammouth-worker-home",
                "MAMMOUTH_API_KEY": "delegated-key",
            },
        )
        return consumer(launch)


class MammouthWorkerHomeRegistryTests(unittest.TestCase):
    def test_homes_are_reused_per_assignment_and_removed_on_discard(self) -> None:
        registry = MammouthWorkerHomeRegistry()
        first = registry.home_for("assignment-a")
        second = registry.home_for("assignment-a")
        self.assertEqual(first, second)
        self.assertTrue(first.is_dir())
        registry.discard("assignment-a")
        self.assertFalse(first.exists())
        replacement = registry.home_for("assignment-a")
        self.assertNotEqual(replacement, first)
        registry.discard("assignment-a")


def _assignment():
    return SimpleNamespace(
        id="assignment-a",
        lease=SimpleNamespace(fence=3),
        deadline_at=None,
    )


def _command():
    return CliRuntimeCommand(
        argv=("/worker/bin/mammouth", "run", "--format", "json"),
        cwd=Path("/workspace/repo"),
        environment={},
    )


class _RecordingOutput:
    def __init__(self) -> None:
        self.outputs: list[CliRuntimeOutput] = []

    async def __call__(self, output: CliRuntimeOutput) -> None:
        self.outputs.append(output)


class MammouthCliSandboxTurnExecutorTests(unittest.IsolatedAsyncioTestCase):
    def _executor(
        self,
        *,
        session: FakeSession | None = None,
        no_session: bool = False,
        process: FakeProcess | None = None,
    ) -> tuple[MammouthCliSandboxTurnExecutor, FakeDelegation, FakeBackend]:
        backend = FakeBackend(process or FakeProcess([]))
        local_worker = FakeLocalWorker(backend)
        delegation = FakeDelegation()
        manager = FakeSessionManager(None if no_session else (session or FakeSession(_assignment())))
        executor = MammouthCliSandboxTurnExecutor(
            local_worker,
            session_manager=manager,
            delegation=delegation,
            worker_homes=MammouthWorkerHomeRegistry(),
            egress_endpoints_resolver=lambda: (
                AgentRuntimeModelEgressEndpoint("api.mammouth.ai", 443),
            ),
        )
        return executor, delegation, backend

    async def test_launches_relay_inside_sandbox_with_delegated_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as bin_dir:
            executable = Path(bin_dir) / "mammouth"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            process = FakeProcess(
                [
                    '{"sessionID":"native-1","type":"step_start",'
                    '"part":{"messageID":"message-1"}}',
                    "",
                ]
            )
            executor, delegation, backend = self._executor(process=process)
            recorder = _RecordingOutput()
            command = CliRuntimeCommand(
                argv=(str(executable), "run", "--format", "json"),
                cwd=Path("/workspace/repo"),
                environment={},
            )

            result = await executor(
                command,
                binding=SimpleNamespace(assignment_id="assignment-a"),
                on_output=recorder,
                timeout_seconds=10,
            )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(delegation.calls[0]["fence"], 3)
            spawn = backend.calls[0]
            argv = spawn["argv"]
            self.assertEqual(argv[:3], ("/usr/bin/python3", "-u", "-c"))
            self.assertEqual(argv[3], AGENT_MODEL_EGRESS_RELAY_SCRIPT)
            self.assertEqual(argv[6:], command.argv)
            environment = spawn["environment"]
            self.assertEqual(environment["MAMMOUTH_API_KEY"], "delegated-key")
            self.assertIn("HTTP_PROXY", environment)
            self.assertIn("HTTPS_PROXY", environment)
            readonly = spawn["trusted_readonly_mounts"]
            self.assertIn((Path(bin_dir), Path(bin_dir)), readonly)
            writable = spawn["trusted_writable_mounts"]
            self.assertIn(
                (writable[-1][0], MAMMOUTH_SANDBOX_WORKER_HOME),
                writable,
            )
            self.assertEqual(
                [output.text for output in recorder.outputs],
                [
                    '{"sessionID":"native-1","type":"step_start",'
                    '"part":{"messageID":"message-1"}}'
                ],
            )

    async def test_non_zero_exit_code_is_returned(self) -> None:
        process = FakeProcess(['{"sessionID":"n","type":"error","part":{"messageID":"m"}}'], exit_code=7)
        executor, _delegation, _backend = self._executor(process=process)
        recorder = _RecordingOutput()

        result = await executor(
            _command(),
            binding=SimpleNamespace(assignment_id="assignment-a"),
            on_output=recorder,
            timeout_seconds=10,
        )

        self.assertEqual(result.exit_code, 7)

    async def test_stderr_is_forwarded_to_runtime_adapter(self) -> None:
        process = FakeProcess([], stderr_lines=["credits exhausted", ""])
        executor, _delegation, _backend = self._executor(process=process)
        recorder = _RecordingOutput()

        await executor(
            _command(),
            binding=SimpleNamespace(assignment_id="assignment-a"),
            on_output=recorder,
            timeout_seconds=10,
        )

        self.assertEqual(
            [(output.stream, output.text) for output in recorder.outputs],
            [("stderr", "credits exhausted")],
        )

    async def test_timeout_terminates_process_and_fails(self) -> None:
        process = FakeProcess([], block_until_terminate=True)
        executor, delegation, backend = self._executor(process=process)
        recorder = _RecordingOutput()

        with self.assertRaises(CliRuntimeTimeoutError):
            await executor(
                _command(),
                binding=SimpleNamespace(assignment_id="assignment-a"),
                on_output=recorder,
                timeout_seconds=0.05,
            )

        self.assertTrue(process.terminated.is_set())
        self.assertEqual(delegation.calls, [{"worker_id": "worker-a", "fence": 3}])

    async def test_cancellation_terminates_process(self) -> None:
        process = FakeProcess([], block_until_terminate=True)
        executor, _delegation, _backend = self._executor(process=process)
        recorder = _RecordingOutput()

        task = asyncio.ensure_future(
            executor(
                _command(),
                binding=SimpleNamespace(assignment_id="assignment-a"),
                on_output=recorder,
                timeout_seconds=30,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(process.terminated.is_set())

    async def test_unclaimed_session_fails_closed_without_launch(self) -> None:
        executor, delegation, backend = self._executor(no_session=True)

        with self.assertRaisesRegex(MammouthTurnExecutorError, "not claimed"):
            await executor(
                _command(),
                binding=SimpleNamespace(assignment_id="assignment-a"),
                on_output=_RecordingOutput(),
                timeout_seconds=10,
            )

        self.assertEqual(delegation.calls, [])
        self.assertEqual(backend.calls, [])

    async def test_stale_assignment_fails_before_launch(self) -> None:
        class StaleSession(FakeSession):
            def validate_current(self):
                raise RuntimeError("lease/fence changed")

        executor, delegation, backend = self._executor(
            session=StaleSession(_assignment())
        )

        with self.assertRaisesRegex(RuntimeError, "lease/fence"):
            await executor(
                _command(),
                binding=SimpleNamespace(assignment_id="assignment-a"),
                on_output=_RecordingOutput(),
                timeout_seconds=10,
            )

        self.assertEqual(delegation.calls, [])
        self.assertEqual(backend.calls, [])

    async def test_missing_workspace_fails_closed(self) -> None:
        session = FakeSession(_assignment(), workspace_path=None)
        executor, delegation, backend = self._executor(session=session)

        with self.assertRaisesRegex(MammouthTurnExecutorError, "workspace"):
            await executor(
                _command(),
                binding=SimpleNamespace(assignment_id="assignment-a"),
                on_output=_RecordingOutput(),
                timeout_seconds=10,
            )

        self.assertEqual(delegation.calls, [])
        self.assertEqual(backend.calls, [])


class AssignmentBoundMammouthSessionManagerTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self) -> tuple[AssignmentBoundMammouthSessionManager, MammouthWorkerHomeRegistry]:
        registry = MammouthWorkerHomeRegistry()
        manager = AssignmentBoundMammouthSessionManager(
            SimpleNamespace(),
            runtime_binding=ExecutionRuntimeBinding(
                provider_id="mammouth-ai",
                runtime_id="mammouth-cli",
                capability_revision=1,
            ),
            worker_homes=registry,
        )
        return manager, registry

    async def test_home_discarded_on_stop(self) -> None:
        manager, registry = self._manager()
        session = FakeSession(_assignment())
        manager.sessions["assignment-a"] = session
        home = registry.home_for("assignment-a")
        self.assertTrue(home.is_dir())

        await manager.stop("assignment-a")

        self.assertFalse(home.exists())
        self.assertEqual(manager.sessions, {})

    async def test_home_discarded_on_complete(self) -> None:
        manager, registry = self._manager()
        home = registry.home_for("assignment-a")

        async def fake_complete(self, assignment_id, **kwargs):
            return SimpleNamespace(id=assignment_id)

        with mock.patch.object(
            AssignmentBoundMammouthSessionManager.__mro__[1],
            "complete",
            fake_complete,
        ):
            await manager.complete("assignment-a", succeeded=True)

        self.assertFalse(home.exists())

    async def test_home_discarded_on_stop_all(self) -> None:
        manager, registry = self._manager()
        session = FakeSession(_assignment())
        manager.sessions["assignment-a"] = session
        home = registry.home_for("assignment-a")

        await manager.stop_all()

        self.assertFalse(home.exists())


if __name__ == "__main__":
    unittest.main()
