from __future__ import annotations

import os
import resource
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.artifact_evidence import EvidenceType
from codex_web.execution_workers import (
    AssignmentClaimRequest,
    AssignmentStartRequest,
    AssignmentStatus,
    ExecutionAssignment,
    ExecutionAssignmentCreate,
    ExecutionWorkerRegister,
    NetworkPolicy,
    WorkerCapability,
    WorkerLifecycle,
    WorkerResourceLimits,
)
from codex_web.execution_workspaces import (
    ExecutionWorkspaceStatus,
    LeaseMode,
    RepositoryCheckpointPushStatus,
)
from codex_web.resources import (
    RepositoryExecutionScope,
    RepositoryExecutionTarget,
    RepositoryTargetSource,
    RepositoryWriteMode,
)
from codex_web.local_execution_backend import (
    BubblewrapExecutionBackend,
    LocalExecutionPolicyError,
    LocalExecutionResult,
)
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.execution_workers import ExecutionWorkerService
from codex_web.services.identity import IdentityService
from codex_web.services.local_execution_worker import (
    LocalExecutionWorkerRuntime,
    RepositoryCheckpointBlockedError,
)
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
            writable_repository_ids=(),
            repository_members=(),
            repository_checkpoints={},
            actual_disk_bytes=0,
        )
        self.checkpoints = []

    def get(self, workspace_id, actor):
        if workspace_id != self.workspace.id:
            raise RuntimeError("workspace not found")
        return self.workspace

    def record_repository_checkpoint(self, workspace_id, checkpoint, *, actor):
        self.checkpoints.append(checkpoint)
        self.workspace.repository_checkpoints[checkpoint.resource_id] = checkpoint
        for member in self.workspace.repository_members:
            if member.resource_id == checkpoint.resource_id:
                member.head_revision = checkpoint.head_revision
        return self.workspace


class _FakeExecutionBackend:
    def __init__(self, result: LocalExecutionResult) -> None:
        self.result = result
        self.validated = []
        self.calls = []

    def validate_assignment(self, assignment):
        self.validated.append(assignment.id)

    @staticmethod
    def discover_git_metadata(workspace_path):
        return None

    def run(
        self,
        assignment,
        *,
        argv,
        workspace_path,
        poll_hook=None,
        environment=None,
        trusted_readonly_mounts=(),
        trusted_writable_mounts=(),
        additional_disk_bytes=0,
        git_metadata_path=None,
    ):
        self.calls.append(
            (
                assignment.id,
                tuple(argv),
                Path(workspace_path),
                tuple(trusted_readonly_mounts),
                tuple(trusted_writable_mounts),
                additional_disk_bytes,
                dict(environment or {}),
            )
        )
        if poll_hook is not None:
            poll_hook()
        return self.result


class _CheckpointBackend(_FakeExecutionBackend):
    def __init__(self, *, status: str) -> None:
        super().__init__(LocalExecutionResult("git", "sha256:" + "c" * 64, 0, "", "", 0.01))
        self.status = status
        self.committed = False

    def run(self, assignment, *, argv, workspace_path, **kwargs):
        command = tuple(argv)
        self.calls.append((assignment.id, command, Path(workspace_path), (), (), 0))
        if "status" in command:
            stdout = "" if self.committed else self.status
        elif "symbolic-ref" in command:
            stdout = "codex/group-app-42/abc123\n"
        elif "rev-parse" in command:
            stdout = (("b" if self.committed else "a") * 40) + "\n"
        elif "diff" in command:
            stdout = (
                "code.py\0"
                if self.committed and not any(("b" * 40) in value for value in command)
                else ""
            )
        elif "commit" in command:
            self.committed = True
            stdout = "checkpointed\n"
        else:
            stdout = ""
        return LocalExecutionResult(
            executable="git",
            command_digest="sha256:" + "c" * 64,
            exit_code=0,
            stdout=stdout,
            stderr="",
            duration_seconds=0.01,
        )


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


class WorkerDiskAccountingTests(unittest.TestCase):
    def test_nested_hidden_and_hardlinked_files_count_without_symlink_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            root.mkdir()
            (root / ".hidden").write_bytes(b"h" * 11)
            (root / "nested").mkdir()
            regular = root / "nested" / "regular"
            regular.write_bytes(b"r" * 17)
            os.link(regular, root / "hardlink")
            (root / "file-link").symlink_to(regular)
            external = Path(temporary) / "external"
            external.mkdir()
            (external / "large").write_bytes(b"e" * 1000)
            (root / "directory-link").symlink_to(external, target_is_directory=True)
            (root / "dangling-link").symlink_to(external / "missing")
            (root / "empty").touch()
            os.mkfifo(root / "pipe")
            self.assertEqual(BubblewrapExecutionBackend._tree_disk_usage(root), 45)
            self.assertEqual(BubblewrapExecutionBackend._tree_disk_usage(root / "missing"), 0)
            self.assertEqual(BubblewrapExecutionBackend._tree_disk_usage(regular), 0)

    def test_file_accounting_uses_one_size_lookup_per_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            file_count = 40
            for number in range(file_count):
                (root / str(number)).write_bytes(b"x" * (number + 1))
            real_scandir, real_stat, real_lstat = os.scandir, os.stat, os.lstat
            metadata_calls = []

            class Entry:
                def __init__(self, actual):
                    self.actual = actual

                def __getattr__(self, name):
                    return getattr(self.actual, name)

                def stat(self, *args, **kwargs):
                    metadata_calls.append(self.actual.path)
                    return self.actual.stat(*args, **kwargs)

            class Entries:
                def __init__(self, path):
                    self.actual = real_scandir(path)

                def __enter__(self):
                    return (Entry(entry) for entry in self.actual)

                def __exit__(self, *args):
                    self.actual.close()

            def file_stat(path, *args, **kwargs):
                metadata_calls.append(str(path))
                return real_stat(path, *args, **kwargs)

            def file_lstat(path, *args, **kwargs):
                metadata_calls.append(str(path))
                return real_lstat(path, *args, **kwargs)

            with (
                patch("os.scandir", side_effect=Entries),
                patch("os.stat", side_effect=file_stat),
                patch("os.lstat", side_effect=file_lstat),
            ):
                actual = BubblewrapExecutionBackend._tree_disk_usage(root)
            self.assertEqual(actual, sum(range(1, file_count + 1)))
            self.assertEqual(len(metadata_calls), file_count)

    def test_unavailable_directory_does_not_hide_accessible_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "visible").write_bytes(b"visible")
            blocked = root / "blocked"
            blocked.mkdir()
            (blocked / "unavailable").write_bytes(b"unavailable")
            real_scandir = os.scandir

            def scandir(path):
                if Path(path) == blocked:
                    raise PermissionError("unavailable directory")
                return real_scandir(path)

            with patch("os.scandir", side_effect=scandir):
                self.assertEqual(BubblewrapExecutionBackend._tree_disk_usage(root), 7)

    def test_vanished_entry_does_not_abort_remaining_file_accounting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "retained").write_bytes(b"retained")
            vanished = root / "vanished"
            vanished.write_bytes(b"gone")
            real_scandir = os.scandir

            class Entry:
                def __init__(self, actual):
                    self.actual = actual

                def __getattr__(self, name):
                    return getattr(self.actual, name)

                def stat(self, *args, **kwargs):
                    if self.actual.name == "vanished":
                        vanished.unlink()
                    return self.actual.stat(*args, **kwargs)

            class Entries:
                def __init__(self, path):
                    self.actual = real_scandir(path)

                def __enter__(self):
                    return (Entry(entry) for entry in self.actual)

                def __exit__(self, *args):
                    self.actual.close()

            with patch("os.scandir", side_effect=Entries):
                self.assertEqual(BubblewrapExecutionBackend._tree_disk_usage(root), 8)

    def test_file_replaced_by_symlink_before_size_lookup_is_not_accounted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            root.mkdir()
            regular = root / "regular"
            regular.write_bytes(b"before")
            external = Path(temporary) / "outside"
            external.write_bytes(b"outside" * 100)
            real_scandir = os.scandir

            class Entry:
                def __init__(self, actual):
                    self.actual = actual

                def __getattr__(self, name):
                    return getattr(self.actual, name)

                def stat(self, *args, **kwargs):
                    regular.unlink()
                    regular.symlink_to(external)
                    return self.actual.stat(*args, **kwargs)

            class Entries:
                def __init__(self, path):
                    self.actual = real_scandir(path)

                def __enter__(self):
                    return (Entry(entry) for entry in self.actual)

                def __exit__(self, *args):
                    self.actual.close()

            with patch("os.scandir", side_effect=Entries):
                self.assertEqual(BubblewrapExecutionBackend._tree_disk_usage(root), 0)

    def test_shared_git_metadata_remains_in_execution_disk_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "source").write_bytes(b"source")
            metadata = root / "shared-git"
            metadata.mkdir()
            (metadata / "pack").write_bytes(b"pack" * 4)
            self.assertEqual(
                BubblewrapExecutionBackend._execution_disk_usage(workspace, metadata), 22
            )
            self.assertEqual(
                BubblewrapExecutionBackend._execution_disk_usage(workspace, workspace), 6
            )


class BubblewrapExecutionBackendTests(unittest.TestCase):
    def test_successful_probe_advertises_command_execution_and_unrestricted_network(self) -> None:
        backend = BubblewrapExecutionBackend(
            executable="/usr/bin/bwrap",
            probe_runner=_probe_success,
        )

        status = backend.probe()

        self.assertTrue(status.ready)
        self.assertIn(WorkerCapability.COMMAND_EXECUTION, status.capabilities)
        self.assertIn(WorkerCapability.NETWORK, status.capabilities)
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
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            trust_store = root / "trust-store"
            workspace.mkdir()
            trust_store.mkdir()
            backend = BubblewrapExecutionBackend(
                executable="/usr/bin/bwrap",
                probe_runner=_probe_success,
                trust_store_mounts=((trust_store, Path("/etc/pki")),),
            )

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
        mounts = [
            command[index:index + 3]
            for index in range(max(0, len(command) - 2))
        ]
        self.assertIn(
            ["--ro-bind", str(trust_store.resolve()), "/etc/pki"],
            mounts,
        )
        self.assertNotIn(["--ro-bind", "/etc", "/etc"], mounts)
        write_index = command.index("--bind")
        self.assertEqual(command[write_index + 1], str(workspace.resolve()))
        self.assertEqual(command[write_index + 2], str(workspace.resolve()))
        self.assertIn("--chdir", command)
        self.assertEqual(command[-3:], ["python", "-m", "pytest"])

    def test_command_mounts_public_identity_maps_without_password_hashes(self) -> None:
        backend = BubblewrapExecutionBackend(
            executable="/usr/bin/bwrap",
            probe_runner=_probe_success,
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw) / "workspace"
            workspace.mkdir()

            command = backend.build_command(
                _assignment(),
                argv=("getent", "passwd", str(os.getuid())),
                workspace_path=workspace,
            )

        mounts = [
            command[index:index + 3]
            for index in range(max(0, len(command) - 2))
        ]
        for identity_file in backend.SYSTEM_IDENTITY_FILES:
            if identity_file.is_file():
                self.assertIn(
                    ["--ro-bind", str(identity_file), str(identity_file)],
                    mounts,
                )
        self.assertNotIn("/etc/shadow", command)
        self.assertNotIn("/etc/gshadow", command)

    def test_command_mounts_only_system_ca_trust_directories_read_only(self) -> None:
        backend = BubblewrapExecutionBackend(
            executable="/usr/bin/bwrap",
            probe_runner=_probe_success,
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            trust = root / "ca-trust"
            workspace.mkdir()
            trust.mkdir()
            backend.SYSTEM_TRUST_DIRECTORIES = (trust,)

            command = backend.build_command(
                _assignment(),
                argv=("python", "-m", "pytest"),
                workspace_path=workspace,
            )

        mounts = [
            command[index:index + 3]
            for index in range(max(0, len(command) - 2))
        ]
        self.assertIn(
            ["--ro-bind", str(trust), str(trust)],
            mounts,
        )
        self.assertNotIn(["--ro-bind", "/etc", "/etc"], mounts)

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

    def test_network_enabled_danger_full_access_uses_host_network_namespace(self) -> None:
        backend = BubblewrapExecutionBackend(
            executable="/usr/bin/bwrap",
            probe_runner=_probe_success,
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw) / "workspace"
            workspace.mkdir()
            command = backend.build_command(
                _assignment(
                    sandbox="danger-full-access",
                    network=NetworkPolicy(enabled=True),
                    required_capabilities=(
                        WorkerCapability.GIT,
                        WorkerCapability.COMMAND_EXECUTION,
                        WorkerCapability.NETWORK,
                    ),
                ),
                argv=("python", "-m", "pip", "--version"),
                workspace_path=workspace,
            )

        self.assertNotIn("--unshare-net", command)
        mounts = [
            command[index:index + 3]
            for index in range(max(0, len(command) - 2))
        ]
        for network_file in backend.SYSTEM_NETWORK_FILES:
            if network_file.is_file():
                self.assertIn(
                    ["--ro-bind", str(network_file), str(network_file)],
                    mounts,
                )
        self.assertNotIn(["--ro-bind", "/", "/"], [
            command[index:index + 3]
            for index in range(max(0, len(command) - 2))
        ])

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

    def test_explicit_secondary_writable_repository_is_bind_mounted(self) -> None:
        backend = BubblewrapExecutionBackend(
            executable="/usr/bin/bwrap",
            probe_runner=_probe_success,
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "primary"
            sibling = root / "secondary"
            workspace.mkdir()
            sibling.mkdir()
            destination = Path("/mnt/codex-repositories/repo-2")

            command = backend.build_command(
                _assignment(sandbox="workspace-write"),
                argv=("sh", "-c", "true"),
                workspace_path=workspace,
                trusted_writable_mounts=((sibling, destination),),
            )

        mounts = [
            command[index:index + 3]
            for index in range(max(0, len(command) - 2))
        ]
        self.assertIn(
            ["--bind", str(sibling.resolve()), str(destination)],
            mounts,
        )
        self.assertNotIn(
            ["--ro-bind", str(sibling.resolve()), str(destination)],
            mounts,
        )

    def test_read_only_assignment_rejects_secondary_writable_repository(self) -> None:
        backend = BubblewrapExecutionBackend(
            executable="/usr/bin/bwrap",
            probe_runner=_probe_success,
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "primary"
            sibling = root / "secondary"
            workspace.mkdir()
            sibling.mkdir()

            with self.assertRaises(LocalExecutionPolicyError):
                backend.build_command(
                    _assignment(sandbox="read-only"),
                    argv=("sh", "-c", "true"),
                    workspace_path=workspace,
                    trusted_writable_mounts=(
                        (sibling, Path("/mnt/codex-repositories/repo-2")),
                    ),
                )

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
                    minimum_address_space_bytes=2 * 1024**4,
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
        preexec_fn = captured["kwargs"]["preexec_fn"]
        self.assertTrue(callable(preexec_fn))
        with patch("resource.setrlimit") as setrlimit:
            preexec_fn()
        self.assertIn(
            (
                resource.RLIMIT_AS,
                (
                    2 * 1024**4,
                    2 * 1024**4,
                ),
            ),
            [call.args for call in setrlimit.call_args_list],
        )

    def test_command_process_headroom_accounts_for_shared_uid_and_hard_limit(self):
        limits = _assignment().limits
        for baseline, hard, expected in [(520, 4096, 520 + limits.process_count), (520, 530, 530)]:
            with patch.object(BubblewrapExecutionBackend, "_host_uid_task_count", return_value=baseline), patch(
                "resource.getrlimit", return_value=(hard, hard)
            ):
                apply = BubblewrapExecutionBackend._limits_preexec(limits)
            with patch("resource.setrlimit") as setrlimit:
                apply()
            calls = [call.args for call in setrlimit.call_args_list]
            self.assertIn((resource.RLIMIT_NPROC, (expected, expected)), calls)
            self.assertIn((resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds)), calls)
            self.assertIn((resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes)), calls)
            self.assertIn((resource.RLIMIT_FSIZE, (limits.disk_bytes, limits.disk_bytes)), calls)

    def test_one_shot_commands_keep_assignment_address_space_limit(self) -> None:
        limits = _assignment().limits
        preexec_fn = BubblewrapExecutionBackend._limits_preexec(limits)

        with patch("resource.setrlimit") as setrlimit:
            preexec_fn()

        self.assertIn(
            (
                resource.RLIMIT_AS,
                (limits.memory_bytes, limits.memory_bytes),
            ),
            [call.args for call in setrlimit.call_args_list],
        )


    def test_process_tree_rss_reports_current_process(self) -> None:
        self.assertGreater(
            BubblewrapExecutionBackend.process_tree_rss_bytes(os.getpid()),
            0,
        )

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
        execution_venv = self.workspace_path / ".venv"
        (execution_venv / "bin").mkdir(parents=True)
        (execution_venv / "bin" / "python").touch()
        (execution_venv / "lib" / "python3.14" / "site-packages").mkdir(
            parents=True
        )
        (execution_venv / ".codex-web-execution-venv").write_text("test\n")
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

    def _checkpoint_runtime(self, *, status: str):
        assignment = self._create_assignment()
        claimed = self.worker_service.claim(
            self.worker.id,
            AssignmentClaimRequest(lease_seconds=120),
            actor=self.worker_actor,
            assignment_id=assignment.id,
        )
        self.worker_service.start(
            self.worker.id,
            assignment.id,
            AssignmentStartRequest(
                lease_token=claimed.lease.lease_token,
                fence=claimed.lease.fence,
            ),
            actor=self.worker_actor,
        )
        self.workspaces.workspace.writable_repository_ids = ("repo-1",)
        self.workspaces.workspace.repository_members = (
            SimpleNamespace(
                resource_id="repo-1",
                access_mode=LeaseMode.WRITE,
                workspace_path=str(self.workspace_path),
                sandbox_path=str(self.workspace_path),
                branch_name="codex/group-app-42/abc123",
                head_revision="a" * 40,
                disk_bytes=0,
            ),
        )
        backend = _CheckpointBackend(status=status)
        runtime = LocalExecutionWorkerRuntime(
            self.worker_service,
            self.workspaces,
            backend,
            worker=self.worker,
            worker_actor=self.worker_actor,
            control_actor=self.admin,
        )
        return assignment, runtime, backend

    def test_checkpoint_assignment_commits_dirty_work_and_records_push_blocker(self) -> None:
        assignment, runtime, backend = self._checkpoint_runtime(
            status=" M code.py\0?? new.py\0"
        )

        checkpoints = runtime.checkpoint_assignment(assignment.id)

        self.assertTrue(backend.committed)
        self.assertEqual(len(checkpoints), 1)
        checkpoint = checkpoints[0]
        self.assertEqual(checkpoint.head_revision, "b" * 40)
        self.assertEqual(checkpoint.dirty_file_count, 2)
        self.assertTrue(checkpoint.local_commit_created)
        self.assertEqual(
            checkpoint.push_status,
            RepositoryCheckpointPushStatus.BLOCKED,
        )
        self.assertEqual(checkpoint.blocker_code, "checkpoint_push_unverified")
        self.assertEqual(self.workspaces.checkpoints, [checkpoint])

        repeated = runtime.checkpoint_assignment(assignment.id)[0]
        self.assertEqual(repeated.push_status, RepositoryCheckpointPushStatus.BLOCKED)
        self.assertEqual(repeated.blocker_code, "checkpoint_push_unverified")

    def test_checkpoint_assignment_refuses_sensitive_untracked_path(self) -> None:
        assignment, runtime, backend = self._checkpoint_runtime(status="?? .env\0")

        with self.assertRaisesRegex(
            RepositoryCheckpointBlockedError,
            "sensitive or policy-excluded",
        ):
            runtime.checkpoint_assignment(assignment.id)

        self.assertFalse(backend.committed)
        self.assertEqual(
            self.workspaces.checkpoints[0].blocker_code,
            "checkpoint_excluded_paths",
        )

    def test_checkpoint_assignment_is_git_zero_write_when_clean(self) -> None:
        assignment, runtime, backend = self._checkpoint_runtime(status="")

        checkpoint = runtime.checkpoint_assignment(assignment.id)[0]

        commands = [call[1] for call in backend.calls]
        self.assertFalse(any("add" in command for command in commands))
        self.assertFalse(any("commit" in command for command in commands))
        self.assertFalse(checkpoint.local_commit_created)
        self.assertEqual(checkpoint.dirty_file_count, 0)
        self.assertEqual(checkpoint.changed_file_count, 0)

    def test_runtime_tool_mounts_expose_playwright_read_only_roots(self) -> None:
        python_root = Path(self.temp.name) / "python-tool"
        tool_root = Path(self.temp.name) / "playwright-tool"
        browser_root = Path(self.temp.name) / "playwright-browsers"
        python_root.mkdir()
        tool_root.mkdir()
        browser_root.mkdir()
        runtime = LocalExecutionWorkerRuntime(
            self.worker_service,
            self.workspaces,
            _FakeExecutionBackend(LocalExecutionResult(
                executable="true",
                command_digest="sha256:" + "0" * 64,
                exit_code=0,
                stdout="",
                stderr="",
                duration_seconds=0.0,
                disk_bytes=0,
            )),
            worker=self.worker,
            worker_actor=self.worker_actor,
            control_actor=self.admin,
            python_tool_root=python_root,
            playwright_tool_root=tool_root,
            playwright_browser_root=browser_root,
        )

        self.assertEqual(
            runtime.runtime_tool_mounts(),
            (
                (python_root.resolve(), Path("/opt/codex-python")),
                (tool_root.resolve(), Path("/opt/codex-playwright")),
                (browser_root.resolve(), Path("/opt/codex-playwright-browsers")),
            ),
        )

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
        self.assertEqual(
            backend.calls[0][6]["VIRTUAL_ENV"],
            str(self.workspace_path / ".venv"),
        )
        self.assertTrue((self.workspace_path / ".venv" / "bin" / "python").exists())
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

    def test_python_environment_layers_project_venv_read_only(self) -> None:
        project_path = Path(self.temp.name) / "project"
        project_site_packages = (
            project_path / ".venv" / "lib" / "python3.14" / "site-packages"
        )
        project_site_packages.mkdir(parents=True)
        (project_path / ".venv" / "bin").mkdir()
        self.workspaces.workspace.repository_members = (
            SimpleNamespace(
                resource_id="repo-1",
                source_path=str(project_path),
            ),
        )
        runtime, _backend = self._runtime(
            LocalExecutionResult(
                executable="python",
                command_digest="sha256:" + "b" * 64,
                exit_code=0,
                stdout="ok",
                stderr="",
                duration_seconds=0.01,
            )
        )

        result = runtime.python_environment(_assignment())

        execution_venv = self.workspace_path / ".venv"
        self.assertEqual(result.environment["VIRTUAL_ENV"], str(execution_venv))
        self.assertEqual(
            result.readonly_mounts,
            ((project_path / ".venv", project_path / ".venv"),),
        )
        baseline = next(
            (execution_venv / "lib").glob(
                "python*/site-packages/codex_web_project_baseline.pth"
            )
        )
        self.assertEqual(baseline.read_text(), f"{project_site_packages}\n")
        self.assertTrue(result.environment["PATH"].startswith(
            f"{execution_venv}/bin:{project_path}/.venv/bin:"
        ))

    def test_python_environment_provisions_missing_venv(self) -> None:
        shutil.rmtree(self.workspace_path / ".venv")
        runtime, _backend = self._runtime(
            LocalExecutionResult(
                executable="python",
                command_digest="sha256:" + "c" * 64,
                exit_code=0,
                stdout="ok",
                stderr="",
                duration_seconds=0.01,
            )
        )

        result = runtime.python_environment(_assignment())

        execution_venv = self.workspace_path / ".venv"
        self.assertTrue((execution_venv / ".codex-web-execution-venv").is_file())
        self.assertTrue((execution_venv / "bin" / "python").exists())
        self.assertTrue((execution_venv / "bin" / "pip").exists())
        self.assertIn(
            "include-system-site-packages = true",
            (execution_venv / "pyvenv.cfg").read_text(),
        )
        self.assertEqual(result.environment["VIRTUAL_ENV"], str(execution_venv))

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
        self.assertEqual(backend.calls[0][4], ())
        self.assertEqual(backend.calls[0][5], 64)

    def test_runtime_passes_coordinated_writable_repository_mounts_to_backend(self) -> None:
        writable_path = Path(self.temp.name) / "writable-repo-2"
        writable_path.mkdir()
        self.workspaces.workspace.resource_ids = ("repo-1", "repo-2")
        self.workspaces.workspace.writable_repository_ids = ("repo-1", "repo-2")
        self.workspaces.workspace.repository_members = (
            SimpleNamespace(
                resource_id="repo-1",
                access_mode=LeaseMode.WRITE,
                workspace_path=str(self.workspace_path),
                sandbox_path=str(self.workspace_path),
                disk_bytes=32,
            ),
            SimpleNamespace(
                resource_id="repo-2",
                access_mode=LeaseMode.WRITE,
                workspace_path=str(writable_path),
                sandbox_path="/mnt/codex-repositories/repo-2",
                disk_bytes=64,
            ),
        )
        assignment = self._create_assignment(
            resource_ids=("repo-1", "repo-2"),
            repository_scope=RepositoryExecutionScope(
                organization_id="local",
                workspace_id="default",
                project_id="home",
                writable_repository_ids=("repo-1", "repo-2"),
                write_mode=RepositoryWriteMode.COORDINATED,
                source=RepositoryTargetSource.EXPLICIT,
            ),
        )
        runtime, backend = self._runtime(
            LocalExecutionResult(
                executable="git",
                command_digest="sha256:" + "e" * 64,
                exit_code=0,
                stdout="ok",
                stderr="",
                duration_seconds=0.01,
                disk_bytes=10,
            )
        )

        runtime.execute(assignment.id, ("git", "status"))

        self.assertEqual(backend.calls[0][3], ())
        self.assertEqual(
            backend.calls[0][4],
            ((writable_path.resolve(), Path("/mnt/codex-repositories/repo-2")),),
        )
        self.assertEqual(backend.calls[0][5], 64)

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
