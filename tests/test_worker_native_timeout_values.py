from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.cli_runtime import CliRuntimeCommand, CliRuntimeRunner
from codex_web.execution_workers import WorkerResourceLimits, ExecutionWorkerState
from codex_web.procedure_execution import ProcedureCheckpoint
from codex_web.storage.execution_workers import ExecutionWorkerStore
from codex_web.local_execution_backend import BubblewrapExecutionBackend
from codex_web.runtime.codex import request_timeout
from codex_web.services.agent_process_session import (
    AssignmentBoundAgentProcessSession,
)
from codex_web.services.codex_cli_agent_runtime import CodexCliAgentRuntimeAdapter
from codex_web.services.mammouth_agent_runtime import MammouthCliAgentRuntimeAdapter
from tests.test_cli_runtime_runner import _Process
from tests.test_local_execution_worker import _assignment
from tests import test_turn_execution_binding as binding_fixture


class _PolledProcess:
    def __init__(self):
        self.polls = 0
        self.killed = False
        self.joined = False

    def poll(self):
        self.polls += 1
        return -9 if self.killed else (0 if self.polls > 2 else None)

    def wait(self):
        self.joined = True
        return -9 if self.killed else 0


class WorkerTimeoutValuesTests(unittest.TestCase):
    def test_new_admission_records_default_wall_but_preserves_explicit_limits_and_authority_ttl(self):
        for bootstrap in (False, True):
            fixture = binding_fixture.TurnExecutionBindingTests(methodName="runTest")
            fixture.setUp()
            try:
                fixture._publish_secret()
                for index, (configured, explicit, expected) in enumerate(
                        (("0", None, 0), ("-1", None, 0), ("0.25", None, 1),
                         ("0", WorkerResourceLimits(cpu_seconds=31, wall_seconds=5), 5))):
                    with self.subTest(bootstrap=bootstrap, configured=configured, explicit=explicit), \
                            patch.dict(os.environ, {"CODEX_WEB_WORKER_WALL_TIMEOUT_SECONDS": configured}):
                        values = dict(execution_id=f"wall-default-{bootstrap}-{index}",
                            project_id=fixture.project.id, sandbox="workspace-write", approval_policy="on-request",
                            limits=explicit)
                        if bootstrap:
                            binding = fixture.service.prepare_bootstrap(
                                bootstrap_id=f"wall-bootstrap-{index}", session_seconds=60, **values)
                        else:
                            binding = fixture.service.prepare(
                                thread_id=f"wall-thread-{index}", deadline_seconds=60, **values)
                        assignment = fixture.workers.store.assignment(binding.assignment_id)
                        self.assertEqual(assignment.limits.wall_seconds, expected)
                        self.assertEqual(binding.deadline_at, fixture.clock + 60)
                        self.assertEqual(assignment.deadline_at, fixture.clock + 60)
                        if explicit is not None:
                            self.assertEqual(assignment.limits, explicit)
                        elif bootstrap:
                            self.assertEqual(assignment.limits.cpu_seconds, 60)
                        # Changing the deployment default never changes admitted
                        # values, which are the UI-visible canonical limits.
                        with patch.dict(os.environ, {"CODEX_WEB_WORKER_WALL_TIMEOUT_SECONDS": "999"}):
                            self.assertEqual(fixture.workers.store.assignment(binding.assignment_id).limits.wall_seconds,
                                             expected)
            finally:
                fixture.tearDown()

    def test_new_state_version_only_for_new_disabled_formats(self):
        store = object.__new__(ExecutionWorkerStore)
        for version in ("1.14", "1.15"):
            state = ExecutionWorkerState(schema_version=version, assignments=[_assignment()])
            encoded = state.model_dump_json()
            self.assertEqual(state.schema_version, version)
            self.assertEqual(store._decode(state.model_dump(mode="json")).model_dump_json(), encoded)
        for wall in (0, -1):
            state = ExecutionWorkerState(assignments=[_assignment(limits=WorkerResourceLimits(wall_seconds=wall))])
            self.assertEqual(state.schema_version, "1.16")
            raw = state.model_dump(mode="json")
            self.assertEqual(raw["schema_version"], "1.16")
            self.assertEqual(store._decode(raw).model_dump(mode="json"), raw)

    def test_disabled_checkpoint_current_and_history_require_new_state_format(self):
        checkpoint = ProcedureCheckpoint(
            schema_version="1.2", id="procedure-run-" + "a" * 32,
            canonical_input_checksum="b" * 64,
            definition={"definition_id": "procedure", "kind": "operational_procedure", "revision": 1,
                        "record_id": "procedure-record", "checksum": "c" * 64,
                        "definition_schema_version": "1.5"},
            actor_identity_id="actor", input_snapshot_id="snapshot", input_snapshot_checksum="d" * 64,
            resource_context_checksum="e" * 64, started_at=100, deadline_at=None, steps={})
        for historical in (False, True):
            current = checkpoint.model_copy(update={"schema_version": "1.1", "id": "procedure-run-" + "f" * 32,
                                                    "deadline_at": 200}) if historical else checkpoint
            assignment = _assignment(procedure_checkpoint=current,
                procedure_history=(checkpoint.model_copy(update={"status": "completed"}),) if historical else ())
            state = ExecutionWorkerState(assignments=[assignment])
            self.assertEqual(state.schema_version, "1.16")
            encoded = state.model_dump_json()
            self.assertEqual(ExecutionWorkerState.model_validate_json(encoded).model_dump_json(), encoded)

    def run_backend(self, wall, *, override=None, operation=None, refuse=False):
        process = _PolledProcess()
        clock = [0.0]
        backend = BubblewrapExecutionBackend(popen=lambda *a, **k: process)
        assignment = SimpleNamespace(limits=WorkerResourceLimits(wall_seconds=wall))
        checks = []

        def poll():
            checks.append(True)
            if refuse and len(checks) == 2:
                raise RuntimeError("actual current authority refused")

        def sleep(_seconds):
            # Simulate a very old server doing work; never spend real wall time.
            clock[0] += 1000

        def kill(candidate):
            self.assertIs(candidate, process)
            candidate.killed = True

        environment = {} if override is None else {
            "CODEX_WEB_WORKER_WALL_TIMEOUT_SECONDS": str(override)}
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, environment, clear=True), \
                patch.object(backend, "probe", return_value=SimpleNamespace(ready=True)), \
                patch.object(backend, "build_command", return_value=("/bin/true",)), \
                patch.object(backend, "discover_git_metadata", return_value=None), \
                patch.object(backend, "_execution_disk_usage", return_value=0), \
                patch.object(backend, "_kill_process_group", side_effect=kill), \
                patch("codex_web.local_execution_backend.time.monotonic", side_effect=lambda: clock[0]), \
                patch("codex_web.local_execution_backend.time.sleep", side_effect=sleep):
            if refuse:
                with self.assertRaisesRegex(RuntimeError, "authority refused"):
                    backend.run(assignment, argv=("/bin/true",), workspace_path=Path(directory),
                                poll_hook=poll, operation_timeout_seconds=operation)
                return process
            result = backend.run(assignment, argv=("/bin/true",), workspace_path=Path(directory),
                                 poll_hook=poll, operation_timeout_seconds=operation)
        self.assertTrue(process.joined)
        return result, process

    def test_worker_wall_sentinels_survive_elapsed_time_without_kill(self):
        for value in (0, -1):
            with self.subTest(value=value):
                self.assertEqual(WorkerResourceLimits(wall_seconds=value).wall_seconds, value)
                result, process = self.run_backend(value)
                self.assertTrue(result.succeeded)
                self.assertFalse(process.killed)
                self.assertGreaterEqual(result.duration_seconds, 1000)

    def test_positive_wall_is_still_enforced_and_joined(self):
        result, process = self.run_backend(5)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.limit_breach, "wall_seconds")
        self.assertTrue(process.killed)

    def test_disabled_default_cannot_erase_explicit_admitted_wall_or_operation_cap(self):
        for value in (0, -1):
            with self.subTest(value=value):
                result, process = self.run_backend(5, override=value, operation=10)
                self.assertTrue(result.timed_out)
                self.assertTrue(process.killed)
                result, process = self.run_backend(5, override=value, operation=value)
                self.assertTrue(result.timed_out)
                self.assertTrue(process.killed)
                result, process = self.run_backend(0, override=value, operation=10)
                self.assertTrue(result.timed_out)
                self.assertTrue(process.killed)
                result, process = self.run_backend(0, override=value, operation=value)
                self.assertTrue(result.succeeded)
                self.assertFalse(process.killed)

    def test_disabled_duration_still_retires_and_joins_on_authority_refusal(self):
        process = self.run_backend(0, operation=-1, refuse=True)
        self.assertTrue(process.killed)
        self.assertTrue(process.joined)

    def test_rpc_and_cli_start_and_turn_use_same_disabled_setting(self):
        for value in (0, -1):
            with self.subTest(value=value), patch.dict(os.environ, {
                "CODEX_WEB_NATIVE_RPC_TIMEOUT_SECONDS": str(value)}):
                for method in ("initialize", "thread/start", "thread/read", "turn/interrupt", "other"):
                    self.assertIsNone(request_timeout(method))
                for adapter_type in (CodexCliAgentRuntimeAdapter, MammouthCliAgentRuntimeAdapter):
                    adapter = adapter_type()
                    self.assertIsNone(adapter.timeout_seconds)
                    self.assertIsNone(adapter.start_timeout_seconds)

    def test_positive_rpc_override_remains_enforceable(self):
        with patch.dict(os.environ, {"CODEX_WEB_NATIVE_RPC_TIMEOUT_SECONDS": "7"}):
            self.assertEqual(request_timeout("turn/start"), 7)

    def test_disabled_preparation_cap_keeps_actual_authority_expiry(self):
        session = object.__new__(AssignmentBoundAgentProcessSession)
        session._clock = lambda: 100
        assignment = SimpleNamespace(lease=SimpleNamespace(expires_at=150), deadline_at=125)
        with patch.dict(os.environ, {"CODEX_WEB_LOCAL_PREPARATION_TIMEOUT_SECONDS": "0"}):
            self.assertEqual(session._assignment_lease_budget(assignment), 25)
        with patch.dict(os.environ, {"CODEX_WEB_LOCAL_PREPARATION_TIMEOUT_SECONDS": "5"}):
            self.assertEqual(session._assignment_lease_budget(assignment), 5)


class CliSentinelCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_sentinels_wait_until_explicit_cancel_and_retire_process(self):
        for value in (0, -1):
            process = _Process(block=True)
            terminated = []

            async def create(*_args, **_kwargs):
                return process

            async def terminate(candidate):
                terminated.append(candidate)
                candidate.returncode = -15

            runner = CliRuntimeRunner(create_subprocess=create, terminate_process_tree=terminate)
            command = CliRuntimeCommand(argv=("/bin/example",), cwd=Path("/unused"), environment={})
            # The original wait_for remains real; inspection distinguishes off
            # from the old positive clamp without a response-time assertion.
            with patch("codex_web.cli_runtime.asyncio.wait_for", wraps=asyncio.wait_for) as timer:
                task = asyncio.create_task(runner.run(command, timeout_seconds=value))
                try:
                    await process.wait_started.wait()
                    self.assertFalse(task.done())
                    self.assertEqual(timer.call_count, 0)
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
            self.assertEqual(terminated, [process])
