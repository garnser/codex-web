from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.execution_workers import (
    AssignmentStatus,
    ExecutionAssignmentCreate,
    ExecutionRuntimeBinding,
    ExecutionWorkerRegister,
    NetworkPolicy,
    WorkerCapability,
    WorkerLifecycle,
    WorkerResourceLimits,
)
from codex_web.execution_workspaces import ExecutionWorkspaceStatus, LeaseMode
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
from codex_web.services.execution_workers import ExecutionWorkerService
from codex_web.services.identity import IdentityService
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
        self.processes: list[_FakeProcess] = []

    def validate_assignment(self, assignment) -> None:
        self.validated.append(assignment.id)

    def discover_git_metadata(self, workspace_path):
        return None

    def execution_disk_usage(self, workspace_path, git_metadata_path=None):
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
                    'shell_environment_policy.inherit="none"',
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
        self.ready = asyncio.Event()
        self.requests = []

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
                launch["environment"],
                {"ALT_AGENT_TOKEN": provider.secret},
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
            self.assertIn(
                'shell_environment_policy.filters.CODEX_ACCESS_TOKEN="exclude"',
                launch["argv"],
            )
            self.assertEqual(
                launch["environment_keys"],
                ("CODEX_ACCESS_TOKEN", "CODEX_HOME"),
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
            launch = self.backend.spawned[0]
            self.assertEqual(launch["argv"][:3], ("/usr/bin/python3", "-u", "-c"))
            self.assertEqual(launch["argv"][-1], "app-server")
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
