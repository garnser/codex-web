from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.artifact_evidence import EvidenceType
from codex_web.execution_workers import (
    AssignmentStatus,
    ExecutionAssignment,
    ExecutionAssignmentCreate,
    ExecutionWorkerRegister,
    NetworkPolicy,
    WorkerCapability,
    WorkerLifecycle,
    WorkerResourceLimits,
)
from codex_web.execution_workspaces import ExecutionWorkspaceStatus, LeaseMode
from codex_web.resources import RepositoryExecutionTarget, RepositoryTargetSource
from codex_web.local_execution_backend import (
    BubblewrapExecutionBackend,
    LocalExecutionPolicyError,
    LocalExecutionResult,
)
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.execution_workers import ExecutionWorkerService
from codex_web.services.identity import IdentityService
from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.execution_workers import ExecutionWorkerStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _ProbeResult:
    returncode = 0
    stderr = ""


def _probe_success(*args, **kwargs):
    return _ProbeResult()


class _FakeWorkspaceService:
    def __init__(self, path: Path) -> None:
        self.workspace = SimpleNamespace(
            id="execws-1",
            execution_id="exec-1",
            work_item_ref="group/app#42",
            project_id="home",
            resource_ids=("repo-1",),
            base_revision="abc123",
            status=ExecutionWorkspaceStatus.ACTIVE,
            path=str(path),
            repository_resource_id="repo-1",
            repository_members=(),
            actual_disk_bytes=0,
        )

    def get(self, workspace_id, actor):
        if workspace_id != self.workspace.id:
            raise RuntimeError("workspace not found")
        return self.workspace


class _FakeExecutionBackend:
    def __init__(self, result: LocalExecutionResult) -> None:
        self.result = result
        self.validated = []
        self.calls = []

    def validate_assignment(self, assignment):
        self.validated.append(assignment.id)

    def run(
        self,
        assignment,
        *,
        argv,
        workspace_path,
        poll_hook=None,
        environment=None,
        trusted_readonly_mounts=(),
        additional_disk_bytes=0,
    ):
        self.calls.append(
            (
                assignment.id,
                tuple(argv),
                Path(workspace_path),
                tuple(trusted_readonly_mounts),
                additional_disk_bytes,
            )
        )
        if poll_hook is not None:
            poll_hook()
        return self.result


def _assignment(**overrides) -> ExecutionAssignment:
    values = {
        "organization_id": "local",
        "workspace_id": "default",
        "work_item_ref": "group/app#42",
        "execution_id": "exec-1",
        "project_id": "home",
        "resource_ids": ("repo-1",),
        "base_revision": "abc123",
        "execution_contract_version": "1.0",
        "required_capabilities": (
            WorkerCapability.GIT,
            WorkerCapability.COMMAND_EXECUTION,
        ),
        "sandbox": "workspace-write",
        "approval_policy": "on-request",
        "network": NetworkPolicy(),
        "limits": WorkerResourceLimits(
            cpu_seconds=60,
            memory_bytes=256 * 1024 * 1024,
            disk_bytes=1024 * 1024 * 1024,
            process_count=32,
            wall_seconds=120,
        ),
        "execution_workspace_id": "execws-1",
        "created_by": "local-admin",
    }
    values.update(overrides)
    return ExecutionAssignment(**values)


class BubblewrapExecutionBackendTests(unittest.TestCase):
    def test_successful_probe_advertises_command_execution_without_network(self) -> None:
        backend = BubblewrapExecutionBackend(
            executable="/usr/bin/bwrap",
            probe_runner=_probe_success,
        )

        status = backend.probe()

        self.assertTrue(status.ready)
        self.assertIn(WorkerCapability.COMMAND_EXECUTION, status.capabilities)
        self.assertNotIn(WorkerCapability.NETWORK, status.capabilities)
        self.assertTrue(status.supports_network_disabled)
        self.assertFalse(status.supports_network_allowlist)

    def test_failed_probe_removes_command_execution_capability(self) -> None:
        def fail(*args, **kwargs):
            raise OSError("user namespaces disabled")

        backend = BubblewrapExecutionBackend(
            executable="/usr/bin/bwrap",
            probe_runner=fail,
        )

        status = backend.probe()

        self.assertFalse(status.ready)
        self.assertNotIn(WorkerCapability.COMMAND_EXECUTION, status.capabilities)
        self.assertIn("user namespaces disabled", status.reason)

    def test_command_is_namespaced_and_workspace_write_is_the_only_writable_repo_mount(self) -> None:
        backend = BubblewrapExecutionBackend(
            executable="/usr/bin/bwrap",
            probe_runner=_probe_success,
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()

            command = backend.build_command(
                _assignment(),
                argv=("python", "-m", "pytest"),
                workspace_path=workspace,
            )

        self.assertEqual(command[0], "/usr/bin/bwrap")
        self.assertIn("--unshare-net", command)
        self.assertIn("--ro-bind", command)
        self.assertNotIn(["--ro-bind", "/", "/"], [
            command[index:index + 3]
            for index in range(max(0, len(command) - 2))
        ])
        self.assertNotIn("/app/data", command)
        self.assertIn("/tmp/codex-worker-home", command)
        write_index = command.index("--bind")
        self.assertEqual(command[write_index + 1], str(workspace.resolve()))
        self.assertEqual(command[write_index + 2], str(workspace.resolve()))
        self.assertIn("--chdir", command)
        self.assertEqual(command[-3:], ["python", "-m", "pytest"])

    def test_read_only_assignment_mounts_workspace_and_git_metadata_read_only(self) -> None:
        backend = BubblewrapExecutionBackend(
            executable="/usr/bin/bwrap",
            probe_runner=_probe_success,
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            metadata = root / "repo.git"
            workspace.mkdir()
            metadata.mkdir()
            command = backend.build_command(
                _assignment(sandbox="read-only"),
                argv=("git", "status"),
                workspace_path=workspace,
                git_metadata_path=metadata,
            )

        mounts = [
            command[index:index + 3]
            for index in range(max(0, len(command) - 2))
        ]
        self.assertIn(
            ["--ro-bind", str(workspace.resolve()), str(workspace.resolve())],
            mounts,
        )
        self.assertIn(
            ["--ro-bind", str(metadata.resolve()), str(metadata.resolve())],
            mounts,
        )

    def test_danger_full_access_is_writable_but_stays_inside_worker_boundary(self) -> None:
        backend = BubblewrapExecutionBackend(
            executable="/usr/bin/bwrap",
            probe_runner=_probe_success,
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw) / "workspace"
            workspace.mkdir()
            command = backend.build_command(
                _assignment(sandbox="danger-full-access"),
                argv=("git", "status"),
                workspace_path=workspace,
            )

        mounts = [
            command[index:index + 3]
            for index in range(max(0, len(command) - 2))
        ]
        self.assertIn(
            ["--bind", str(workspace.resolve()), str(workspace.resolve())],
            mounts,
        )
        self.assertIn("--unshare-net", command)
        self.assertNotIn(["--ro-bind", "/", "/"], mounts)

    def test_danger_full_access_cannot_make_read_only_sibling_repository_writable(self) -> None:
        backend = BubblewrapExecutionBackend(
            executable="/usr/bin/bwrap",
            probe_runner=_probe_success,
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "mutable-repo"
            sibling = root / "readonly-repo"
            workspace.mkdir()
            sibling.mkdir()
            destination = Path("/mnt/codex-context/repo-2")

            command = backend.build_command(
                _assignment(sandbox="danger-full-access"),
                argv=("sh", "-c", "true"),
                workspace_path=workspace,
                trusted_readonly_mounts=((sibling, destination),),
            )

        mounts = [
            command[index:index + 3]
            for index in range(max(0, len(command) - 2))
        ]
        self.assertIn(
            ["--bind", str(workspace.resolve()), str(workspace.resolve())],
            mounts,
        )
        self.assertIn(
            ["--ro-bind", str(sibling.resolve()), str(destination)],
            mounts,
        )
        self.assertNotIn(
            ["--bind", str(sibling.resolve()), str(destination)],
            mounts,
        )
        self.assertNotIn(["--ro-bind", "/", "/"], mounts)
        self.assertIn("--unshare-net", command)

    def test_network_requests_still_fail_closed_for_local_worker(self) -> None:
        backend = BubblewrapExecutionBackend(
            executable="/usr/bin/bwrap",
            probe_runner=_probe_success,
        )
        with self.assertRaises(LocalExecutionPolicyError):
            backend.validate_assignment(
                _assignment(
                    required_capabilities=(
                        WorkerCapability.GIT,
                        WorkerCapability.COMMAND_EXECUTION,
                        WorkerCapability.NETWORK,
                    ),
                    network=NetworkPolicy(
                        enabled=True,
                        allowed_hosts=("packages.example.com",),
                    ),
                )
            )

    def test_interactive_trusted_mount_keeps_network_namespace_private(self) -> None:
        captured = {}

        class Process:
            pid = 4322

        def popen(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return Process()

        backend = BubblewrapExecutionBackend(
            executable="/usr/bin/bwrap",
            probe_runner=_probe_success,
            popen=popen,
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            broker_root = root / "broker"
            broker_root.mkdir()
            destination = Path("/run/codex-model-egress")

            backend.spawn_interactive(
                _assignment(),
                argv=("python", "-c", "print('ok')"),
                workspace_path=workspace,
                trusted_readonly_mounts=((broker_root, destination),),
            )

        command = captured["command"]
        mounts = [
            command[index:index + 3]
            for index in range(max(0, len(command) - 2))
        ]
        self.assertIn("--unshare-net", command)
        self.assertNotIn("--share-net", command)
        self.assertIn(
            ["--ro-bind", str(broker_root.resolve()), str(destination)],
            mounts,
        )

    def test_interactive_spawn_reuses_bubblewrap_environment_and_resource_limits(self) -> None:
        captured = {}

        class Process:
            pid = 4321

        def popen(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return Process()

        backend = BubblewrapExecutionBackend(
            executable="/usr/bin/bwrap",
            probe_runner=_probe_success,
            popen=popen,
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw) / "workspace"
            workspace.mkdir()
            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "ambient-must-not-cross",
                    "LANG": "C.UTF-8",
                },
                clear=True,
            ):
                process = backend.spawn_interactive(
                    _assignment(),
                    argv=(
                        "codex",
                        "--config",
                        'cli_auth_credentials_store="ephemeral"',
                        "app-server",
                    ),
                    workspace_path=workspace,
                    environment={
                        "CODEX_HOME": "/tmp/codex-worker-home",
                        "CODEX_ACCESS_TOKEN": "delegated-only-at-launch",
                    },
                )

        self.assertEqual(process.pid, 4321)
        self.assertEqual(captured["command"][0], "/usr/bin/bwrap")
        self.assertIn("--unshare-net", captured["command"])
        self.assertEqual(captured["command"][-1], "app-server")
        self.assertNotIn("delegated-only-at-launch", " ".join(captured["command"]))
        env = captured["kwargs"]["env"]
        self.assertEqual(env["CODEX_HOME"], "/tmp/codex-worker-home")
        self.assertEqual(env["CODEX_ACCESS_TOKEN"], "delegated-only-at-launch")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertTrue(captured["kwargs"]["start_new_session"])
        self.assertTrue(callable(captured["kwargs"]["preexec_fn"]))

    def test_minimal_environment_does_not_inherit_ambient_secrets(self) -> None:
        backend = BubblewrapExecutionBackend(
            executable="/usr/bin/bwrap",
            probe_runner=_probe_success,
        )
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "must-not-cross-boundary",
                "AWS_SECRET_ACCESS_KEY": "also-secret",
                "LANG": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
            },
            clear=True,
        ):
            env = backend.minimal_environment()

        self.assertEqual(env["LANG"], "C.UTF-8")
        self.assertEqual(env["PATH"], "/usr/local/bin:/usr/bin:/bin")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)


class LocalExecutionWorkerRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.workspace_path = root / "workspace"
        self.workspace_path.mkdir()
        sqlite = SQLiteStateStore(root / "state.sqlite3")
        self.identity = IdentityService(IdentityStateStore(sqlite))
        self.identity.bootstrap_local()
        self.admin = self.identity.local_trusted_actor()
        self.worker_actor = self.identity.bootstrap_service_actor(
            identity_id="execution-worker-test",
            name="Execution Worker Test",
            scope=self.admin.tenant,
            service_scopes=("execution-worker:run",),
        )
        self.workspaces = _FakeWorkspaceService(self.workspace_path)
        self.worker_service = ExecutionWorkerService(
            ExecutionWorkerStore(sqlite),
            identity=self.identity,
            workspaces=self.workspaces,
        )
        self.worker = self.worker_service.register(
            ExecutionWorkerRegister(
                service_identity_id=self.worker_actor.identity_id,
                pool="local",
                version="test-v1",
                capabilities=(
                    WorkerCapability.GIT,
                    WorkerCapability.COMMAND_EXECUTION,
                    WorkerCapability.ARTIFACT_UPLOAD,
                ),
            ),
            actor=self.admin,
        )
        self.artifacts = ArtifactEvidenceService(ArtifactEvidenceStore(sqlite))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _create_assignment(self, **overrides):
        values = {
            "work_item_ref": "group/app#42",
            "execution_id": "exec-1",
            "project_id": "home",
            "resource_ids": ("repo-1",),
            "base_revision": "abc123",
            "execution_contract_version": "1.0",
            "required_capabilities": (
                WorkerCapability.GIT,
                WorkerCapability.COMMAND_EXECUTION,
            ),
            "sandbox": "workspace-write",
            "approval_policy": "on-request",
            "limits": WorkerResourceLimits(
                cpu_seconds=60,
                memory_bytes=256 * 1024 * 1024,
                disk_bytes=1024 * 1024 * 1024,
                process_count=32,
                wall_seconds=120,
            ),
            "execution_workspace_id": "execws-1",
        }
        values.update(overrides)
        return self.worker_service.create_assignment(
            ExecutionAssignmentCreate(**values),
            actor=self.admin,
        )

    def _runtime(self, result: LocalExecutionResult):
        backend = _FakeExecutionBackend(result)
        runtime = LocalExecutionWorkerRuntime(
            self.worker_service,
            self.workspaces,
            backend,
            worker=self.worker,
            worker_actor=self.worker_actor,
            control_actor=self.admin,
            artifact_evidence=self.artifacts,
            heartbeat_interval_seconds=5,
            renew_margin_seconds=200,
        )
        return runtime, backend

    def test_local_bootstrap_reactivates_offline_worker_and_drops_stale_command_capability(self) -> None:
        self.worker_service.set_lifecycle(
            self.worker.id,
            WorkerLifecycle.OFFLINE,
            actor=self.admin,
            reason="simulated restart gap",
        )

        refreshed = self.worker_service.ensure_local_worker(
            service_identity_id=self.worker_actor.identity_id,
            version="test-v2",
            capabilities=(
                WorkerCapability.GIT,
                WorkerCapability.ARTIFACT_UPLOAD,
            ),
            actor=self.admin,
        )

        self.assertEqual(refreshed.id, self.worker.id)
        self.assertEqual(refreshed.lifecycle, WorkerLifecycle.ACTIVE)
        self.assertNotIn(
            WorkerCapability.COMMAND_EXECUTION,
            refreshed.capabilities,
        )
        self.assertEqual(refreshed.version, "test-v2")

    def test_runtime_claims_starts_renews_and_completes_exact_assignment(self) -> None:
        assignment = self._create_assignment()
        runtime, backend = self._runtime(
            LocalExecutionResult(
                executable="python",
                command_digest="sha256:" + "a" * 64,
                exit_code=0,
                stdout="ok",
                stderr="",
                duration_seconds=0.01,
                disk_bytes=10,
            )
        )

        completion = runtime.execute(
            assignment.id,
            ("python", "-m", "pytest"),
        )

        self.assertEqual(completion.assignment.status, AssignmentStatus.SUCCEEDED)
        self.assertEqual(backend.validated, [assignment.id])
        self.assertEqual(backend.calls[0][2], self.workspace_path)
        persisted = next(
            item
            for item in self.worker_service.store.load().assignments
            if item.id == assignment.id
        )
        self.assertIsNone(persisted.lease)
        events = self.worker_service.events(self.admin)
        self.assertTrue(
            any(
                item.event_type == "assignment_started"
                and item.assignment_id == assignment.id
                for item in events
            )
        )

    def test_runtime_passes_canonical_read_only_repository_mounts_to_backend(self) -> None:
        readonly_path = Path(self.temp.name) / "readonly-repo"
        readonly_path.mkdir()
        self.workspaces.workspace.resource_ids = ("repo-1", "repo-2")
        self.workspaces.workspace.repository_members = (
            SimpleNamespace(
                resource_id="repo-1",
                access_mode=LeaseMode.WRITE,
                workspace_path=str(self.workspace_path),
                sandbox_path=str(self.workspace_path),
            ),
            SimpleNamespace(
                resource_id="repo-2",
                access_mode=LeaseMode.READ,
                workspace_path=str(readonly_path),
                sandbox_path="/mnt/codex-context/repo-2",
                disk_bytes=64,
            ),
        )
        assignment = self._create_assignment(
            resource_ids=("repo-1", "repo-2"),
            repository_target=RepositoryExecutionTarget(
                organization_id="local",
                workspace_id="default",
                project_id="home",
                mutable_repository_id="repo-1",
                read_only_repository_ids=("repo-2",),
                source=RepositoryTargetSource.EXPLICIT,
            ),
        )
        runtime, backend = self._runtime(
            LocalExecutionResult(
                executable="git",
                command_digest="sha256:" + "d" * 64,
                exit_code=0,
                stdout="ok",
                stderr="",
                duration_seconds=0.01,
                disk_bytes=10,
            )
        )

        runtime.execute(assignment.id, ("git", "status"))

        self.assertEqual(
            backend.calls[0][3],
            ((readonly_path.resolve(), Path("/mnt/codex-context/repo-2")),),
        )
        self.assertEqual(backend.calls[0][4], 64)

    def test_limit_breach_creates_metadata_only_failure_evidence(self) -> None:
        assignment = self._create_assignment()
        runtime, _ = self._runtime(
            LocalExecutionResult(
                executable="python",
                command_digest="sha256:" + "b" * 64,
                exit_code=-9,
                stdout="",
                stderr="",
                duration_seconds=1.5,
                limit_breach="memory_bytes",
                disk_bytes=1024,
            )
        )

        completion = runtime.execute(assignment.id, ("python", "tests.py"))

        self.assertEqual(completion.assignment.status, AssignmentStatus.FAILED)
        self.assertEqual(
            completion.assignment.failure_code,
            "worker_limit_memory_bytes",
        )
        self.assertIsNotNone(completion.evidence_id)
        evidence = self.artifacts.list_evidence(self.worker_actor)
        record = next(item for item in evidence if item.id == completion.evidence_id)
        self.assertEqual(record.evidence_type, EvidenceType.POLICY_EVALUATION)
        self.assertEqual(record.metadata["limit_breach"], "memory_bytes")
        self.assertEqual(record.metadata["command_digest"], "sha256:" + "b" * 64)
        serialized = record.model_dump_json()
        self.assertNotIn("tests.py", serialized)
        self.assertNotIn("stdout", serialized)
        self.assertNotIn("stderr", serialized)

    def test_runtime_rejects_assignment_without_canonical_execution_workspace(self) -> None:
        assignment = self._create_assignment(
            execution_id="exec-no-workspace",
            execution_workspace_id=None,
        )
        runtime, _ = self._runtime(
            LocalExecutionResult(
                executable="true",
                command_digest="sha256:" + "c" * 64,
                exit_code=0,
                stdout="",
                stderr="",
                duration_seconds=0.01,
            )
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "requires an isolated execution workspace",
        ):
            runtime.execute(assignment.id, ("true",))


if __name__ == "__main__":
    unittest.main()
