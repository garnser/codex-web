from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.cli_runtime import CliRuntimeCommand
from codex_web.services.mammouth_assignment_runner import (
    MammouthAssignmentCliRunner,
    MammouthAssignmentRunnerError,
)
from codex_web.services.mammouth_auth_delegation import (
    MammouthAuthDelegation,
    MammouthDelegatedLaunch,
)


class _Process:
    def __init__(self, *, exit_code=0) -> None:
        self.exit_code = exit_code
        self.stdout = io.StringIO('{"type":"turn.started","sessionID":"native-1"}\n')
        self.stderr = io.StringIO("")
        self.terminated = False

    def poll(self):
        return self.exit_code

    def wait(self):
        return self.exit_code


class _BlockingProcess(_Process):
    def __init__(self) -> None:
        super().__init__(exit_code=None)

    def wait(self):
        return -15 if self.terminated else 0


class _Backend:
    def __init__(self, process) -> None:
        self.process = process
        self.spawned = []
        self.terminated = []

    def spawn_interactive(self, assignment, **kwargs):
        self.spawned.append((assignment, kwargs))
        return self.process

    def terminate_process(self, process):
        process.terminated = True
        process.exit_code = -15
        self.terminated.append(process)


class _LocalWorker:
    def __init__(self, backend) -> None:
        self.backend = backend
        self.worker_actor = object()

    def repository_mounts(self, assignment):
        del assignment
        return ((Path("/host/ro"), Path("/mnt/ro")),), ()


class _Session:
    def __init__(self, workspace: Path, backend) -> None:
        self.workspace_path = workspace
        self.fence = 4
        self.worker_id = "worker-1"
        self.local_worker = _LocalWorker(backend)
        self.assignment = SimpleNamespace(id="assignment-1")
        self.validation_calls = 0

    def validate_current(self):
        self.validation_calls += 1
        return self.assignment


class _CredentialProvider:
    def __init__(self, *, fail_validation=False) -> None:
        self.fail_validation = fail_validation
        self.validations = 0
        self.actor_seen = None
        self.delegation = MammouthAuthDelegation(
            assignment_id="assignment-1",
            worker_id="worker-1",
            fence=4,
            secret_id="secret-1",
            secret_rotation=2,
            issued_at=10.0,
            expires_at=100.0,
        )

    def use(self, assignment, *, worker_id, fence, actor, consumer):
        self.actor_seen = actor
        self.assignment_seen = assignment
        self.worker_id_seen = worker_id
        self.fence_seen = fence
        return consumer(
            MammouthDelegatedLaunch(
                delegation=self.delegation,
                command=("mammouth",),
                environment={
                    "HOME": "/tmp/mammouth-worker-home",
                    "MAMMOUTH_API_KEY": "ephemeral-secret",
                },
            )
        )

    def validate_current(self, delegation, assignment, *, actor):
        del delegation, assignment, actor
        self.validations += 1
        if self.fail_validation:
            raise RuntimeError("delegation stale")


class MammouthAssignmentCliRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_spawn_uses_canonical_workspace_mounts_and_delegated_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            process = _Process()
            backend = _Backend(process)
            session = _Session(workspace, backend)
            credentials = _CredentialProvider()
            runner = MammouthAssignmentCliRunner(session, credentials)
            outputs = []

            result = await runner.run(
                CliRuntimeCommand(
                    argv=("mammouth", "run", "--format", "json", "hello"),
                    cwd=workspace,
                    environment={
                        "IGNORED_SAFE_SETTING": "value",
                        "MAMMOUTH_API_KEY": "must-not-win",
                    },
                ),
                on_output=outputs.append,
            )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(len(backend.spawned), 1)
            assignment, kwargs = backend.spawned[0]
            self.assertIs(assignment, session.assignment)
            self.assertEqual(kwargs["workspace_path"], workspace.resolve())
            self.assertEqual(
                kwargs["trusted_readonly_mounts"],
                ((Path("/host/ro"), Path("/mnt/ro")),),
            )
            self.assertEqual(
                kwargs["environment"]["MAMMOUTH_API_KEY"],
                "ephemeral-secret",
            )
            self.assertEqual(
                kwargs["environment"]["HOME"],
                "/tmp/mammouth-worker-home",
            )
            self.assertEqual(
                kwargs["environment"]["IGNORED_SAFE_SETTING"],
                "value",
            )
            self.assertEqual(outputs[0].stream, "stdout")
            self.assertIn("native-1", outputs[0].text)

    async def test_wrong_cwd_fails_before_secret_resolution_or_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as other:
            workspace = Path(temp_dir)
            backend = _Backend(_Process())
            session = _Session(workspace, backend)
            credentials = _CredentialProvider()
            runner = MammouthAssignmentCliRunner(session, credentials)

            with self.assertRaisesRegex(
                MammouthAssignmentRunnerError,
                "does not match",
            ):
                await runner.run(
                    CliRuntimeCommand(
                        argv=("mammouth", "run"),
                        cwd=Path(other),
                        environment={},
                    )
                )

            self.assertEqual(backend.spawned, [])
            self.assertFalse(hasattr(credentials, "assignment_seen"))

    async def test_stale_delegation_terminates_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            process = _BlockingProcess()
            backend = _Backend(process)
            session = _Session(workspace, backend)
            credentials = _CredentialProvider(fail_validation=True)
            runner = MammouthAssignmentCliRunner(
                session,
                credentials,
                poll_interval_seconds=0.01,
            )

            with self.assertRaisesRegex(RuntimeError, "delegation stale"):
                await runner.run(
                    CliRuntimeCommand(
                        argv=("mammouth", "run"),
                        cwd=workspace,
                        environment={},
                    )
                )

            self.assertTrue(process.terminated)
            self.assertEqual(backend.terminated, [process])

    async def test_cancellation_terminates_owned_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            process = _BlockingProcess()
            backend = _Backend(process)
            session = _Session(workspace, backend)
            credentials = _CredentialProvider()
            runner = MammouthAssignmentCliRunner(
                session,
                credentials,
                poll_interval_seconds=0.01,
            )

            task = asyncio.create_task(
                runner.run(
                    CliRuntimeCommand(
                        argv=("mammouth", "run"),
                        cwd=workspace,
                        environment={},
                    )
                )
            )
            for _ in range(100):
                if backend.spawned:
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(backend.spawned)

            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            self.assertTrue(process.terminated)
            self.assertEqual(backend.terminated, [process])


if __name__ == "__main__":
    unittest.main()
