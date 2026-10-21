"""Real message API, bootstrap lifecycle, Git isolation and custody; native RPC is a seam."""
import asyncio
import json
from pathlib import Path
import subprocess
import time
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import httpx
from fastapi import FastAPI

from codex_web.api.turns import build_turns_router
from codex_web.agent_providers import AgentProviderUpsert
from codex_web.agent_runtime import AgentRuntimeSessionRequest
from codex_web.execution_workers import AssignmentCancelRequest, AssignmentClaimRequest, AssignmentStartRequest
from codex_web.execution_workspace_backend import LocalGitWorkspaceBackend
from codex_web.local_execution_backend import BubblewrapExecutionBackend
from codex_web.procedure_discovery import ProcedureDiscovery
from codex_web.runtime.execution import TurnExecutionService
from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSessionManager
from codex_web.services.agent_providers import AgentProviderService
from codex_web.services.agent_session_custody import AgentSessionCustodyService
from codex_web.services.artifact_content import ArtifactContentRegistry, ArtifactContentService
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.local_artifact_content import LocalArtifactContentStore
from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime
from codex_web.services.managed_validation_custody import ManagedValidationCustody
from codex_web.services.procedure_discovery import (ProcedureDiscoveryService,
    ProcedureExecutionCapabilityService, install_native_procedure_admission)
from codex_web.services.skills import SkillService
from codex_web.storage.agent_providers import AgentProviderStore
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.thread_history import ThreadHistoryRepository
from tests import test_project_coordination_delegation as delegated


class AssignmentRenewalTests(unittest.IsolatedAsyncioTestCase):
    _request = delegated.ProjectDelegatedDispatchTests._request
    call = delegated.ProjectDelegatedDispatchTests.call
    publish = delegated.ProjectDelegatedDispatchTests.publish
    create = delegated.ProjectDelegatedDispatchTests.create

    async def asyncSetUp(self):
        await delegated.ProjectDelegatedDispatchTests.asyncSetUp(self)
        f = self.f
        f.workers.identity = f.identity
        f.workers.cancel_bootstrap(self.assignment.id, AssignmentCancelRequest(
            expected_fence=self.assignment.fence, reason='prepare private real Git fixture'), actor=self.actor)
        source = Path(f.project.path)
        def git(*args):
            return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', '-C', str(source), *args],
                check=True, capture_output=True, text=True).stdout.strip()
        git('init', '-b', 'main')
        (source/'.gitignore').write_text('.venv/\n')
        (source/'source.txt').write_text('Original clean source\n')
        git('add', '.gitignore', 'source.txt')
        git('commit', '-m', 'Private fixture')
        f.workspaces.backend = LocalGitWorkspaceBackend(Path(f.temp.name)/'real-workspaces')
        binding = f.service.prepare_bootstrap(bootstrap_id='real-bootstrap', execution_id='real-execution',
            project_id='home', sandbox='workspace-write', approval_policy='on-request',
            explicit_repository_id=f.repository.id, execution_profile_id='project-governor')
        self.bindings.rebind(bootstrap_id='real-bootstrap', thread_id='native-governor',
            execution_id=binding.execution_id, assignment_id=binding.assignment_id,
            execution_workspace_id=binding.workspace_id, actor=self.actor)
        backend = BubblewrapExecutionBackend()
        self.assertTrue(backend.probe().ready)
        self.local_worker = LocalExecutionWorkerRuntime(f.workers, f.workspaces, backend,
            worker=f.worker, worker_actor=self.worker_actor, control_actor=self.actor)
        registry = ArtifactContentRegistry()
        registry.register(LocalArtifactContentStore(Path(f.temp.name)/'content'))
        self.artifacts = ArtifactEvidenceService(ArtifactEvidenceStore(f.sqlite), content=ArtifactContentService(registry))
        self.native_status = 'idle'
        self.native_thread_id = 'native-governor'
        self.resume_outcome = 'success'
        self.resume_entered = asyncio.Event()
        self.resume_release = asyncio.Event()
        self.native_requests = []
        self.native_inputs = []
        fixture = self

        class TransportSession:
            def __init__(self, local_worker, host, assignment_id, **kwargs):
                self.assignment_id = assignment_id
                self.fence = None
                self.runtime = SimpleNamespace(native_session_id=fixture.native_thread_id)
                self.runtime_binding = f.service.runtime_binding
                self.stopped = False
                self.pending = 0
            async def start(self):
                current = f.workers.store.assignment(self.assignment_id)
                if current.status == 'pending':
                    claimed = f.workers.claim(f.worker.id, AssignmentClaimRequest(lease_seconds=3600),
                        actor=fixture.worker_actor, assignment_id=current.id)
                    current = f.workers.start(f.worker.id, current.id, AssignmentStartRequest(
                        fence=claimed.fence, lease_token=claimed.lease.lease_token), actor=fixture.worker_actor)
                self.fence = current.fence
                workspace = f.workspaces.get(current.execution_workspace_id, fixture.actor)
                self.workspace_path = Path(workspace.path)
                self.managed_validation = ManagedValidationCustody(fixture.artifacts, current,
                    self.workspace_path, fixture.actor)
                self.managed_validation.prepare()
                return self
            def validate_current(self):
                current = fixture.local_worker._pending_assignment(self.assignment_id)
                if (current.status != 'running' or current.fence != self.fence or current.lease is None
                        or current.lease.expires_at <= time.time()):
                    raise RuntimeError('stale transport assignment')
                return current
            def status(self):
                return SimpleNamespace(worker_id=f.worker.id, fence=self.fence,
                    running=not self.stopped, ready=not self.stopped, pending_executor_work=self.pending)
            async def request(self, method, params):
                self.validate_current()
                fixture.native_requests.append((method, params))
                if method == 'thread/read':
                    return {'thread': {'id': fixture.native_thread_id, 'status': {'type': fixture.native_status}}}
                if method == 'thread/resume':
                    if fixture.resume_outcome == 'hold':
                        fixture.resume_entered.set()
                        await fixture.resume_release.wait()
                    if fixture.resume_outcome == 'unknown':
                        raise TimeoutError('native resume outcome unknown')
                    if fixture.resume_outcome == 'cancel':
                        raise asyncio.CancelledError
                    return {'thread': {'id': fixture.native_thread_id}}
                if method == 'turn/start':
                    current_session = fixture.sessions.get(fixture.original_session.id, fixture.actor)
                    if current_session.assignment_id != self.assignment_id:
                        raise AssertionError('input preceded canonical custody reassociation')
                    fixture.native_inputs.append(params)
                    if fixture.resume_outcome == 'input_unknown':
                        raise TimeoutError('native input outcome unknown')
                    fixture.native_status = 'active'
                    return {'turn': {'id': 'new-input-'+str(len(fixture.native_inputs))}}
                raise AssertionError(method)
            async def stop(self):
                self.stopped = True

        self.real_manager = AssignmentBoundAgentProcessSessionManager(self.local_worker, self.host,
            runtime_factory=lambda *a, **k: None, credential_provider=object(), session_factory=TransportSession)
        self.addAsyncCleanup(self.real_manager.stop_all)
        old_session = await self.real_manager.start(binding.assignment_id)
        self.assignment = old_session.validate_current()
        self.old = self.assignment
        self.custody = AgentSessionCustodyService(self.sessions, f.workers, self.bindings,
            f.workspaces, f.execution_profiles)
        adapter = CodexAgentRuntimeAdapter(old_session)
        # The older coordination fixture uses a shortened runtime label.
        # Create the real adapter identity before exercising custody validation.
        baseline = self.sessions.get(self.goal_binding.agent_session_id, self.actor)
        self.sessions.store.upsert(baseline.model_copy(update={'runtime_type': adapter.runtime_type}))
        self.bindings.mark_initial_turn_started('native-governor', self.actor)
        request = AgentRuntimeSessionRequest(project_id='home', assignment_id=self.assignment.id,
            execution_id=self.assignment.execution_id, execution_workspace_id=binding.workspace_id,
            worker_id=f.worker.id, workspace_cwd=str(old_session.workspace_path), resource_ids=self.assignment.resource_ids)
        proof = self.custody.capture('native-governor', adapter, old_session, request, self.assignment.fence)
        resumed = await adapter.resume_session('native-governor', request)
        self.custody.complete(proof, 'native-governor', adapter, old_session, request, self.assignment.fence, resumed)
        self.original_session = self.sessions.get(self.goal_binding.agent_session_id, self.actor)
        for index in range(64):
            f.workers.record_procedure_discovery(self.assignment.id, ProcedureDiscovery(
                actor_identity_id=self.actor.identity_id, organization_id=self.actor.organization_id,
                workspace_id=self.actor.workspace_id, project_id='home', resource_ids=self.assignment.resource_ids,
                operation=None, context_checksum=f'old-{index}', evaluated_at=time.time(), outcome='no_match',
                policy_source='canonical:existing-policy', policy_allows=True), expected_fence=self.assignment.fence, actor=self.actor)
        self.old = f.workers.store.assignment(self.assignment.id)
        self.old_workspace = f.workspaces.get(self.old.execution_workspace_id, self.actor)
        self.actual = TurnExecutionService(self.host, binding_service=f.service, session_manager=self.real_manager,
            bootstrap_bindings=self.bindings, control_actor=self.actor)
        self.actual.session_custody = self.custody
        self.actual.readiness_origin_blocker = lambda _assignment_id: ()
        self.actual.schedule_queue_drain = Mock()
        self.actual._trusted_local_codex_session_enabled = lambda **kwargs: False
        self.actual.automated_admission_allowed = lambda: False
        self.host._project = lambda project_id: f.project
        providers = AgentProviderService(AgentProviderStore(f.sqlite))
        self.sessions.registry.register(adapter)
        providers.upsert(AgentProviderUpsert(id=adapter.provider_id, display_name='Private Codex fixture',
            declared_capabilities=adapter.capabilities, granted_capabilities=adapter.capabilities,
            health='healthy'), actor=self.actor)
        discovery = ProcedureDiscoveryService(SkillService(self.registry), f.resources,
            execution_capabilities=ProcedureExecutionCapabilityService(f.workers, self.sessions.registry, providers))
        install_native_procedure_admission(self.actual, SimpleNamespace(), discovery, f.workers,
            ThreadHistoryRepository(f.sqlite), self.actor, f.execution_profiles)
        self.turn_service.execution = self.actual
        self.turn_service.preflight = ExecutionPreflightService(ExecutionPreflightStore(f.sqlite))
        self.app = FastAPI()
        @self.app.middleware('http')
        async def identity(request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)
        self.app.include_router(build_turns_router(self.turn_service, self.scope))
        self.native_requests.clear()

    async def post(self, message, **parameters):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test') as client:
            return await client.post(f'/api/threads/{self.native_thread_id}/turns', json={
                'project_id': 'home', 'message': message, 'execution_profile_id': 'project-governor', **parameters})

    async def test_exhausted_origin_with_unresolved_ready_is_not_retired_or_given_new_input(self):
        self.actual.readiness_origin_blocker = lambda assignment_id: ('retained-attempted-ready',)
        before = self.f.workers.store.assignment(self.old.id).model_dump(mode='json')
        response = await self.post('A separately retained current instruction must not retire the READY origin.')
        self.assertEqual(response.status_code,409,response.text)
        self.assertIn('same-origin read-only reconciliation',response.text)
        self.assertEqual(self.f.workers.store.assignment(self.old.id).model_dump(mode='json'),before)
        self.assertEqual(self.actual._bootstrap_binding_for_thread(self.native_thread_id).assignment_id,self.old.id)
        self.assertEqual(self.native_inputs,[])

    async def test_settled_origin_with_appended_recovery_evidence_can_renew_without_deletion(self):
        from codex_web.procedure_reconciliation import RECOVERY_PREFIX
        def append(state):
            for item in state.assignments:
                if item.id == self.old.id:
                    item.procedure_discoveries = (*item.procedure_discoveries,
                        item.procedure_discoveries[-1].model_copy(update={
                            'supporting_sources':(RECOVERY_PREFIX+'a'*32+':settled-ready',)}))
            return state
        self.f.workers.store.update(append)
        before = self.f.workers.store.assignment(self.old.id).procedure_discoveries
        response = await self.post('A genuine current task after positive settlement.')
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(response.json()['assignmentRenewed'])
        self.assertEqual(self.f.workers.store.assignment(self.old.id).procedure_discoveries,before)
        self.assertEqual(len(self.native_inputs),1)

    async def test_idle_message_renews_once_preserving_native_goal_custody_and_history(self):
        goal_before = self.goals.get(self.goal.id, scope=self.actor.tenant).model_dump(mode='json')
        binding_before = self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant).model_dump(mode='json')
        response = await self.post('The exact original\nwith a second line.')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['assignmentRenewed'])
        self.assertEqual(len(self.native_inputs), 1)
        self.assertEqual(self.native_inputs[0]['input'][0]['text'], 'The exact original\nwith a second line.')
        retired = self.f.workers.store.assignment(self.old.id)
        self.assertEqual(retired.status, 'cancelled')
        self.assertEqual(retired.fence, self.old.fence+1)
        self.assertEqual(retired.procedure_discoveries, self.old.procedure_discoveries)
        self.assertTrue(Path(self.old_workspace.path).exists())
        current = self.sessions.get(self.original_session.id, self.actor)
        self.assertEqual(current.provider_native_session_id, 'native-governor')
        self.assertNotEqual(current.assignment_id, self.old.id)
        fresh = self.f.workers.store.assignment(current.assignment_id)
        self.assertEqual(fresh.repository_scope, self.old.repository_scope)
        self.assertEqual(fresh.agent_profile, self.old.agent_profile)
        self.assertEqual(fresh.runtime_binding, self.old.runtime_binding)
        self.assertEqual(fresh.network, self.old.network)
        self.assertEqual(fresh.secret_refs, self.old.secret_refs)
        self.assertEqual(fresh.limits, self.old.limits)
        self.assertEqual(len(fresh.procedure_discoveries), 1)
        self.assertEqual(self.goals.get(self.goal.id, scope=self.actor.tenant).model_dump(mode='json'), goal_before)
        self.assertEqual(self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant).model_dump(mode='json'), binding_before)
        self.assertEqual([method for method, _ in self.native_requests], ['thread/read', 'thread/resume', 'turn/start'])

    async def assert_retained_refusal(self, message):
        response = await self.post(message)
        self.assertEqual(response.status_code, 409, response.text)
        detail = response.json()['detail']
        self.assertTrue(detail['retainedMessage'])
        attempt = self.turn_service.preflight.get(detail['attemptId'], actor=self.actor)
        self.assertEqual(attempt.message, message)
        self.assertEqual(attempt.status, 'blocked')
        self.assertEqual(self.native_inputs, [])
        return detail

    async def test_dirty_source_is_preserved_and_original_stays_recoverable(self):
        source = Path(self.old_workspace.repository_members[0].workspace_path)/'source.txt'
        source.write_text('Unpublished original work\n')
        detail = await self.assert_retained_refusal('Continue the unpublished work')
        self.assertIn('repository changes', detail['blockers'][-1]['message'])
        self.assertEqual(source.read_text(), 'Unpublished original work\n')
        self.assertEqual(self.f.workers.store.assignment(self.old.id), self.old)

    async def test_provider_busy_or_unknown_is_not_idle_authority(self):
        for status in ('active', 'notLoaded', 'systemError'):
            self.native_status = status
            detail = await self.assert_retained_refusal('Preserve original for '+status)
            self.assertIn('confirmed', detail['blockers'][-1]['message'])
            self.assertEqual(self.f.workers.store.assignment(self.old.id), self.old)

    async def test_pending_executor_and_running_validation_prevent_retirement(self):
        session = self.real_manager.get(self.old.id)
        session.pending = 1
        await self.assert_retained_refusal('Wait for the owned executor')
        session.pending = 0
        report = dict(schema_version='1.0', run_id='a'*32, command_digest='b'*64,
            sources=[{'path':'source.txt','sha256':'c'*64}], started_at=time.time(),
            finished_at=None, exit_code=None, state='running')
        (session.managed_validation.directory/('report-'+'a'*32+'.json')).write_text(json.dumps(report))
        detail = await self.assert_retained_refusal('Wait for the actual validation')
        self.assertIn('validation', detail['blockers'][-1]['message'])
        self.assertEqual(self.f.workers.store.assignment(self.old.id), self.old)

    async def test_unknown_resume_preserves_original_and_sends_no_input(self):
        self.resume_outcome = 'unknown'
        response = await self.post('One retained input after uncertain resume')
        self.assertEqual(response.status_code, 409, response.text)
        self.assertTrue(response.json()['detail']['reconciliationRequired'])
        self.assertEqual(self.native_inputs, [])
        self.assertEqual(self.f.workers.store.assignment(self.old.id).status, 'cancelled')
        attempts = self.turn_service.preflight.for_thread('native-governor', actor=self.actor)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].message, 'One retained input after uncertain resume')
        self.assertNotEqual(attempts[0].status, 'started')
        self.assertEqual(self.sessions.get(self.original_session.id, self.actor).assignment_id, self.old.id)
        before = list(self.native_requests)
        with self.assertRaises(Exception) as caught:
            await self.turn_service.retry_preflight('native-governor', attempts[0].id, actor=self.actor)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.native_requests, before)

    async def test_cancelled_inspection_retains_input_and_joins_the_actual_reader(self):
        entered, release, exited = threading.Event(), threading.Event(), threading.Event()
        original = self.local_worker.inspect_git_metadata
        def inspect(*args, **kwargs):
            entered.set()
            release.wait(5)
            try:
                return original(*args, **kwargs)
            finally:
                exited.set()
        self.local_worker.inspect_git_metadata = inspect
        task = asyncio.create_task(self.post('Retain before cancellation'))
        try:
            async with asyncio.timeout(5):
                while not entered.is_set():
                    await asyncio.sleep(.005)
            task.cancel()
            await asyncio.sleep(.02)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(exited.is_set())
        attempts = self.turn_service.preflight.for_thread('native-governor', actor=self.actor)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].message, 'Retain before cancellation')
        self.assertEqual(attempts[0].status, 'blocked')
        self.assertEqual(self.f.workers.store.assignment(self.old.id), self.old)
        self.assertEqual(self.native_inputs, [])

    async def test_cancelled_resume_never_becomes_a_stale_retry_claim(self):
        self.resume_outcome = 'cancel'
        response = await self.post('Retain cancelled resume')
        self.assertEqual(response.status_code, 409, response.text)
        attempt, = self.turn_service.preflight.for_thread('native-governor', actor=self.actor)
        self.assertEqual(attempt.status, 'reconciliation_required')
        self.assertEqual(attempt.message, 'Retain cancelled resume')
        self.assertEqual(self.native_inputs, [])
        self.assertFalse(self.turn_service.preflight.public(attempt, actor=self.actor)['can_retry'])

    async def test_client_cancellation_during_resume_holds_the_original(self):
        self.resume_outcome = 'hold'
        task = asyncio.create_task(self.post('Retain original during client cancellation'))
        try:
            await asyncio.wait_for(self.resume_entered.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            self.resume_release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        attempt, = self.turn_service.preflight.for_thread('native-governor', actor=self.actor)
        self.assertEqual(attempt.status, 'reconciliation_required')
        self.assertEqual(attempt.message, 'Retain original during client cancellation')
        self.assertEqual(self.native_inputs, [])

    async def test_uncertain_native_input_cannot_be_replayed(self):
        self.resume_outcome = 'input_unknown'
        response = await self.post('One physical native input attempt')
        self.assertEqual(response.status_code, 409, response.text)
        attempt, = self.turn_service.preflight.for_thread('native-governor', actor=self.actor)
        self.assertEqual(attempt.status, 'reconciliation_required')
        self.assertEqual(len(self.native_inputs), 1)
        with self.assertRaises(Exception) as caught:
            await self.turn_service.retry_preflight('native-governor', attempt.id, actor=self.actor)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(len(self.native_inputs), 1)
        _, acquired = self.turn_service.preflight.store.claim_retry(attempt.id,
            claim_id='expired-claim-must-not-replay', now=time.time()+120)
        self.assertFalse(acquired)
        self.assertEqual(len(self.native_inputs), 1)

    async def replace_fixture_assignment(self, *, execution_profile_id='project-governor',
                                         agent_profile=None, read_only_repository_ids=()):
        await self.real_manager.complete(self.old.id, succeeded=False, failure_message='private pinned fixture')
        binding = self.f.service.prepare_bootstrap(bootstrap_id='pinned-bootstrap', execution_id='pinned-execution',
            project_id='home', sandbox='workspace-write', approval_policy='on-request',
            explicit_repository_id=self.f.repository.id, execution_profile_id=execution_profile_id,
            agent_profile=agent_profile, read_only_repository_ids=read_only_repository_ids)
        self.bindings.rebind(bootstrap_id='pinned-bootstrap', thread_id='native-governor',
            execution_id=binding.execution_id, assignment_id=binding.assignment_id,
            execution_workspace_id=binding.workspace_id, actor=self.actor, expected_assignment_id=self.old.id)
        session = await self.real_manager.start(binding.assignment_id)
        assignment = session.validate_current()
        adapter = CodexAgentRuntimeAdapter(session)
        request = AgentRuntimeSessionRequest(project_id='home', assignment_id=assignment.id,
            execution_id=assignment.execution_id, execution_workspace_id=binding.workspace_id,
            worker_id=self.f.worker.id, workspace_cwd=str(session.workspace_path), resource_ids=assignment.resource_ids)
        proof = self.custody.capture('native-governor', adapter, session, request, assignment.fence)
        self.custody.complete(proof, 'native-governor', adapter, session, request, assignment.fence,
            await adapter.resume_session('native-governor', request))
        for index in range(64):
            self.f.workers.record_procedure_discovery(assignment.id, ProcedureDiscovery(
                actor_identity_id=self.actor.identity_id, organization_id=self.actor.organization_id,
                workspace_id=self.actor.workspace_id, project_id='home', resource_ids=assignment.resource_ids,
                operation=None, context_checksum=f'pinned-{index}', evaluated_at=time.time(), outcome='no_match',
                policy_source='canonical:existing-policy', policy_allows=True), expected_fence=assignment.fence, actor=self.actor)
        self.native_requests.clear()
        self.old = self.f.workers.store.assignment(assignment.id)
        self.old_workspace = self.f.workspaces.get(self.old.execution_workspace_id, self.actor)
        return self.old

    async def test_pinned_agent_profile_is_preserved_through_fresh_admission(self):
        from codex_web.agent_profiles import AgentProfileCreate, AgentProfileUpdate
        self.profiles.create(AgentProfileCreate(profile_id='retained-governor', name='Retained governor',
            execution_profile_id='repository-write', sandbox_requirement='workspace-write'), actor=self.actor)
        profile, _ = self.profiles.resolve_for_execution('retained-governor', actor=self.actor,
            project_id='home', revision=1)
        pinned = self.profiles.binding_for(profile)
        assignment = await self.replace_fixture_assignment(execution_profile_id='repository-write', agent_profile=pinned)
        self.profiles.update('retained-governor',AgentProfileUpdate(name='Later revision',
            reason='New metadata must not replace the retained revision'),actor=self.actor)
        response = await self.post('Continue this exact profile revision', agent_profile_id='retained-governor',
            agent_profile_revision=1, execution_profile_id='repository-write')
        self.assertEqual(response.status_code, 200, response.text)
        current = self.sessions.get(self.original_session.id, self.actor)
        fresh = self.f.workers.store.assignment(current.assignment_id)
        self.assertNotEqual(fresh.id, assignment.id)
        self.assertEqual(fresh.agent_profile, assignment.agent_profile)
        self.assertEqual((fresh.agent_profile.profile_id, fresh.agent_profile.profile_revision,
            fresh.agent_profile.profile_record_id), (pinned.profile_id, pinned.profile_revision, pinned.profile_record_id))
        self.assertEqual(len(self.native_inputs), 1)

    async def test_clean_detached_read_only_context_is_preserved_during_renewal(self):
        from codex_web.resources import ResourceCreate, ResourceAlias
        context = Path(self.f.temp.name)/'readonly-context'
        subprocess.run(['git','-c','core.hooksPath=/dev/null','clone','--no-hardlinks',
            str(self.f.project.path),str(context)],check=True,capture_output=True)
        resource = self.f.resources.create(ResourceCreate(resource_type='repository',name='Read-only context',
            aliases=[ResourceAlias(namespace='filesystem',value=str(context))]),actor=self.actor)
        self.f.resources.bind_project(project=self.f.project,resource_id=resource.id,actor=self.actor)
        # Establish a new native session with this scope from its first
        # admission. Custody intentionally refuses retrofitting another scope
        # onto the earlier governor's historical session.
        await self.real_manager.complete(self.old.id,succeeded=False,failure_message='private context fixture')
        self.native_thread_id = 'native-with-context'
        binding = self.f.service.prepare_bootstrap(bootstrap_id='context-bootstrap',execution_id='context-execution',
            project_id='home',sandbox='workspace-write',approval_policy='on-request',
            explicit_repository_id=self.f.repository.id,execution_profile_id='project-governor',
            read_only_repository_ids=(resource.id,))
        self.bindings.bind(bootstrap_id='context-bootstrap',thread_id=self.native_thread_id,
            execution_id=binding.execution_id,assignment_id=binding.assignment_id,
            execution_workspace_id=binding.workspace_id,actor=self.actor)
        self.bindings.mark_initial_turn_started(self.native_thread_id,self.actor)
        session = await self.real_manager.start(binding.assignment_id)
        assignment = session.validate_current()
        self.original_session = self.sessions.adopt(provider_id='openai',runtime_id='codex',
            runtime_type=CodexAgentRuntimeAdapter.runtime_type,provider_native_session_id=self.native_thread_id,
            capability_snapshot=CodexAgentRuntimeAdapter(session).capabilities,
            request=AgentRuntimeSessionRequest(project_id='home',assignment_id=assignment.id,
                execution_id=assignment.execution_id,execution_workspace_id=binding.workspace_id,
                worker_id=self.f.worker.id,workspace_cwd=str(session.workspace_path),resource_ids=assignment.resource_ids),
            actor=self.actor)
        for index in range(64):
            self.f.workers.record_procedure_discovery(assignment.id,ProcedureDiscovery(
                actor_identity_id=self.actor.identity_id,organization_id=self.actor.organization_id,
                workspace_id=self.actor.workspace_id,project_id='home',resource_ids=assignment.resource_ids,
                operation=None,context_checksum=f'context-{index}',evaluated_at=time.time(),outcome='no_match',
                policy_source='canonical:existing-policy',policy_allows=True),expected_fence=assignment.fence,actor=self.actor)
        response = await self.post('Continue with exact read-only scope', read_only_repository_resource_ids=[resource.id])
        self.assertEqual(response.status_code,200,response.text)
        current = self.sessions.get(self.original_session.id,self.actor)
        fresh = self.f.workers.store.assignment(current.assignment_id)
        self.assertNotEqual(fresh.id,assignment.id)
        self.assertEqual(fresh.repository_scope,assignment.repository_scope)
        self.assertEqual(fresh.repository_scope.read_only_repository_ids,(resource.id,))
        self.assertEqual(len(self.native_inputs),1)

    async def test_malformed_or_incomplete_validation_custody_cannot_authorize_renewal(self):
        import os
        slot = self.real_manager.get(self.old.id).managed_validation.directory
        path = slot/('report-'+'d'*32+'.json')
        outside = Path(self.f.temp.name)/'external-validation'
        outside.write_text('external-synthetic-content')
        for mode in ('malformed','symlink','fifo','oversized','partial'):
            with self.subTest(mode=mode):
                target = slot/'partial.tmp' if mode == 'partial' else path
                if mode == 'symlink': target.symlink_to(outside)
                elif mode == 'fifo': os.mkfifo(target)
                else: target.write_text('x'*16385 if mode == 'oversized' else 'external-synthetic-content')
                try:
                    detail = await self.assert_retained_refusal('Retained for '+mode)
                    self.assertNotIn('external-synthetic-content',str(detail))
                    self.assertEqual(self.f.workers.store.assignment(self.old.id),self.old)
                finally:
                    target.unlink()

    async def test_concurrent_messages_keep_each_original_and_only_one_assignment_renewal(self):
        responses = await asyncio.gather(self.post('Concurrent original A'), self.post('Concurrent original B'))
        self.assertTrue(all(response.status_code in {200, 409} for response in responses),
            [response.text for response in responses])
        events = [e for e in self.host.events if e.get('type') == 'thread_assignment_renewed']
        self.assertEqual(len(events), 1)
        attempts = self.turn_service.preflight.for_thread('native-governor', actor=self.actor)
        self.assertEqual({a.message for a in attempts}, {'Concurrent original A', 'Concurrent original B'})
        texts = [value['input'][0]['text'] for value in self.native_inputs]
        self.assertEqual(len(texts), len(set(texts)))
        self.assertTrue(set(texts) <= {a.message for a in attempts})
        self.assertTrue(all(a.status in {'started', 'blocked', 'queued', 'retrying'} for a in attempts))

    async def prepare_scratch_fixture(self, *, administrative=False):
        agent_profile = None
        profile_id = 'orchestration-only'
        if administrative:
            from copy import deepcopy
            from unittest.mock import patch
            from codex_web.agent_profiles import AgentProfileCreate
            from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
            from codex_web.services.execution_role_definitions import install_execution_role_definitions
            with patch('codex_web.services.execution_role_definitions.install_execution_role_catalog_provider'):
                roles = install_execution_role_definitions(self.registry)
            self.profiles.role_resolver = lambda role, actor: roles.reference(
                organization_id=actor.organization_id,workspace_id=actor.workspace_id)
            payload = deepcopy(self.payload)
            payload['profiles'].append(dict(id='orchestration-admin',name='Private scratch administrator',
                description='Exact-project fixture',workspace_mode='scratch',repository_access='none',
                required_worker_capabilities=['command_execution'],
                control_plane_operations=['control_plane.operations.list','repository.issue.create']))
            for scope in ('global','project'):
                current = deepcopy(payload)
                if scope == 'project':
                    current['profiles'][-1]['administrative_repository_ids'] = [self.f.repository.id]
                record = self.registry.create_draft(DefinitionDraftCreate(kind='execution-profile-catalog',
                    definition_id='execution.profiles.default',definition_schema_version='1.2',payload=current,
                    scope_type=scope,scope_id='home' if scope == 'project' else None,actor='fixture'))
                self.registry.publish(record.record_id,DefinitionPublishRequest(actor='fixture'))
            self.profiles.create(AgentProfileCreate(profile_id='scratch-administrator',name='Scratch administrator',
                role_id='orchestrator',authority_role_id='local-admin',
                execution_profile_id='orchestration-admin'),actor=self.actor)
            profile,_ = self.profiles.resolve_for_execution('scratch-administrator',actor=self.actor,
                project_id='home',revision=1)
            agent_profile = self.profiles.binding_for(profile)
            profile_id = 'orchestration-admin'
        await self.real_manager.complete(self.old.id, succeeded=False, failure_message='private scratch fixture')
        self.native_thread_id = 'native-scratch'
        binding = self.f.service.prepare_bootstrap(bootstrap_id='scratch-bootstrap',
            execution_id='scratch-execution', project_id='home', sandbox='workspace-write',
            approval_policy='on-request', execution_profile_id=profile_id, agent_profile=agent_profile)
        self.bindings.bind(bootstrap_id='scratch-bootstrap', thread_id=self.native_thread_id,
            execution_id=binding.execution_id, assignment_id=binding.assignment_id,
            execution_workspace_id=binding.workspace_id, actor=self.actor)
        self.bindings.mark_initial_turn_started(self.native_thread_id,self.actor)
        session = await self.real_manager.start(binding.assignment_id)
        assignment = session.validate_current()
        self.original_session = self.sessions.adopt(provider_id='openai', runtime_id='codex',
            runtime_type=CodexAgentRuntimeAdapter.runtime_type, provider_native_session_id=self.native_thread_id,
            capability_snapshot=CodexAgentRuntimeAdapter(session).capabilities,
            request=AgentRuntimeSessionRequest(project_id='home', assignment_id=assignment.id,
                execution_id=assignment.execution_id, execution_workspace_id=binding.workspace_id,
                worker_id=self.f.worker.id, workspace_cwd=str(session.workspace_path), resource_ids=assignment.resource_ids),
            actor=self.actor)
        for index in range(64):
            self.f.workers.record_procedure_discovery(assignment.id,ProcedureDiscovery(
                actor_identity_id=self.actor.identity_id,organization_id=self.actor.organization_id,
                workspace_id=self.actor.workspace_id,project_id='home',resource_ids=assignment.resource_ids,
                operation=None,context_checksum=f'scratch-{index}',evaluated_at=time.time(),outcome='no_match',
                policy_source='canonical:existing-policy',policy_allows=True),expected_fence=assignment.fence,actor=self.actor)
        self.old = self.f.workers.store.assignment(assignment.id)
        self.old_workspace = self.f.workspaces.get(self.old.execution_workspace_id,self.actor)
        self.native_requests.clear()

    async def test_scratch_renews_once_preserving_files_history_and_native_identity(self):
        await self.prepare_scratch_fixture(administrative=True)
        preserved = Path(self.old_workspace.path)/'coordination-notes.txt'
        preserved.write_text('Do not discard unknown coordination receipts.\n')
        response = await self.post('New current coordination task', execution_profile_id='orchestration-admin',
            agent_profile_id='scratch-administrator',agent_profile_revision=1)
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(response.json()['assignmentRenewed'])
        retired = self.f.workers.store.assignment(self.old.id)
        self.assertEqual(retired.status,'cancelled')
        self.assertEqual(retired.fence,self.old.fence+1)
        self.assertEqual(retired.procedure_discoveries,self.old.procedure_discoveries)
        self.assertEqual(preserved.read_text(),'Do not discard unknown coordination receipts.\n')
        current = self.sessions.get(self.original_session.id,self.actor)
        fresh = self.f.workers.store.assignment(current.assignment_id)
        self.assertNotEqual(fresh.id,self.old.id)
        self.assertEqual(current.provider_native_session_id,self.native_thread_id)
        self.assertEqual(fresh.resource_ids,())
        self.assertEqual(fresh.repository_scope,self.old.repository_scope)
        self.assertEqual(fresh.execution_profile_definition,self.old.execution_profile_definition)
        self.assertEqual(fresh.agent_profile,self.old.agent_profile)
        self.assertEqual(fresh.limits,self.old.limits)
        self.assertEqual(len(fresh.procedure_discoveries),1)
        self.assertEqual([method for method,_ in self.native_requests],['thread/read','thread/resume','turn/start'])
        self.assertEqual(len(self.native_inputs),1)

    async def test_scratch_busy_and_pending_executor_cannot_renew(self):
        await self.prepare_scratch_fixture()
        self.native_status = 'active'
        response = await self.post('Retain while provider busy',execution_profile_id='orchestration-only')
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(self.f.workers.store.assignment(self.old.id),self.old)
        self.native_status = 'idle'
        self.real_manager.get(self.old.id).pending = 1
        response = await self.post('Retain while executor busy',execution_profile_id='orchestration-only')
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(self.f.workers.store.assignment(self.old.id),self.old)
        self.assertEqual(self.native_inputs,[])

    async def test_scratch_unknown_resume_stays_held_without_input_or_replay(self):
        await self.prepare_scratch_fixture()
        self.resume_outcome = 'unknown'
        response = await self.post('Preserve scratch unknown resume',execution_profile_id='orchestration-only')
        self.assertEqual(response.status_code,409,response.text)
        attempt, = self.turn_service.preflight.for_thread(self.native_thread_id,actor=self.actor)
        self.assertEqual(attempt.status,'reconciliation_required')
        self.assertEqual(self.native_inputs,[])
        before = list(self.native_requests)
        with self.assertRaises(Exception) as caught:
            await self.turn_service.retry_preflight(self.native_thread_id,attempt.id,actor=self.actor)
        self.assertEqual(caught.exception.status_code,409)
        self.assertEqual(self.native_requests,before)
        self.assertEqual(self.f.workers.store.assignment(self.old.id).procedure_discoveries,
            self.old.procedure_discoveries)
