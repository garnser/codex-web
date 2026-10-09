from __future__ import annotations

import asyncio
import dataclasses
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from codex_web.execution_subjects import ExecutionSubject
from codex_web.execution_workers import (
    AssignmentClaimRequest,
    AssignmentStartRequest,
    AssignmentStatus,
    ExecutionAssignmentCreate,
    ExecutionRuntimeBinding,
    ExecutionWorkerRegister,
    NetworkPolicy,
    WorkerCapability,
    WorkerLifecycle,
    WorkerResourceLimits,
)
from codex_web.execution_workspaces import (
    ExecutionWorkspace, ExecutionWorkspaceLease, ExecutionWorkspaceKind,
    ExecutionWorkspaceStatus, LeaseMode,
)
from codex_web.resources import (
    RepositoryExecutionScope,
    RepositoryTargetSource,
    RepositoryWriteMode,
)
from codex_web.runtime.codex import CodexRuntime
from codex_web.services.agent_process_session import (
    AssignmentBoundAgentProcessSession,
    AssignmentBoundAgentProcessSessionStaleError,
)
from codex_web.services.codex_auth_delegation import (
    CODEX_WORKER_HOME,
    CodexAuthDelegation,
    CodexAuthDelegationStaleError,
    CodexDelegatedLaunch,
)
from codex_web.services.codex_worker_session import (
    AssignmentBoundCodexSession,
    AssignmentBoundCodexSessionManager,
    AssignmentBoundCodexSessionStaleError,
)
from codex_web.services.codex_model_egress import CodexModelEgressEndpoint
from codex_web.services.execution_workers import ExecutionWorkerService, WorkerConflictError
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.execution_workspaces import ExecutionWorkspaceService
from codex_web.storage.execution_workspaces import ExecutionWorkspaceStateStore
from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime
from codex_web.storage.execution_workers import ExecutionWorkerStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _FakeWorkspaceService:
    def __init__(self, path: Path) -> None:
        self.workspace = SimpleNamespace(
            id="execws-codex",
            execution_id="exec-codex",
            work_item_ref="group/app#codex",
            project_id="home",
            resource_ids=("repo-1",),
            base_revision="abc123",
            status=ExecutionWorkspaceStatus.ACTIVE,
            path=str(path),
        )

    def get(self, workspace_id, actor):
        if workspace_id != self.workspace.id:
            raise RuntimeError("workspace not found")
        return self.workspace


class _FakeProcess:
    _next_pid = 4000

    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.pid = _FakeProcess._next_pid
        _FakeProcess._next_pid += 1

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class _FakeBackend:
    def __init__(self) -> None:
        self.validated = []
        self.spawned = []
        self.disk_bytes = 0
        self.disk_usage_calls = 0
        self.processes: list[_FakeProcess] = []
        self.git_metadata: Path | None = None
        self.git_worktree_metadata: Path | None = None

    def validate_assignment(self, assignment) -> None:
        self.validated.append(assignment.id)

    def discover_git_metadata(self, workspace_path):
        return self.git_metadata

    def discover_git_worktree_metadata(self, workspace_path):
        return self.git_worktree_metadata

    def execution_disk_usage(self, workspace_path, git_metadata_path=None):
        self.disk_usage_calls += 1
        return self.disk_bytes

    def terminate_process(self, process) -> None:
        process.kill()

    def spawn_interactive(
        self,
        assignment,
        *,
        argv,
        workspace_path,
        environment=None,
        **kwargs,
    ):
        process = _FakeProcess()
        self.processes.append(process)
        self.spawned.append(
            {
                "assignment_id": assignment.id,
                "argv": tuple(argv),
                "workspace_path": Path(workspace_path),
                "environment_keys": tuple(sorted((environment or {}).keys())),
                "access_token_present": bool((environment or {}).get("CODEX_ACCESS_TOKEN")),
                "environment": dict(environment or {}),
                "trusted_readonly_mounts": tuple(
                    kwargs.get("trusted_readonly_mounts") or ()
                ),
                "trusted_writable_mounts": tuple(
                    kwargs.get("trusted_writable_mounts") or ()
                ),
                "minimum_address_space_bytes": kwargs.get(
                    "minimum_address_space_bytes", 0
                ),
                "minimum_process_count": kwargs.get("minimum_process_count", 0),
            }
        )
        return process


class _FakeDelegationService:
    def __init__(self) -> None:
        self.validation_error: Exception | None = None
        self.now = 1_800_000_000.0
        self.secret = "worker-codex-token-do-not-persist"
        self.use_calls = 0
        self.validate_calls = 0

    def _delegation(self, assignment, worker_id, fence):
        return CodexAuthDelegation(
            assignment_id=assignment.id,
            worker_id=worker_id,
            fence=fence,
            secret_id="secret-codex",
            secret_rotation=1,
            issued_at=self.now,
            expires_at=self.now + 300,
        )

    def use(
        self,
        assignment,
        *,
        worker_id,
        fence,
        actor,
        consumer,
        subcommand=("app-server",),
    ):
        self.use_calls += 1
        delegation = self._delegation(assignment, worker_id, fence)
        return consumer(
            CodexDelegatedLaunch(
                delegation=delegation,
                command=(
                    "codex",
                    "--config",
                    'cli_auth_credentials_store="ephemeral"',
                    "--config",
                    "features.apps=false",
                    "--config",
                    'shell_environment_policy.inherit="none"',
                    "--config",
                    'shell_environment_policy.set={PATH="/usr/local/bin:/usr/bin:/bin",HOME="/tmp/codex-worker-home"}',
                    "--config",
                    'shell_environment_policy.filters.CODEX_ACCESS_TOKEN="exclude"',
                    *subcommand,
                ),
                environment={
                    "CODEX_HOME": CODEX_WORKER_HOME,
                    "CODEX_ACCESS_TOKEN": self.secret,
                },
            )
        )

    def validate_current(self, delegation, assignment, *, actor) -> None:
        self.validate_calls += 1
        if self.validation_error is not None:
            raise self.validation_error
        if assignment.id != delegation.assignment_id:
            raise CodexAuthDelegationStaleError("assignment changed")
        if assignment.fence != delegation.fence:
            raise CodexAuthDelegationStaleError("fence changed")
        if delegation.expires_at <= self.now:
            raise CodexAuthDelegationStaleError("credential expired")


class _FakeAlternateCredentialProvider(_FakeDelegationService):
    def __init__(self) -> None:
        super().__init__()
        self.secret = "alternate-runtime-token-do-not-persist"

    def use(
        self,
        assignment,
        *,
        worker_id,
        fence,
        actor,
        consumer,
    ):
        self.use_calls += 1
        delegation = self._delegation(assignment, worker_id, fence)
        return consumer(
            SimpleNamespace(
                delegation=delegation,
                command=("alternate-agent", "serve"),
                environment={"ALT_AGENT_TOKEN": self.secret},
            )
        )


class _FakeAlternateRuntime:
    def __init__(self, host, *, command, cwd, popen, metrics=None) -> None:
        self.host = host
        self.command = tuple(command)
        self.cwd = Path(cwd)
        self._popen = popen
        self.proc = None
        self.ready = asyncio.Event()
        self.requests = []
        self.approval_namespace = None

    async def start(self) -> None:
        self.proc = self._popen(
            list(self.command),
            cwd=str(self.cwd),
            stdin=None,
            stdout=None,
            stderr=None,
            text=True,
            bufsize=1,
        )
        self.ready.set()

    async def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
        self.ready.clear()

    async def request(self, method, params=None):
        self.requests.append((method, params))
        return {"runtime": "alternate", "method": method}

    async def notify(self, method, params=None) -> None:
        self.requests.append((method, params))

    async def respond_to_server_request(self, request_id, result) -> None:
        self.requests.append(("response", {"id": request_id, "result": result}))


class _FakeCodexRuntime:
    def __init__(self, host, *, command, cwd, popen, metrics=None) -> None:
        self.host = host
        self.command = tuple(command)
        self.cwd = Path(cwd)
        self._popen = popen
        self.proc = None
        self.restart_on_timeout = True
        self.ready = asyncio.Event()
        self.requests = []
        self.request_error = None

    async def start(self) -> None:
        self.proc = self._popen(
            list(self.command),
            cwd=str(self.cwd),
            stdin=None,
            stdout=None,
            stderr=None,
            text=True,
            bufsize=1,
        )
        self.ready.set()

    async def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
        self.ready.clear()

    async def request(self, method, params=None):
        self.requests.append((method, params))
        if self.request_error is not None:
            raise self.request_error
        return {"method": method, "params": params}

    async def notify(self, method, params=None) -> None:
        self.requests.append((method, params))

    async def respond_to_server_request(self, request_id, result) -> None:
        self.requests.append(("response", {"id": request_id, "result": result}))


class AssignmentBoundCodexSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.workspace_path = root / "workspace"
        self.workspace_path.mkdir()
        execution_venv = self.workspace_path / ".venv"
        (execution_venv / "bin").mkdir(parents=True)
        (execution_venv / "lib" / "python3.14" / "site-packages").mkdir(
            parents=True
        )
        (execution_venv / ".codex-web-execution-venv").write_text("test\n")

        sqlite = SQLiteStateStore(root / "state.sqlite3")
        self.identity = IdentityService(IdentityStateStore(sqlite))
        self.identity.bootstrap_local()
        self.admin = self.identity.local_trusted_actor()
        self.worker_actor = self.identity.bootstrap_service_actor(
            identity_id="execution-worker-codex-test",
            name="Execution Worker Codex Test",
            scope=self.admin.tenant,
            service_scopes=("execution-worker:run", "secret:use"),
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
                version="test-codex-session",
                capabilities=(
                    WorkerCapability.GIT,
                    WorkerCapability.COMMAND_EXECUTION,
                ),
            ),
            actor=self.admin,
        )
        self.backend = _FakeBackend()
        self.delegation = _FakeDelegationService()
        self.local_worker = LocalExecutionWorkerRuntime(
            self.worker_service,
            self.workspaces,
            self.backend,
            worker=self.worker,
            worker_actor=self.worker_actor,
            control_actor=self.admin,
            codex_auth_delegation=self.delegation,
            heartbeat_interval_seconds=5,
            renew_margin_seconds=200,
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _create_assignment(self, **overrides):
        values = {
            "work_item_ref": "group/app#codex",
            "execution_id": "exec-codex",
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
                memory_bytes=512 * 1024 * 1024,
                disk_bytes=1024 * 1024 * 1024,
                process_count=64,
                wall_seconds=600,
            ),
            "secret_refs": ("secret-codex",),
            "deadline_at": self.delegation.now + 600,
            "execution_workspace_id": "execws-codex",
        }
        values.update(overrides)
        return self.worker_service.create_assignment(
            ExecutionAssignmentCreate(**values),
            actor=self.admin,
        )

    async def _session(self, assignment, gate: asyncio.Event | None = None):
        async def sleep(_seconds):
            if gate is None:
                await asyncio.sleep(3600)
            else:
                await gate.wait()

        session = AssignmentBoundCodexSession(
            self.local_worker,
            SimpleNamespace(),
            assignment.id,
            runtime_factory=_FakeCodexRuntime,
            watchdog_interval_seconds=0.05,
            clock=lambda: self.delegation.now,
            monotonic=lambda: self.delegation.now,
            sleep=sleep,
        )
        await session.start()
        return session

    async def test_codex_session_mounts_coordinated_writable_repository_set(self) -> None:
        secondary = Path(self.temp.name) / "secondary-writable"
        secondary.mkdir()
        self.workspaces.workspace.resource_ids = ("repo-1", "repo-2")
        self.workspaces.workspace.repository_resource_id = "repo-1"
        self.workspaces.workspace.writable_repository_ids = ("repo-1", "repo-2")
        self.workspaces.workspace.repository_members = (
            SimpleNamespace(
                resource_id="repo-1",
                access_mode=LeaseMode.WRITE,
                workspace_path=str(self.workspace_path),
                sandbox_path=str(self.workspace_path),
            ),
            SimpleNamespace(
                resource_id="repo-2",
                access_mode=LeaseMode.WRITE,
                workspace_path=str(secondary),
                sandbox_path="/mnt/codex-repositories/repo-2",
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

        session = await self._session(assignment)
        try:
            launch = self.backend.spawned[0]
            self.assertEqual(
                launch["trusted_writable_mounts"],
                (
                    (
                        secondary.resolve(),
                        Path("/mnt/codex-repositories/repo-2"),
                    ),
                ),
            )
            self.assertEqual(
                launch["environment"]["CODEX_WRITABLE_REPOSITORIES"],
                "/mnt/codex-repositories/repo-2",
            )
        finally:
            await session.stop()

    async def test_codex_session_marks_primary_worktree_metadata_writable(self) -> None:
        git_metadata = Path(self.temp.name) / "repository.git"
        git_metadata.mkdir()
        self.backend.git_metadata = git_metadata.resolve()
        worktree_metadata = git_metadata / "worktrees" / "assignment"
        worktree_metadata.mkdir(parents=True)
        self.backend.git_worktree_metadata = worktree_metadata.resolve()
        assignment = self._create_assignment()

        session = await self._session(assignment)
        try:
            launch = self.backend.spawned[0]
            self.assertEqual(
                launch["environment"]["CODEX_WRITABLE_REPOSITORIES"],
                ":".join(
                    (
                        str(git_metadata.resolve()),
                        str(worktree_metadata.resolve()),
                    )
                ),
            )
            self.assertEqual(launch["trusted_writable_mounts"], ())
        finally:
            await session.stop()

    async def test_generic_process_session_launches_non_codex_runtime_under_same_assignment_boundary(self) -> None:
        assignment = self._create_assignment()
        provider = _FakeAlternateCredentialProvider()
        session = AssignmentBoundAgentProcessSession(
            self.local_worker,
            SimpleNamespace(),
            assignment.id,
            runtime_factory=_FakeAlternateRuntime,
            credential_provider=provider,
            watchdog_interval_seconds=60,
            clock=lambda: provider.now,
            monotonic=lambda: provider.now,
        )

        await session.start()
        try:
            persisted = next(
                item
                for item in self.worker_service.store.load().assignments
                if item.id == assignment.id
            )
            self.assertEqual(persisted.status, AssignmentStatus.RUNNING)
            self.assertEqual(persisted.assigned_worker_id, self.worker.id)
            self.assertEqual(session.fence, persisted.fence)
            self.assertIsInstance(session.runtime, _FakeAlternateRuntime)
            launch = self.backend.spawned[0]
            self.assertEqual(launch["argv"], ("alternate-agent", "serve"))
            self.assertEqual(
                launch["environment"]["ALT_AGENT_TOKEN"],
                provider.secret,
            )
            self.assertEqual(
                launch["environment"]["VIRTUAL_ENV"],
                str(self.workspace_path / ".venv"),
            )
            self.assertEqual(provider.use_calls, 1)
            result = await session.request("session/read")
            self.assertEqual(result["runtime"], "alternate")
            self.assertNotIn(provider.secret, repr(session.status().public()))
        finally:
            await session.stop()

    async def test_generic_process_session_rejects_runtime_binding_mismatch_before_launch(self) -> None:
        assignment = self._create_assignment(
            runtime_binding=ExecutionRuntimeBinding(
                provider_id="provider-a",
                runtime_id="runtime-a",
                capability_revision=3,
            )
        )
        provider = _FakeAlternateCredentialProvider()
        session = AssignmentBoundAgentProcessSession(
            self.local_worker,
            SimpleNamespace(),
            assignment.id,
            runtime_factory=_FakeAlternateRuntime,
            credential_provider=provider,
            runtime_binding=ExecutionRuntimeBinding(
                provider_id="provider-b",
                runtime_id="runtime-b",
                capability_revision=3,
            ),
            watchdog_interval_seconds=60,
            clock=lambda: provider.now,
            monotonic=lambda: provider.now,
        )

        with self.assertRaisesRegex(
            AssignmentBoundAgentProcessSessionStaleError,
            "runtime binding is incompatible",
        ):
            await session.start()

        self.assertEqual(self.backend.spawned, [])
        self.assertEqual(provider.use_calls, 0)

    async def test_generic_process_session_accepts_enriched_assignment_binding(self) -> None:
        provider = _FakeAlternateCredentialProvider()
        session = AssignmentBoundAgentProcessSession(
            self.local_worker,
            SimpleNamespace(),
            self._create_assignment(
                runtime_binding=ExecutionRuntimeBinding(
                    provider_id="provider-alt",
                    runtime_id="alternate",
                    capability_revision=1,
                    sandbox_profiles=("read-only", "workspace-write"),
                    authentication_mode="trusted_local_session",
                )
            ).id,
            runtime_factory=_FakeAlternateRuntime,
            credential_provider=provider,
            runtime_binding=ExecutionRuntimeBinding(
                provider_id="provider-alt",
                runtime_id="alternate",
                capability_revision=1,
            ),
            watchdog_interval_seconds=60,
            clock=lambda: provider.now,
            monotonic=lambda: provider.now,
        )

        await session.start()

        self.assertEqual(len(self.backend.spawned), 1)
        self.assertEqual(provider.use_calls, 1)
        await session.stop()

    async def test_trusted_local_dispatch_launches_ambient_codex_without_secrets(self) -> None:
        from codex_web.services.codex_worker_session import (
            TrustedLocalCodexCredentialProvider,
        )

        assignment = self._create_assignment(
            runtime_binding=ExecutionRuntimeBinding(
                provider_id="openai",
                runtime_id="codex",
                capability_revision=1,
                authentication_mode="trusted_local_session",
            )
        )
        codex_home = self.temp_path = self.workspace_path / "ambient-codex"
        codex_home.mkdir()
        executable_dir = self.workspace_path / "codex-bin"
        executable_dir.mkdir()
        executable = executable_dir / "codex"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        trusted = TrustedLocalCodexCredentialProvider(
            codex_home=codex_home,
            executable=str(executable),
        )
        self.local_worker.trusted_local_codex_delegation = trusted
        session = await self._session(assignment)
        try:
            launch = self.backend.spawned[0]
            self.assertEqual(launch["argv"][0], str(executable))
            self.assertIn("--config", launch["argv"])
            self.assertIn("features.apps=false", launch["argv"])
            self.assertTrue(
                any(
                    'CODEX_WEB_CONTROL_PLANE_URL="http://127.0.0.1:8788"'
                    in value
                    for value in launch["argv"]
                )
            )
            self.assertNotIn("features.code_mode=false", launch["argv"])
            self.assertNotIn("features.code_mode_host=false", launch["argv"])
            self.assertEqual(
                launch["minimum_address_space_bytes"],
                2 * 1024**4,
            )
            self.assertEqual(launch["minimum_process_count"], 4096)
            self.assertFalse(session.runtime.restart_on_timeout)
            self.assertEqual(launch["argv"][-1], "app-server")
            self.assertEqual(
                launch["environment"].get("CODEX_HOME"),
                str(codex_home),
            )
            self.assertFalse(launch["access_token_present"])
            writable_mounts = {
                (source, destination)
                for source, destination in launch["trusted_writable_mounts"]
            }
            self.assertIn((codex_home, codex_home), writable_mounts)
            self.assertIn((executable_dir, executable_dir), {
                (source, destination)
                for source, destination in launch["trusted_readonly_mounts"]
            })
            self.assertEqual(self.delegation.use_calls, 0)
        finally:
            await session.stop()

    async def test_dispatch_rejects_stale_trusted_local_grant(self) -> None:
        from codex_web.services.codex_worker_session import (
            DispatchingCodexCredentialProvider,
            TrustedLocalCodexCredentialProvider,
        )

        assignment = self._create_assignment(
            runtime_binding=ExecutionRuntimeBinding(
                provider_id="openai",
                runtime_id="codex",
                capability_revision=1,
                authentication_mode="trusted_local_session",
            )
        )
        trusted = TrustedLocalCodexCredentialProvider(
            codex_home=self.workspace_path,
            executable="/usr/bin/codex",
        )
        dispatch = DispatchingCodexCredentialProvider(
            delegated=self.delegation,
            trusted_local=trusted,
        )
        claimed = self.worker_service.claim(
            self.worker.id,
            AssignmentClaimRequest(lease_seconds=600),
            actor=self.worker_actor,
            assignment_id=assignment.id,
        )
        started = self.worker_service.start(
            self.worker.id,
            assignment.id,
            AssignmentStartRequest(
                lease_token=claimed.lease.lease_token,
                fence=claimed.fence,
            ),
            actor=self.worker_actor,
        )
        grant = trusted._grant(
            started,
            worker_id=self.worker.id,
            fence=started.fence,
        )

        with self.assertRaisesRegex(
            AssignmentBoundCodexSessionStaleError,
            "lease/fence is stale",
        ):
            dispatch.validate_current(
                dataclasses.replace(grant, fence=grant.fence + 1),
                started,
                actor=self.worker_actor,
            )
        self.delegation.now += 10
        dispatch.validate_current(
            grant,
            started,
            actor=self.worker_actor,
        )

    async def test_generic_process_session_stops_when_runtime_revision_changes(self) -> None:
        expected = ExecutionRuntimeBinding(
            provider_id="provider-alt",
            runtime_id="alternate",
            capability_revision=4,
        )
        assignment = self._create_assignment(runtime_binding=expected)
        provider = _FakeAlternateCredentialProvider()
        gate = asyncio.Event()

        async def sleep(_seconds):
            await gate.wait()

        session = AssignmentBoundAgentProcessSession(
            self.local_worker,
            SimpleNamespace(),
            assignment.id,
            runtime_factory=_FakeAlternateRuntime,
            credential_provider=provider,
            runtime_binding=expected,
            watchdog_interval_seconds=0.05,
            clock=lambda: provider.now,
            monotonic=lambda: provider.now,
            sleep=sleep,
        )
        await session.start()

        def change_runtime_binding(state):
            for index, item in enumerate(state.assignments):
                if item.id == assignment.id:
                    state.assignments[index] = item.model_copy(
                        update={
                            "runtime_binding": ExecutionRuntimeBinding(
                                provider_id="provider-alt",
                                runtime_id="alternate",
                                capability_revision=5,
                            )
                        }
                    )
            return state

        self.worker_service.store.update(change_runtime_binding)
        gate.set()
        await asyncio.wait_for(session.watchdog_task, timeout=1)

        self.assertTrue(self.backend.processes[0].terminated)
        self.assertIn("runtime binding changed", session.last_error)

    async def test_default_session_factory_reuses_canonical_codex_runtime(self) -> None:
        assignment = self._create_assignment()
        session = AssignmentBoundCodexSession(
            self.local_worker,
            SimpleNamespace(),
            assignment.id,
        )
        self.assertIs(session.runtime_factory, CodexRuntime)
        self.assertIsNone(session.runtime)

    async def test_session_claims_starts_and_launches_codex_inside_worker_boundary(self) -> None:
        assignment = self._create_assignment()
        session = await self._session(assignment)
        try:
            persisted = next(
                item
                for item in self.worker_service.store.load().assignments
                if item.id == assignment.id
            )
            self.assertEqual(persisted.status, AssignmentStatus.RUNNING)
            self.assertEqual(persisted.assigned_worker_id, self.worker.id)
            self.assertEqual(session.fence, persisted.fence)
            self.assertIsInstance(session.runtime, _FakeCodexRuntime)
            self.assertTrue(session.status().ready)
            self.assertEqual(self.backend.validated, [assignment.id])
            launch = self.backend.spawned[0]
            self.assertEqual(launch["assignment_id"], assignment.id)
            self.assertEqual(launch["workspace_path"], self.workspace_path)
            self.assertEqual(launch["argv"][0], "codex")
            self.assertEqual(launch["argv"][-1], "app-server")
            self.assertIn('cli_auth_credentials_store="ephemeral"', launch["argv"])
            self.assertIn("features.apps=false", launch["argv"])
            self.assertNotIn("features.code_mode=false", launch["argv"])
            self.assertNotIn("features.code_mode_host=false", launch["argv"])
            self.assertIn(
                'shell_environment_policy.filters.CODEX_ACCESS_TOKEN="exclude"',
                launch["argv"],
            )
            self.assertEqual(
                launch["environment_keys"],
                (
                    "CODEX_ACCESS_TOKEN",
                    "CODEX_HOME",
                    "PATH",
                    "PIP_DISABLE_PIP_VERSION_CHECK",
                    "PIP_REQUIRE_VIRTUALENV",
                    "PYTHONNOUSERSITE",
                    "VIRTUAL_ENV",
                ),
            )
            self.assertTrue(launch["access_token_present"])
            self.assertNotIn(self.delegation.secret, repr(session.status().public()))
        finally:
            await session.stop()

    async def test_explicit_runtime_credential_provider_isolated_from_worker_default(self) -> None:
        assignment = self._create_assignment()
        injected = _FakeDelegationService()
        injected.secret = "runtime-specific-token-do-not-persist"
        session = AssignmentBoundCodexSession(
            self.local_worker,
            SimpleNamespace(),
            assignment.id,
            runtime_factory=_FakeCodexRuntime,
            credential_provider=injected,
            watchdog_interval_seconds=60,
            clock=lambda: injected.now,
            monotonic=lambda: injected.now,
        )

        await session.start()
        try:
            launch = self.backend.spawned[0]
            self.assertEqual(injected.use_calls, 1)
            self.assertEqual(self.delegation.use_calls, 0)
            self.assertEqual(
                launch["environment"]["CODEX_ACCESS_TOKEN"],
                injected.secret,
            )
            self.assertNotEqual(
                launch["environment"]["CODEX_ACCESS_TOKEN"],
                self.delegation.secret,
            )
            session.validate_current()
            self.assertGreaterEqual(injected.validate_calls, 1)
            self.assertEqual(self.delegation.validate_calls, 0)
            self.assertNotIn(injected.secret, repr(session.status().public()))
        finally:
            await session.stop()

    async def test_disk_validation_is_cached_between_broker_checks(self) -> None:
        assignment = self._create_assignment()
        session = await self._session(assignment)
        try:
            session.validate_current()
            session.validate_current()
            session._validate_egress_state()
            self.assertEqual(self.backend.disk_usage_calls, 1)

            self.delegation.now += session.disk_validation_interval_seconds
            session.validate_current()
            self.assertEqual(self.backend.disk_usage_calls, 2)
        finally:
            await session.stop()

    async def test_session_brokers_model_egress_without_sharing_worker_network(self) -> None:
        assignment = self._create_assignment()

        async def sleep(_seconds):
            await asyncio.sleep(3600)

        session = AssignmentBoundCodexSession(
            self.local_worker,
            SimpleNamespace(),
            assignment.id,
            runtime_factory=_FakeCodexRuntime,
            watchdog_interval_seconds=0.05,
            egress_endpoints_resolver=lambda: (
                CodexModelEgressEndpoint("models.example.test", 443),
            ),
            clock=lambda: self.delegation.now,
            monotonic=lambda: self.delegation.now,
            sleep=sleep,
        )
        await session.start()
        broker = session.egress_broker
        self.assertIsNotNone(broker)
        broker_root = broker.mount_source
        try:
            with patch.object(session, "_heartbeat_and_renew") as renew:
                validated = session._validate_egress_state()
            self.assertEqual(validated.id, assignment.id)
            renew.assert_not_called()
            launch = self.backend.spawned[0]
            self.assertEqual(launch["argv"][:3], ("/usr/bin/python3", "-u", "-c"))
            self.assertEqual(launch["argv"][-1], "app-server")
            joined_argv = " ".join(launch["argv"])
            self.assertIn(
                f'VIRTUAL_ENV="{self.workspace_path}/.venv"',
                joined_argv,
            )
            self.assertIn(
                f'PATH="{self.workspace_path}/.venv/bin:/usr/local/bin:/usr/bin:/bin"',
                joined_argv,
            )
            self.assertIn("HTTPS_PROXY", launch["environment"])
            self.assertIn("HTTP_PROXY", launch["environment"])
            self.assertIn("127.0.0.1:8787", launch["environment"]["HTTPS_PROXY"])
            self.assertNotIn(
                launch["environment"]["HTTPS_PROXY"],
                repr(session.status().public()),
            )
            self.assertEqual(
                launch["trusted_readonly_mounts"],
                ((broker_root, Path("/run/agent-model-egress")),),
            )
            self.assertTrue(broker_root.exists())
        finally:
            await session.stop()

        self.assertFalse(broker_root.exists())

    async def test_session_renews_exact_fenced_assignment_lease(self) -> None:
        assignment = self._create_assignment()
        session = await self._session(assignment)
        try:
            current = session.validate_current()
            old_expiry = current.lease.expires_at
            renewed = session._heartbeat_and_renew(current)
            self.assertIsNotNone(renewed.lease)
            self.assertEqual(renewed.fence, session.fence)
            self.assertGreaterEqual(renewed.lease.expires_at, old_expiry)
            self.assertIsNotNone(renewed.lease.renewed_at)
        finally:
            await session.stop()

    async def test_watchdog_terminates_on_stale_fence(self) -> None:
        assignment = self._create_assignment()
        gate = asyncio.Event()
        session = await self._session(assignment, gate)

        def make_stale(state):
            for index, item in enumerate(state.assignments):
                if item.id == assignment.id:
                    state.assignments[index] = item.model_copy(
                        update={
                            "fence": item.fence + 1,
                            "lease": item.lease.model_copy(
                                update={"fence": item.lease.fence + 1}
                            ),
                        }
                    )
            return state

        self.worker_service.store.update(make_stale)
        gate.set()
        await asyncio.wait_for(session.watchdog_task, timeout=1)

        self.assertTrue(self.backend.processes[0].terminated)
        self.assertIn("lease/fence changed", session.last_error)

    async def test_watchdog_fails_assignment_when_runtime_process_exits(self) -> None:
        assignment = self._create_assignment()
        gate = asyncio.Event()
        session = await self._session(assignment, gate)
        self.backend.processes[0].returncode = 1

        gate.set()
        await asyncio.wait_for(session.watchdog_task, timeout=1)

        current = self.worker_service.store.assignment(assignment.id)
        self.assertEqual(current.status, AssignmentStatus.FAILED)
        self.assertIsNone(current.lease)
        self.assertEqual(
            current.failure_code,
            "agent_runtime_process_exited",
        )
        self.assertIn("process exited", session.last_error)

    async def test_rpc_timeout_retires_one_shot_assignment(self) -> None:
        assignment = self._create_assignment()
        session = await self._session(assignment)
        session.runtime.request_error = HTTPException(
            status_code=504,
            detail="turn/start timed out after 60s",
        )

        with self.assertRaises(HTTPException) as raised:
            await session.request("turn/start", {"threadId": "thread-1"})

        self.assertEqual(raised.exception.status_code, 504)
        current = self.worker_service.store.assignment(assignment.id)
        self.assertEqual(current.status, AssignmentStatus.FAILED)
        self.assertIsNone(current.lease)
        self.assertEqual(current.failure_code, "agent_runtime_rpc_timeout")
        self.assertTrue(self.backend.processes[0].terminated)
        self.assertFalse(session.runtime.ready.is_set())

    async def test_interrupt_timeout_preserves_running_assignment(self) -> None:
        assignment = self._create_assignment()
        session = await self._session(assignment)
        session.runtime.request_error = HTTPException(status_code=504, detail="turn/interrupt timed out after 10s")
        with self.assertRaises(HTTPException):
            await session.request("turn/interrupt", {"threadId": "thread-1"})
        current = self.worker_service.store.assignment(assignment.id)
        self.assertEqual(current.status, AssignmentStatus.RUNNING)
        self.assertIsNotNone(current.lease)
        self.assertFalse(self.backend.processes[0].terminated)
        await session.stop()

    async def test_thread_read_timeout_preserves_one_shot_assignment(self) -> None:
        assignment = self._create_assignment()
        session = await self._session(assignment)
        session.runtime.request_error = HTTPException(
            status_code=504,
            detail="thread/read timed out after 10s",
        )

        with self.assertRaises(HTTPException) as raised:
            await session.request("thread/read", {"threadId": "thread-1"})

        self.assertEqual(raised.exception.status_code, 504)
        current = self.worker_service.store.assignment(assignment.id)
        self.assertEqual(current.status, AssignmentStatus.RUNNING)
        self.assertIsNotNone(current.lease)
        self.assertFalse(self.backend.processes[0].terminated)
        self.assertTrue(session.runtime.ready.is_set())
        await session.stop()

    async def test_watchdog_terminates_when_worker_is_quarantined(self) -> None:
        assignment = self._create_assignment()
        gate = asyncio.Event()
        session = await self._session(assignment, gate)
        self.worker_service.set_lifecycle(
            self.worker.id,
            WorkerLifecycle.QUARANTINED,
            actor=self.admin,
            reason="test quarantine",
        )
        gate.set()
        await asyncio.wait_for(session.watchdog_task, timeout=1)

        self.assertTrue(self.backend.processes[0].terminated)
        self.assertIn("quarantined", session.last_error)

    async def test_watchdog_terminates_when_delegated_credential_rotates(self) -> None:
        assignment = self._create_assignment()
        gate = asyncio.Event()
        session = await self._session(assignment, gate)
        self.delegation.validation_error = CodexAuthDelegationStaleError(
            "credential rotated or changed"
        )
        gate.set()
        await asyncio.wait_for(session.watchdog_task, timeout=1)

        self.assertTrue(self.backend.processes[0].terminated)
        self.assertIn("credential rotated or changed", session.last_error)

    async def test_watchdog_terminates_when_delegated_credential_expires(self) -> None:
        assignment = self._create_assignment()
        gate = asyncio.Event()
        session = await self._session(assignment, gate)
        self.delegation.validation_error = CodexAuthDelegationStaleError(
            "credential expired"
        )
        gate.set()
        await asyncio.wait_for(session.watchdog_task, timeout=1)

        self.assertTrue(self.backend.processes[0].terminated)
        self.assertIn("credential expired", session.last_error)

    async def test_manager_pending_bootstrap_cannot_resume_and_cancel_a_raced_same_worker_claim(self) -> None:
        subject = ExecutionSubject(kind="thread_bootstrap", ref="bootstrap-raced")
        self.workspaces.workspace.subject = subject
        self.workspaces.workspace.work_item_ref = None
        self.workspaces.release = lambda *args, **kwargs: self.fail("must not release raced claim")
        assignment = self._create_assignment(subject=subject, work_item_ref=None)
        lookup = self.local_worker._pending_assignment
        calls = 0
        def racing_lookup(key):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.worker_service.claim(
                    self.worker.id, AssignmentClaimRequest(),
                    actor=self.worker_actor, assignment_id=key,
                )
            return lookup(key)
        manager = AssignmentBoundCodexSessionManager(
            self.local_worker, SimpleNamespace(), runtime_factory=_FakeCodexRuntime,
            watchdog_interval_seconds=60,
        )
        with patch.object(self.local_worker, "_pending_assignment", side_effect=racing_lookup):
            with self.assertRaisesRegex(WorkerConflictError, "fence changed") as caught:
                await manager.start(assignment.id)
        self.assertIn("another claim", str(caught.exception.__cause__))
        current = self.worker_service.store.assignment(assignment.id)
        self.assertEqual(current.status, AssignmentStatus.CLAIMED)
        self.assertEqual(current.fence, 1)
        self.assertIsNotNone(current.lease)
        self.assertEqual(self.backend.processes, [])

    async def test_atomic_claim_rejects_same_and_foreign_worker_races_after_pending_snapshot(self) -> None:
        other_actor = self.identity.bootstrap_service_actor(
            identity_id="raced-worker", name="Raced worker", scope=self.admin.tenant,
            service_scopes=("execution-worker:run",),
        )
        other_worker = self.worker_service.register(
            ExecutionWorkerRegister(service_identity_id=other_actor.identity_id,
                                    pool="local", version="test",
                                    capabilities=self.worker.capabilities),
            actor=self.admin,
        )
        for label, worker, actor in [("same", self.worker, self.worker_actor), ("foreign", other_worker, other_actor)]:
            with self.subTest(claimant=label):
                subject = ExecutionSubject(kind="thread_bootstrap", ref=f"bootstrap-atomic-{label}")
                self.workspaces.workspace.subject = subject
                self.workspaces.workspace.work_item_ref = None
                self.workspaces.workspace.execution_id = f"exec-atomic-{label}"
                self.workspaces.release = lambda *args, **kwargs: self.fail("must retain the raced claimant reservation")
                assignment = self._create_assignment(subject=subject, work_item_ref=None,
                                                     execution_id=self.workspaces.workspace.execution_id)
                entered = threading.Event()
                release = threading.Event()
                original_claim = self.local_worker._claim_or_resume
                def paused_claim(snapshot):
                    self.assertEqual(snapshot.status, AssignmentStatus.PENDING)
                    entered.set()
                    if not release.wait(10):
                        raise RuntimeError("claim race barrier timed out")
                    return original_claim(snapshot)
                manager = AssignmentBoundCodexSessionManager(
                    self.local_worker, SimpleNamespace(), runtime_factory=_FakeCodexRuntime,
                    watchdog_interval_seconds=60,
                )
                with patch.object(self.local_worker, "_claim_or_resume", side_effect=paused_claim):
                    task = asyncio.create_task(manager.start(assignment.id))
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                        winner = await asyncio.to_thread(
                            self.worker_service.claim, worker.id, AssignmentClaimRequest(),
                            actor=actor, assignment_id=assignment.id,
                        )
                        self.assertIsNotNone(winner)
                        release.set()
                        with self.assertRaisesRegex(WorkerConflictError, "fence changed"):
                            await task
                    finally:
                        release.set()
                        await asyncio.gather(task, return_exceptions=True)
                self.assertEqual(self.worker_service.store.assignment(assignment.id), winner)
                self.assertEqual(self.backend.processes, [])
                self.assertIsNone(manager.get(assignment.id))

    async def test_manager_runtime_start_failure_cancels_unregistered_bootstrap(self) -> None:
        subject = ExecutionSubject(kind="thread_bootstrap", ref="bootstrap-failure")
        self.workspaces.workspace.subject = subject
        self.workspaces.workspace.work_item_ref = None
        released = []
        self.workspaces.release = lambda key, request, **kwargs: released.append((key, kwargs))
        assignment = self._create_assignment(subject=subject, work_item_ref=None)
        class FailedRuntime(_FakeCodexRuntime):
            async def start(self):
                await super().start()
                raise RuntimeError("runtime handshake failed")
        manager = AssignmentBoundCodexSessionManager(
            self.local_worker, SimpleNamespace(), runtime_factory=FailedRuntime,
            watchdog_interval_seconds=60,
        )
        with self.assertRaisesRegex(RuntimeError, "runtime handshake failed"):
            await manager.start(assignment.id)
        current = self.worker_service.store.assignment(assignment.id)
        self.assertEqual(current.status, AssignmentStatus.CANCELLED)
        self.assertIsNone(current.lease)
        self.assertEqual(current.fence, 2)
        self.assertIsNone(manager.get(assignment.id))
        self.assertTrue(self.backend.processes[0].terminated)
        self.assertEqual(released[0][1]["actor"], self.admin)
        self.assertTrue(released[0][1]["preserve_files"])

    def _install_real_owner_workspace(self):
        store = ExecutionWorkspaceStateStore(self.worker_service.store.store)
        now = time.time()
        workspace = ExecutionWorkspace(
            id="execws-codex", organization_id=self.admin.organization_id,
            workspace_id=self.admin.workspace_id, work_item_ref="group/app#codex",
            execution_id="exec-codex", project_id="home", owner_identity_id=self.admin.identity_id,
            kind=ExecutionWorkspaceKind.GIT_WORKTREE, resource_ids=("repo-1",),
            repository_resource_id="repo-1", writable_repository_ids=("repo-1",),
            lease_id="lease-codex", path=str(self.workspace_path),
            branch_name="retained-branch", base_revision="abc123", head_revision="abc123",
            status=ExecutionWorkspaceStatus.ACTIVE, created_at=now, updated_at=now)
        lease = ExecutionWorkspaceLease(
            id=workspace.lease_id, execution_workspace_id=workspace.id,
            organization_id=self.admin.organization_id, workspace_id=self.admin.workspace_id,
            work_item_ref=workspace.work_item_ref, execution_id=workspace.execution_id,
            owner_identity_id=self.admin.identity_id, resource_ids=workspace.resource_ids,
            mode=LeaseMode.WRITE, acquired_at=now, expires_at=now+3600)
        def insert(state):
            state.workspaces.append(workspace)
            state.leases.append(lease)
            return state
        store.update(insert)
        service = ExecutionWorkspaceService(store, SimpleNamespace(), None, lambda _: None)
        service._cleanup_git_workspace = Mock(side_effect=AssertionError("lifecycle must retain every file"))
        self.workspaces = service
        self.local_worker.workspace_service = service
        self.worker_service.workspaces = service
        for name, body in (("untracked.txt", b"uncommitted evidence\x00"),
                           (".ignored-artifact", b"ignored evidence\xff")):
            (self.workspace_path/name).write_bytes(body)
        (self.workspace_path/".gitignore").write_text(".ignored-artifact\n")
        return service, {path.relative_to(self.workspace_path):path.read_bytes()
                         for path in self.workspace_path.rglob("*") if path.is_file()}

    def _assert_owner_release_preserved_files(self, service, before):
        state = service.store.load()
        workspace = next(w for w in state.workspaces if w.id=="execws-codex")
        lease = next(l for l in state.leases if l.id=="lease-codex")
        self.assertEqual(workspace.status, ExecutionWorkspaceStatus.RELEASED)
        self.assertIsNotNone(lease.released_at)
        self.assertIsNone(workspace.cleaned_at)
        self.assertEqual((workspace.branch_name, workspace.head_revision), ("retained-branch", "abc123"))
        service._cleanup_git_workspace.assert_not_called()
        event = next(e for e in state.events if e.event_type=="workspace_released")
        self.assertEqual(event.actor_identity_id, self.admin.identity_id)
        self.assertEqual(event.details, {"discard":False, "preserve_files":True})
        self.assertEqual(before, {path.relative_to(self.workspace_path):path.read_bytes()
                                for path in self.workspace_path.rglob("*") if path.is_file()})
        self.assertNotIn("execution-workspace:admin", self.worker_actor.service_scopes)

    async def test_completion_releases_owner_workspace_with_real_authorization_and_retains_files(self):
        service, before = self._install_real_owner_workspace()
        with self.assertRaises(AuthorizationError):
            service._authorized(service.get("execws-codex", self.admin), self.worker_actor)
        assignment = self._create_assignment()
        manager = AssignmentBoundCodexSessionManager(
            self.local_worker, SimpleNamespace(), runtime_factory=_FakeCodexRuntime,
            watchdog_interval_seconds=60)
        await manager.start(assignment.id)
        # Isolate the unrelated Git checkpoint machinery; authorization,
        # assignment completion, lease transition and workspace audit are real.
        with patch.object(manager, "checkpoint", new_callable=AsyncMock) as checkpoint:
            with patch.object(self.worker_service, "complete", wraps=self.worker_service.complete) as completed_call:
                completed = await manager.complete(assignment.id, succeeded=True)
            checkpoint.assert_awaited_once_with(assignment.id)
        self.assertIs(completed_call.call_args.kwargs["actor"], self.worker_actor)
        self.assertEqual(completed.status, AssignmentStatus.SUCCEEDED)
        self.assertIsNone(completed.lease)
        self._assert_owner_release_preserved_files(service, before)

    async def test_runtime_failure_releases_owner_workspace_and_keeps_worker_completion_identity(self):
        service, before = self._install_real_owner_workspace()
        assignment = self._create_assignment()
        session = await self._session(assignment)
        try:
            with patch.object(self.worker_service, "complete", wraps=self.worker_service.complete) as completed_call:
                session._record_runtime_failure(
                    failure_code="agent_runtime_process_exited", failure_message="actual process ended",
                    release_reason="agent runtime process exited")
            self.assertIs(completed_call.call_args.kwargs["actor"], self.worker_actor)
            completed = self.worker_service.store.assignment(assignment.id)
            self.assertEqual(completed.status, AssignmentStatus.FAILED)
            self.assertIsNone(completed.lease)
            self._assert_owner_release_preserved_files(service, before)
        finally:
            await session.stop()

    async def test_manager_completes_exact_fenced_assignment_and_stops_session(self) -> None:
        assignment = self._create_assignment()
        manager = AssignmentBoundCodexSessionManager(
            self.local_worker,
            SimpleNamespace(),
            runtime_factory=_FakeCodexRuntime,
            watchdog_interval_seconds=60,
        )

        session = await manager.start(assignment.id)
        self.assertEqual(
            next(
                item.status
                for item in self.worker_service.store.load().assignments
                if item.id == assignment.id
            ),
            AssignmentStatus.RUNNING,
        )

        completed = await manager.complete(
            assignment.id,
            succeeded=True,
            artifact_ids=("artifact-1",),
            evidence_ids=("evidence-1",),
        )

        self.assertEqual(completed.status, AssignmentStatus.SUCCEEDED)
        self.assertEqual(completed.artifact_ids, ("artifact-1",))
        self.assertEqual(completed.evidence_ids, ("evidence-1",))
        self.assertIsNone(completed.lease)
        self.assertIsNone(manager.get(assignment.id))
        self.assertTrue(self.backend.processes[0].terminated)
        self.assertEqual(session.fence, completed.fence)

    async def test_request_reuses_existing_codex_runtime_protocol_owner(self) -> None:
        assignment = self._create_assignment()
        session = await self._session(assignment)
        try:
            result = await session.request("thread/list", {"limit": 1})
            self.assertEqual(result["method"], "thread/list")
            self.assertEqual(
                session.runtime.requests[-1],
                ("thread/list", {"limit": 1}),
            )
        finally:
            await session.stop()


if __name__ == "__main__":
    unittest.main()
