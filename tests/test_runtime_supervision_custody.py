"""Bounded faults on real supervision, canonical SQLite and worker transitions."""
import asyncio
import json
import os
import signal
import sqlite3
import hashlib
import sys
import subprocess
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from codex_web.runtime.codex import CodexRuntime
from codex_web.runtime import managed_validation_runner
from codex_web.services.codex_worker_session import AssignmentBoundCodexSession

import httpx
import tempfile
from fastapi import FastAPI

from codex_web.api.system import build_system_router
from codex_web.artifact_evidence import EvidenceResult
from codex_web.execution_workers import AssignmentClaimRequest, AssignmentStartRequest, AssignmentStatus, AssignmentCompleteRequest
from codex_web.services.execution_workers import WorkerLeaseError
from codex_web.local_execution_backend import BubblewrapExecutionBackend
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from tests import test_codex_worker_session as fixture
from tests import test_agent_runtime_telemetry as usage_fixture
from codex_web.api.agent_runtime_usage import build_agent_runtime_usage_router
from tests import test_execution_workspaces as workspace_fixture
from codex_web.execution_workspace_backend import LocalGitWorkspaceBackend
from codex_web.execution_workspaces import ExecutionWorkspaceRelease
from codex_web.services.artifact_content import ArtifactContentRegistry, ArtifactContentService
from codex_web.services.local_artifact_content import LocalArtifactContentStore


class SupervisionCustodyTests(unittest.IsolatedAsyncioTestCase):
    _create_assignment = fixture.AssignmentBoundCodexSessionTests._create_assignment
    _session = fixture.AssignmentBoundCodexSessionTests._session

    async def asyncSetUp(self):
        await fixture.AssignmentBoundCodexSessionTests.asyncSetUp(self)
        self.evidence = ArtifactEvidenceService(ArtifactEvidenceStore(self.worker_service.store.store))
        self.local_worker.artifact_evidence = self.evidence
        self.sessions = []

    async def asyncTearDown(self):
        for session in self.sessions:
            await session.stop()
            # Bounded stop does not pretend cooperative executor work has ended.
            # Keep fixture SQLite alive until every tracked operation exits.
            pending = list(session._executor_work)
            if pending:
                await asyncio.wait_for(asyncio.gather(*[asyncio.wrap_future(work) for work in pending],
                    return_exceptions=True), 2)
            self.assertFalse(session._executor_work, "fixture storage still has an owned executor")
        await fixture.AssignmentBoundCodexSessionTests.asyncTearDown(self)

    async def session(self, **overrides):
        gate = asyncio.Event()
        session = await self._session(self._create_assignment(**overrides), gate)
        self.sessions.append(session)
        return session, gate

    def observations(self):
        return [e for e in self.worker_service.events(self.admin)
                if e.event_type == "agent_runtime_supervision_failed"]

    def short_lease(self, assignment_id, *, deadline=False):
        def update(state):
            item = state.assignments[0]
            values = {"lease": item.lease.model_copy(update={"expires_at": time.time() + .15})}
            if deadline:
                values = {"deadline_at": time.time() + .15}
            state.assignments[0] = item.model_copy(update=values)
            return state
        current = self.worker_service.store.assignment(assignment_id)
        self.worker_service.store.update_assignment(self.worker.id, assignment_id, update)
        return self.worker_service.store.assignment(assignment_id)

    async def test_native_rpc_interrupted_managed_validation_is_captured_without_manual_upload(self):
        registry = ArtifactContentRegistry()
        registry.register(LocalArtifactContentStore(Path(self.temp.name)/'managed-content'))
        self.evidence.content = ArtifactContentService(registry)
        source = self.workspace_path/'source.py'
        source.write_text('original source\n')
        subprocess.run(['git','init',str(self.workspace_path)],check=True,capture_output=True)
        subprocess.run(['git','-C',str(self.workspace_path),'add','source.py'],check=True,capture_output=True)
        subprocess.run(['git','-C',str(self.workspace_path),'-c','user.name=Fixture',
            '-c','user.email=fixture@example.invalid','commit','-m','source'],check=True,capture_output=True)
        source.write_text('real uncommitted source retained after native loss\n')
        expected = hashlib.sha256(source.read_bytes()).hexdigest()
        # The transport is the real CodexRuntime. Only native executable launch
        # is replaced with a controlled JSON-RPC fixture; no model/provider runs.
        with tempfile.TemporaryDirectory() as ephemeral:
            marker = Path(ephemeral)/'started'
            server = Path(self.temp.name)/'native_fixture.py'
            server.write_text("""
import json,os,subprocess,sys
for line in sys.stdin:
    request=json.loads(line)
    if 'id' not in request: continue
    if request['method']=='fixture/startValidation':
        child=subprocess.Popen([sys.executable,sys.argv[1],'--source','source.py','--',
            sys.executable,'-c',"import pathlib,time,os; pathlib.Path(os.environ['FIXTURE_STARTED']).write_text(str(os.getpid())); time.sleep(30)"])
        result={'pid':child.pid}
    else: result={}
    print(json.dumps({'id':request['id'],'result':result}),flush=True)
""")
            def spawn(assignment, *, environment, **kwargs):
                self.assertIn('CODEX_WEB_MANAGED_VALIDATION_DIR',environment)
                self.assertEqual(environment['CODEX_WEB_MANAGED_VALIDATION_RUNNER'],'/opt/codex-managed-validation.py')
                child_env = {'PATH':os.environ['PATH'],'FIXTURE_STARTED':str(marker),
                    'CODEX_WEB_MANAGED_VALIDATION_DIR':environment['CODEX_WEB_MANAGED_VALIDATION_DIR']}
                return subprocess.Popen([sys.executable,str(server),managed_validation_runner.__file__],
                    cwd=self.workspace_path,env=child_env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,text=True,start_new_session=True)
            self.backend.spawn_interactive = spawn
            self.backend.terminate_process = BubblewrapExecutionBackend._kill_process_group
            gate = asyncio.Event()
            async def sleep(_): await gate.wait()
            session = AssignmentBoundCodexSession(self.local_worker,SimpleNamespace(hub=SimpleNamespace(publish=AsyncMock())),
                self._create_assignment().id,runtime_factory=CodexRuntime,watchdog_interval_seconds=.05,sleep=sleep)
            self.sessions.append(session)
            await session.start()
            self.assertEqual(session.managed_validation.state,'registered')
            await session.request('fixture/startValidation')
            for _ in range(200):
                if marker.exists(): break
                await asyncio.sleep(.005)
            self.assertTrue(marker.exists(),'actual validation command never began')
            validation_pid = int(marker.read_text())
            process = session._owned_process
            os.killpg(process.pid,signal.SIGKILL)
            await asyncio.to_thread(process.wait,2)
            gate.set()
            await asyncio.wait_for(session.watchdog_task,3)
            self.assertEqual(session.managed_validation.state,'captured')
            self.assertFalse(session.status().running)
            child = Path(f'/proc/{validation_pid}/stat')
            self.assertTrue(not child.exists() or child.read_text().split()[2]=='Z')
        await session.stop()
        for pipe in (process.stdin,process.stdout,process.stderr):
            pipe.close()
        self.assertFalse(marker.exists(),'ephemeral namespace must actually disappear')
        reopened = ArtifactEvidenceService(ArtifactEvidenceStore(self.worker_service.store.store),
            content=ArtifactContentService(registry))
        captures = [a for a in reopened.list_artifacts(self.admin) if a.source=='native-validation-capture']
        self.assertEqual(len(captures),1)
        artifact = captures[0]
        _, content = reopened.open_artifact_content(artifact.id,actor=self.admin)
        report = json.loads(b''.join(content))
        self.assertEqual(report['sources'],[{'path':'source.py','sha256':expected}])
        self.assertEqual((report['state'],report['exit_code'],report['finished_at']),('running',None,None))
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),expected)
        self.assertEqual(artifact.metadata['fence'],session.fence)
        observations = [e for e in reopened.list_evidence(self.admin) if e.source=='native-validation-capture']
        self.assertEqual(len(observations),1)
        self.assertEqual(observations[0].result,EvidenceResult.INFO)
        self.assertEqual(observations[0].metadata['result'],'unknown')
        self.assertNotIn(str(marker),json.dumps(report))
        self.assertNotIn('time.sleep',json.dumps(report))

    async def test_registered_managed_capture_and_cleanup_are_shared_by_repeated_concurrent_stop(self):
        registry = ArtifactContentRegistry()
        registry.register(LocalArtifactContentStore(Path(self.temp.name)/'managed-content'))
        self.evidence.content = ArtifactContentService(registry)
        (self.workspace_path/'source.py').write_text('registered source\n')
        session, _ = await self.session()
        process = session._owned_process
        subprocess.run([sys.executable,managed_validation_runner.__file__,'--source','source.py','--',
            sys.executable,'-c','pass'],cwd=self.workspace_path,env={
                'CODEX_WEB_MANAGED_VALIDATION_DIR':str(session.managed_validation.directory)},check=True)
        capture = Mock(wraps=session.managed_validation.capture)
        session.managed_validation.capture = capture
        first_broker = SimpleNamespace(stop=AsyncMock())
        second_broker = SimpleNamespace(stop=AsyncMock())
        session.egress_broker, session.control_plane_broker = first_broker, second_broker
        # Simulate partial transport cleanup; the owned native handle remains.
        session.runtime = None
        await asyncio.gather(session.stop(),session.stop())
        await session.stop()
        self.assertTrue(process.killed)
        capture.assert_called_once()
        first_broker.stop.assert_awaited_once()
        second_broker.stop.assert_awaited_once()
        self.assertEqual(session.managed_validation.state,'captured')
        self.assertFalse(session._executor_work)
        with self.assertRaises(RuntimeError):
            session._diagnostic_executor.submit(lambda: None)
        evidence = [e for e in self.evidence.list_evidence(self.admin) if e.source=='native-validation-capture']
        self.assertEqual(len(evidence),1)
        self.assertEqual(evidence[0].result,EvidenceResult.INFO)

    async def test_registered_capture_refuses_symlink_without_copying_external_content(self):
        registry = ArtifactContentRegistry()
        registry.register(LocalArtifactContentStore(Path(self.temp.name)/'managed-content'))
        self.evidence.content = ArtifactContentService(registry)
        session, _ = await self.session()
        outside = Path(self.temp.name)/'not-a-report'
        outside.write_text('fixture-private-material')
        (session.managed_validation.directory/('report-'+'a'*32+'.json')).symlink_to(outside)
        await session.stop()
        self.assertEqual(session.managed_validation.state,'unavailable')
        captures = [a for a in self.evidence.list_artifacts(self.admin) if a.source=='native-validation-capture']
        self.assertEqual(captures,[])
        self.assertTrue(self.backend.processes[0].killed)

    async def test_old_failure_release_cannot_release_retry_claim_workspace(self):
        service, _ = fixture.AssignmentBoundCodexSessionTests._install_real_owner_workspace(self)
        session, _ = await self.session()
        original = session._bound_assignment
        self.worker_service.complete(self.worker.id,original.id,AssignmentCompleteRequest(
            lease_token=original.lease.lease_token,fence=original.fence,succeeded=False,
            failure_code='agent_runtime_process_exited'),actor=self.worker_actor)
        # A late diagnostic release must compare current canonical assignment,
        # including a subsequent retry fence, inside the same transaction.
        def retry(state):
            item = state.assignments[0]
            state.assignments[0] = item.model_copy(update={'status':AssignmentStatus.PENDING})
            return state
        self.worker_service.store.update_assignment(self.worker.id,original.id,retry)
        claimant = self.worker_service.claim(self.worker.id,AssignmentClaimRequest(lease_seconds=120),
            actor=self.worker_actor,assignment_id=original.id)
        before = service.store.load().model_dump()
        with self.assertRaises(RuntimeError):
            service.release_failed_assignment(original.execution_workspace_id,original,actor=self.admin)
        self.assertEqual(service.store.load().model_dump(),before)
        self.assertGreater(claimant.fence,original.fence)

    async def test_commit_time_completion_and_start_reject_expiry_during_real_store_lock_wait(self):
        for operation in ("complete", "start"):
            for deadline, database_lock in ((False,False),(True,False),(False,True),(True,True)):
                assignment = self._create_assignment()
                claimed = self.worker_service.claim(self.worker.id, AssignmentClaimRequest(lease_seconds=120),
                    actor=self.worker_actor, assignment_id=assignment.id)
                if operation == "complete":
                    self.worker_service.start(self.worker.id, assignment.id,
                        AssignmentStartRequest(lease_token=claimed.lease.lease_token, fence=claimed.fence), actor=self.worker_actor)
                current = self.short_lease(assignment.id, deadline=deadline)
                entered = threading.Event()
                def invoke():
                    entered.set()
                    if operation == "complete":
                        return self.worker_service.complete(self.worker.id, assignment.id,
                            AssignmentCompleteRequest(lease_token=current.lease.lease_token, fence=current.fence, succeeded=True),
                            actor=self.worker_actor)
                    return self.worker_service.start(self.worker.id, assignment.id,
                        AssignmentStartRequest(lease_token=current.lease.lease_token, fence=current.fence), actor=self.worker_actor)
                connection = sqlite3.connect(self.worker_service.store.store.path) if database_lock else None
                if connection: connection.execute('BEGIN IMMEDIATE')
                else: self.worker_service.store._lock.acquire()
                task = asyncio.create_task(asyncio.to_thread(invoke))
                try:
                    while not entered.is_set(): await asyncio.sleep(.001)
                    await asyncio.sleep(.2)
                    self.assertFalse(task.done())
                finally:
                    if connection:
                        connection.rollback()
                        connection.close()
                    else: self.worker_service.store._lock.release()
                with self.assertRaises(WorkerLeaseError): await task
                self.assertEqual(self.worker_service.store.assignment(assignment.id).model_dump(), current.model_dump())
                # Release this controlled fixture's slot through canonical loss.
                self.worker_service.recover_expired(actor=self.admin, now=current.lease.expires_at + 1)

    async def test_blocked_diagnostic_sqlite_writer_cannot_delay_owned_process_cleanup(self):
        session, gate = await self.session()
        sqlite = self.worker_service.store.store
        sqlite._write_lock.acquire()
        self.backend.disk_bytes = 2 * 1024**3
        try:
            gate.set()
            deadline = time.monotonic() + .5
            while not self.backend.processes[0].killed and time.monotonic() < deadline:
                await asyncio.sleep(.005)
            self.assertTrue(self.backend.processes[0].killed)
            self.assertFalse(session.watchdog_task.done(), "diagnostic writer is still unavailable")
            await asyncio.wait_for(session.watchdog_task, 2)
            self.assertEqual(session.diagnostic_state, "unavailable")
        finally:
            sqlite._write_lock.release()
        for _ in range(100):
            if not session._executor_work: break
            await asyncio.sleep(.005)
        self.assertFalse(session._executor_work, "timed-out work remains owned until its cooperative exit")
        self.assertEqual(self.evidence.list_evidence(self.admin), [])
        self.assertEqual(self.worker_service.store.assignment(session.assignment_id).status, AssignmentStatus.RUNNING)

    async def test_blocked_validation_and_renewal_retire_at_last_validated_lease(self):
        for phase in ("validation", "renewal"):
            session, gate = await self.session()
            session._bound_assignment = self.short_lease(session.assignment_id)
            session._clock = time.time
            lock = session._disk_validation_lock if phase == "validation" else self.worker_service.store.store._write_lock
            lock.acquire()
            try:
                gate.set()
                deadline = time.monotonic() + .5
                while not self.backend.processes[-1].killed and time.monotonic() < deadline:
                    await asyncio.sleep(.005)
                self.assertTrue(self.backend.processes[-1].killed)
                await asyncio.wait_for(session.watchdog_task, 2)
            finally:
                lock.release()
            for _ in range(100):
                if not session._executor_work: break
                await asyncio.sleep(.005)
            self.assertFalse(session._executor_work)
            if phase == "validation":
                self.assertGreater(session._supervision_timings['resource_disk_lock_seconds'], .02)
            current = self.worker_service.store.assignment(session.assignment_id)
            self.assertEqual(current.lease.expires_at, session._bound_assignment.lease.expires_at)
            self.assertEqual(current.status, AssignmentStatus.RUNNING)
            self.worker_service.recover_expired(actor=self.admin, now=time.time())
            self.assertEqual(self.worker_service.store.assignment(current.id).status, AssignmentStatus.LOST)

    async def test_resource_timeout_reports_distinct_stage_and_never_caches_partial_scan(self):
        from unittest.mock import patch
        from codex_web.storage.state_store import check_operation_budget
        for stage, owner, attribute in (
            ('resource_disk_seconds', self.backend, 'execution_disk_usage'),
            ('resource_readonly_seconds', self.local_worker, 'readonly_disk_bytes'),
            ('resource_rss_seconds', self.backend, 'process_tree_rss_bytes'),
        ):
            with self.subTest(stage=stage):
                session, gate = await self.session()
                session._bound_assignment = self.short_lease(session.assignment_id)
                session._clock = time.time
                session.last_disk_validation_monotonic = None
                cached = session.last_disk_bytes
                def blocked(*args):
                    while True:
                        check_operation_budget()
                        time.sleep(.005)
                with patch.object(owner, attribute, blocked, create=True):
                    gate.set()
                    await asyncio.wait_for(session.watchdog_task, 2)
                    for _ in range(100):
                        if not session._executor_work:
                            break
                        await asyncio.sleep(.005)
                self.assertFalse(session._executor_work)
                self.assertTrue(self.backend.processes[-1].killed)
                self.assertGreater(session._supervision_timings[stage], .02)
                if stage != 'resource_rss_seconds':
                    self.assertEqual(session.last_disk_bytes, cached)
                    self.assertIsNone(session.last_disk_validation_monotonic)
                observation = session._failure_observation
                self.assertEqual(observation['code'], 'execution_timeout')
                self.assertEqual(observation['phase'], 'validation')
                self.worker_service.recover_expired(actor=self.admin, now=time.time())

    async def test_real_resource_failure_is_durable_and_sanitized_before_stop(self):
        session, gate = await self.session()
        self.backend.disk_bytes = 2 * 1024**3
        gate.set()
        await asyncio.wait_for(session.watchdog_task, 2)
        current = self.worker_service.store.assignment(session.assignment_id)
        self.assertEqual(current.status, AssignmentStatus.FAILED)
        self.assertEqual(current.failure.reason_code.value, "resource_limit")
        self.assertIsNone(current.lease)
        records = self.evidence.list_evidence(self.admin)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].result, EvidenceResult.FAIL)
        self.assertEqual(current.evidence_ids, (records[0].id,))
        self.assertEqual(records[0].metadata["observed_fence"], session.fence)
        self.assertTrue(self.backend.processes[0].killed)
        # Reopen canonical storage, as a recovered namespace would.
        reopened = ArtifactEvidenceService(ArtifactEvidenceStore(self.worker_service.store.store))
        self.assertEqual(reopened.list_evidence(self.admin)[0].id, records[0].id)
        serialized = json.dumps([r.model_dump(mode="json") for r in records])
        self.assertNotIn(session._bound_assignment.lease.lease_token, serialized)
        self.assertEqual(len(self.observations()), 1)

    async def test_original_error_after_canonical_loss_cannot_overwrite_new_claim(self):
        session, gate = await self.session()
        dirty = self.workspace_path / "uncommitted.py"
        dirty.write_text("source survives\n")
        current = self.worker_service.store.assignment(session.assignment_id)
        self.worker_service.recover_expired(actor=self.admin, now=current.lease.expires_at + 1)
        lost = self.worker_service.store.assignment(session.assignment_id)
        self.assertEqual(lost.status, AssignmentStatus.LOST)
        self.worker_service.retry_lost(current.id, actor=self.admin)
        claimed = self.worker_service.claim(self.worker.id,
            AssignmentClaimRequest(lease_seconds=120), actor=self.worker_actor, assignment_id=current.id)
        successor = self.worker_service.start(self.worker.id, current.id,
            AssignmentStartRequest(lease_token=claimed.lease.lease_token, fence=claimed.fence), actor=self.worker_actor)
        before = successor.model_dump()
        gate.set()
        await asyncio.wait_for(session.watchdog_task, 2)
        self.assertEqual(self.worker_service.store.assignment(current.id).model_dump(), before)
        self.assertEqual(dirty.read_text(), "source survives\n")
        event = self.observations()[0]
        self.assertEqual(event.details["observed_fence"], session.fence)
        self.assertEqual(event.details["current_fence"], successor.fence)
        self.assertFalse(event.details["completion_asserted"])
        self.assertEqual(len(self.evidence.list_evidence(self.admin)), 1)

    async def test_two_real_renewals_and_asgi_livez_under_default_executor_contention(self):
        # Two bounded blocking tool/projection slots reproduce default-executor
        # admission delay, without host stress or a live control-plane request.
        executor = ThreadPoolExecutor(max_workers=2)
        loop = asyncio.get_running_loop()
        loop.set_default_executor(executor)
        first, gate1 = await self.session()
        self.worker_service.store.update(lambda state: state.model_copy(update={"workers": [
            w.model_copy(update={"max_concurrency": 2}) for w in state.workers]}))
        original = self.workspaces.workspace
        secondary = self.workspace_path.parent / "second"
        secondary.mkdir()
        replacement = SimpleNamespace(**{**vars(original), "id": "execws-second", "execution_id": "second", "path": str(secondary)})
        self.workspaces.get = lambda workspace_id, actor: {original.id: original, replacement.id: replacement}[workspace_id]
        second, gate2 = await self.session(execution_id="second", execution_workspace_id="execws-second")
        release = threading.Event()
        started = [threading.Event(), threading.Event()]
        def occupy(index):
            started[index].set()
            release.wait(3)
        occupying = [loop.run_in_executor(None, occupy, i) for i in range(2)]
        while not all(e.is_set() for e in started):
            await asyncio.sleep(.001)
        telemetry = usage_fixture.AgentRuntimeTelemetryTests()
        telemetry.setUp()
        app = FastAPI()
        @app.middleware("http")
        async def identity(request, call_next):
            request.state.identity_actor = telemetry.actor
            return await call_next(request)
        app.include_router(build_system_router(SimpleNamespace(version=lambda: "fixture"), None, None, None))
        app.include_router(build_agent_runtime_usage_router(telemetry.service))
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture")
        probe = asyncio.create_task(client.get('/api/agent-runtime-usage'))
        try:
            gate1.set(); gate2.set()
            response = await asyncio.wait_for(client.get("/api/livez"), 1)
            self.assertEqual(response.status_code, 200)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                current = [self.worker_service.store.assignment(s.assignment_id) for s in (first, second)]
                if all(a.lease.renewed_at is not None for a in current):
                    break
                await asyncio.sleep(.005)
            self.assertTrue(all(a.lease.renewed_at is not None for a in current))
            self.assertFalse(probe.done(), "control: default executor still occupied")
            self.assertTrue(all(a.status == AssignmentStatus.RUNNING for a in current))
            for session in (first, second):
                self.assertIn("validation_queue_seconds", session._supervision_timings)
        finally:
            release.set()
            await asyncio.gather(*occupying, probe)
            self.assertEqual(probe.result().status_code, 200)
            self.assertEqual(probe.result().json()['count'], 0)
            await client.aclose()
            telemetry.tearDown()

    async def test_real_process_group_descendants_are_retired(self):
        session, gate = await self.session()
        child_file = self.workspace_path / "child.pid"
        process = subprocess.Popen(["/bin/bash", "-c", 'sleep 30 & echo $! > "$1.tmp"; mv -- "$1.tmp" "$1"; wait', "fixture", str(child_file)], start_new_session=True)
        session.runtime.proc = process
        self.backend.terminate_process = BubblewrapExecutionBackend._kill_process_group
        try:
            for _ in range(100):
                if child_file.exists(): break
                await asyncio.sleep(.005)
            child = int(child_file.read_text())
            self.backend.disk_bytes = 2 * 1024**3
            gate.set()
            await asyncio.wait_for(session.watchdog_task, 2)
            self.assertIsNotNone(process.poll())
            # A zombie is dead, awaiting the namespace reaper, and cannot execute.
            for _ in range(100):
                path = Path(f"/proc/{child}/stat")
                if not path.exists() or path.read_text().split()[2] == "Z": break
                await asyncio.sleep(.005)
            self.assertTrue(not path.exists() or path.read_text().split()[2] == "Z")
        finally:
            BubblewrapExecutionBackend._kill_process_group(process)
            process.wait(timeout=2)


class DurableNamespaceCustodyTests(unittest.TestCase):
    def test_real_git_workspace_retains_uncommitted_source_on_release(self):
        fixture = workspace_fixture.ExecutionWorkspaceTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        repo = Path(fixture.project.path)
        subprocess.run(['git','init',str(repo)], check=True, capture_output=True)
        (repo/'source.py').write_text('original\n')
        subprocess.run(['git','-C',str(repo),'add','source.py'], check=True, capture_output=True)
        subprocess.run(['git','-C',str(repo),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','fixture'], check=True, capture_output=True)
        fixture.service.backend = LocalGitWorkspaceBackend(Path(fixture.temp.name)/'real-workspaces')
        workspace = fixture._acquire('custody')
        dirty = Path(workspace.path)/'source.py'
        dirty.write_text('uncommitted recovery source\n')
        fixture.service.release(workspace.id, ExecutionWorkspaceRelease(discard=False,reason='worker lost'),
            actor=fixture.actor, preserve_files=True)
        self.assertEqual(dirty.read_text(), 'uncommitted recovery source\n')

class ManagedValidationProtocolTests(unittest.TestCase):
    def test_real_runner_records_exit_without_pass_claim_and_refuses_unregistered_execution(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root/'source.py').write_text('source\n')
            slot = root/'slot'
            marker = root/'executed'
            command = [sys.executable, managed_validation_runner.__file__, '--source','source.py','--',
                sys.executable,'-c',f'from pathlib import Path; Path({str(marker)!r}).touch()']
            environment = {'PATH':os.environ['PATH'],'CODEX_WEB_MANAGED_VALIDATION_DIR':str(slot)}
            rejected = subprocess.run(command,cwd=root,env=environment,capture_output=True)
            self.assertNotEqual(rejected.returncode,0)
            self.assertFalse(marker.exists())
            slot.mkdir()
            completed = subprocess.run(command,cwd=root,env=environment,capture_output=True)
            self.assertEqual(completed.returncode,0,completed.stderr)
            report = json.loads(next(slot.glob('report-*.json')).read_bytes())
            self.assertEqual((report['state'],report['exit_code']),('finished',0))
            self.assertNotIn('passed',report)
            self.assertNotIn(str(marker),json.dumps(report))
            for name in ('../escape','.env','.env.prod','source.key','/etc/passwd','a/.git/config'):
                with self.assertRaises(ValueError): managed_validation_runner.source_name(name)
            report['stdout'] = 'credential-like material'
            with self.assertRaises(ValueError): managed_validation_runner.sanitize_report(report)
