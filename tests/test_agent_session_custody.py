"""Real custody stores/claims/Goal dispatch; worker RPC is the transport seam."""
import asyncio
import copy
import hashlib
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from codex_web.agent_runtime import AgentRuntimeResult, AgentRuntimeSessionRequest, AgentSessionStatus
from codex_web.execution_workers import AssignmentCancelRequest, AssignmentClaimRequest, AssignmentStartRequest
from codex_web.models import ProjectCoordinationDispatch
from codex_web.services.agent_runtime import AgentRuntimeError
from codex_web.services.agent_session_custody import AgentSessionCustodyService
from codex_web.services.control_plane_broker import AssignmentBoundControlPlaneBroker
from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter
from codex_web.runtime.execution import TurnExecutionService
from tests import test_project_coordination_delegation as delegated
from tests import test_turn_execution as execution_tests


class SessionCustodyTests(unittest.IsolatedAsyncioTestCase):
    _request = delegated.ProjectDelegatedDispatchTests._request
    call = delegated.ProjectDelegatedDispatchTests.call
    publish = delegated.ProjectDelegatedDispatchTests.publish
    create = delegated.ProjectDelegatedDispatchTests.create
    message = delegated.ProjectDelegatedDispatchTests.message

    async def asyncSetUp(self):
        await delegated.ProjectDelegatedDispatchTests.asyncSetUp(self)
        self.f.workers.identity = self.f.identity
        self.custody = AgentSessionCustodyService(self.sessions, self.f.workers, self.bindings,
            self.f.workspaces, self.f.execution_profiles)
        self.adapter = SimpleNamespace(provider_id='openai', runtime_id='codex', runtime_type='app-server')
        self.old = self.assignment
        self.original_session = self.sessions.get(self.goal_binding.agent_session_id, self.actor)
        self.f.workers.cancel_bootstrap(self.old.id, AssignmentCancelRequest(
            expected_fence=self.old.fence, reason='bounded same-thread rollover'), actor=self.actor)
        binding = self.f.service.prepare_bootstrap(bootstrap_id='bootstrap-successor', execution_id='execution-successor',
            project_id='home', sandbox='workspace-write', approval_policy='on-request',
            explicit_repository_id=self.f.repository.id, execution_profile_id='project-governor')
        claimed = self.f.workers.claim(self.f.worker.id, AssignmentClaimRequest(),
            actor=self.worker_actor, assignment_id=binding.assignment_id)
        self.assignment = self.f.workers.start(self.f.worker.id, binding.assignment_id,
            AssignmentStartRequest(fence=claimed.fence, lease_token=claimed.lease.lease_token), actor=self.worker_actor)
        self.bindings.rebind(bootstrap_id='bootstrap-successor', thread_id='native-governor',
            execution_id=self.assignment.execution_id, assignment_id=self.assignment.id,
            execution_workspace_id=self.assignment.execution_workspace_id, actor=self.actor)
        workspace = self.f.workspaces.store.workspace(self.assignment.execution_workspace_id)
        self.worker_session = SimpleNamespace(workspace_path=Path(workspace.path),
            validate_current=lambda: self.f.workers.store.assignment(self.assignment.id))
        self.request = AgentRuntimeSessionRequest(project_id='home', assignment_id=self.assignment.id,
            execution_id=self.assignment.execution_id, execution_workspace_id=workspace.id,
            resource_ids=self.assignment.resource_ids, worker_id=self.f.worker.id, workspace_cwd=workspace.path)
        await self.broker.stop()
        self.broker = AssignmentBoundControlPlaneBroker(self.service, assignment=self.assignment,
            worker_id=self.f.worker.id, worker_actor=self.worker_actor, fence=self.assignment.fence,
            validator=lambda: self.f.workers.store.assignment(self.assignment.id))
        await self.broker.start()
        self.addAsyncCleanup(self.broker.stop)

    def capture(self):
        return self.custody.capture('native-governor', self.adapter, self.worker_session,
            self.request, self.assignment.fence)

    def complete(self, proof, result=None):
        return self.custody.complete(proof, 'native-governor', self.adapter, self.worker_session,
            self.request, self.assignment.fence, result or AgentRuntimeResult(provider_native_session_id='native-governor'))

    async def test_rollover_reassociates_and_preserves_retired_custody(self):
        child, _ = await self.create()
        proof = ProjectCoordinationDispatch(goal_binding_id=self.goal_binding.id,
            assignment_id=self.assignment.id, fence=self.assignment.fence)
        with self.assertRaises(Exception):
            self.coordination.validate_delegation(proof, thread_id=child, project_id='home')
        old_assignment = self.f.workers.store.assignment(self.old.id).model_dump(mode='json')
        old_workspace = self.f.workspaces.store.workspace(self.old.execution_workspace_id).model_dump(mode='json')
        # Existing immutable usage payload must remain byte-for-byte unchanged.
        self.f.sqlite.record_apply('agent_runtime_usage.records.v1', upserts={'historical': {
            'agent_session_id': self.original_session.id, 'assignment_id': self.old.id,
            'execution_id': self.old.execution_id, 'execution_workspace_id': self.old.execution_workspace_id,
            'provider_native_turn_id': 'prior-turn', 'input_tokens': 17}})
        usage_before = self.f.sqlite.record_items('agent_runtime_usage.records.v1')
        current = self.complete(self.capture())
        self.assertEqual(current.id, self.original_session.id)
        self.assertEqual(current.assignment_id, self.assignment.id)
        self.assertEqual(current.execution_workspace_id, self.assignment.execution_workspace_id)
        self.assertEqual(self.f.workers.store.assignment(self.old.id).model_dump(mode='json'), old_assignment)
        self.assertEqual(self.f.workspaces.store.workspace(self.old.execution_workspace_id).model_dump(mode='json'), old_workspace)
        self.assertEqual(self.f.sqlite.record_items('agent_runtime_usage.records.v1'), usage_before)
        self.coordination.validate_delegation(proof, thread_id=child, project_id='home')
        queued, _ = await self.message(child, key='after-rollover')
        self.assertEqual(queued.source, 'project-coordination')
        await self.execution.drain_thread_queue(child)
        self.execution._start_thread_turn_now_admitted.assert_awaited_once()
        self.assertIsNotNone(self.execution.automation_wait_reason('business-scheduler'))
        events = [e for e in self.f.workers.store.load().events
                  if e.event_type == 'agent_session_execution_reassociated']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].details['previous_assignment_id'], self.old.id)

    async def test_no_successful_resume_means_no_custody_write(self):
        self.capture()  # RPC failure/cancellation does not call complete.
        self.assertEqual(self.sessions.get(self.original_session.id, self.actor), self.original_session)

    async def test_conflicting_session_or_native_result_denies(self):
        proof = self.capture()
        with self.assertRaises(AgentRuntimeError):
            self.complete(proof, AgentRuntimeResult(provider_native_session_id='another-native-id'))
        self.sessions.store.upsert(self.original_session.model_copy(update={'worker_id': 'concurrent-change'}))
        with self.assertRaises(AgentRuntimeError):
            self.complete(proof)
        self.assertEqual(self.sessions.get(self.original_session.id, self.actor).assignment_id, self.old.id)

    async def test_stale_fence_foreign_scope_and_changed_profile_deny(self):
        for changes in ({'fence': self.assignment.fence+1}, {'project_id': 'foreign'},
                        {'status': 'cancelled'}, {'execution_profile_definition': None}):
            proof = self.capture()
            self.f.workers.store.update(lambda s: s.model_copy(update={'assignments': [
                a.model_copy(update=changes) if a.id == self.assignment.id else a for a in s.assignments]}))
            with self.assertRaises(AgentRuntimeError):
                self.complete(proof)
            self.assertEqual(self.sessions.get(self.original_session.id, self.actor).assignment_id, self.old.id)
            self.f.workers.store.update(lambda s: s.model_copy(update={'assignments': [
                self.assignment if a.id == self.assignment.id else a for a in s.assignments]}))

    async def test_cancelled_custody_waiter_drains_its_actual_future(self):
        entered, release, exited = threading.Event(), threading.Event(), threading.Event()
        def operation():
            entered.set()
            release.wait(2)
            exited.set()
        task = asyncio.create_task(TurnExecutionService._settle_custody_call(operation))
        while not entered.is_set():
            await asyncio.sleep(.005)
        task.cancel()
        await asyncio.sleep(.01)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(exited.is_set())

    async def test_shutdown_cancellation_drains_real_writer_not_only_async_wrapper(self):
        entered, release, exited = threading.Event(), threading.Event(), threading.Event()
        def operation():
            entered.set()
            try:
                release.wait(4)
            finally:
                exited.set()
        before = set(asyncio.all_tasks())
        owner = asyncio.create_task(TurnExecutionService._settle_custody_call(operation))
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(.005)
            # Model all-task shutdown, including the inner to_thread task in
            # the original implementation, then repeated owner cancellation.
            for task in set(asyncio.all_tasks()) - before:
                task.cancel()
            await asyncio.sleep(.02)
            owner.cancel()
            await asyncio.sleep(.02)
            self.assertFalse(exited.is_set())
            self.assertFalse(owner.done(), 'owner returned before the actual writer exited')
        finally:
            release.set()
            try:
                await owner
            except asyncio.CancelledError:
                pass
            # Clean up the actual old-algorithm thread even on the expected
            # failing baseline; no unfinished writer escapes this regression.
            async with asyncio.timeout(2):
                while not exited.is_set():
                    await asyncio.sleep(.005)
        self.assertTrue(exited.is_set())

    async def test_same_custody_resume_recovers_status_without_rewriting_event(self):
        current = self.complete(self.capture())
        def events():
            return [e.model_dump(mode='json') for e in self.f.workers.store.load().events
                    if e.event_type == 'agent_session_execution_reassociated']
        original_events = events()
        self.assertEqual(len(original_events), 1)
        for status in (AgentSessionStatus.FAILED, AgentSessionStatus.CLOSED):
            self.sessions.store.upsert(current.model_copy(update={
                'status': status, 'failure_reason': 'previous provider failure'}))
            recovered = self.complete(self.capture())
            self.assertEqual(recovered.status, AgentSessionStatus.READY)
            self.assertIsNone(recovered.failure_reason)
            self.assertEqual(events(), original_events)
            current = recovered
        before = self.sessions.get(current.id, self.actor)
        self.assertEqual(self.complete(self.capture()), before)
        self.assertEqual(events(), original_events)

    async def exercise_actual_resume_path(self, outcome, *, legacy_adoption=False):
        child, _ = await self.create()
        # This is an existing native thread being resumed after rollover, not
        # the special newly-created thread whose first input skips resume.
        self.bindings.mark_initial_turn_started('native-governor', self.actor)
        proof = ProjectCoordinationDispatch(goal_binding_id=self.goal_binding.id,
            assignment_id=self.assignment.id, fence=self.assignment.fence)
        # Real adapter identity, not the shortened type used by the older fixture.
        baseline = self.original_session.model_copy(update={'runtime_type': CodexAgentRuntimeAdapter.runtime_type})
        self.sessions.store.upsert(baseline)
        if legacy_adoption:
            # Legacy discovery is idempotent, even with caller-supplied new
            # provenance. Only the subsequent trusted resume may change custody.
            for request in (self.request, self.request.model_copy(update={
                    'assignment_id': 'caller-chosen', 'execution_id': 'caller-chosen'})):
                adopted = self.sessions.adopt(provider_id=baseline.provider_id,
                    runtime_id=baseline.runtime_id, runtime_type=baseline.runtime_type,
                    provider_native_session_id='native-governor', request=request,
                    actor=self.actor, capability_snapshot=())
                self.assertEqual(adopted, baseline)
            self.assertEqual(self.sessions.get(baseline.id, self.actor), baseline)
        with self.assertRaises(Exception):
            self.coordination.validate_delegation(proof, thread_id=child, project_id='home')
        requests = []
        async def request(method, params=None):
            requests.append(method)
            if method == 'thread/resume':
                self.assertEqual(self.sessions.get(baseline.id, self.actor), baseline)
                if outcome == 'failure':
                    raise RuntimeError('provider resume failed')
                if outcome == 'cancel':
                    raise asyncio.CancelledError
                if outcome == 'unknown':
                    raise TimeoutError('resume outcome unknown')
                return {'thread': {'id': 'native-governor'}}
            if method == 'turn/start':
                # This assertion executes at the actual native-input transport
                # boundary, after the real resume-hook custody transaction.
                current = self.sessions.get(baseline.id, self.actor)
                self.assertEqual(current.assignment_id, self.assignment.id)
                self.assertEqual(current.execution_workspace_id, self.assignment.execution_workspace_id)
                self.coordination.validate_delegation(proof, thread_id=child, project_id='home')
                return {'turn': {'id': 'governor-turn'}}
            raise AssertionError('unexpected native RPC: '+method)
        self.worker_session.request = request
        self.worker_session.status = lambda: SimpleNamespace(worker_id=self.f.worker.id,
            fence=self.assignment.fence, running=True, ready=True)
        self.worker_session.runtime_binding = self.assignment.runtime_binding
        self.worker_session.runtime = SimpleNamespace(native_session_id='native-governor')
        manager = execution_tests._SessionManager()
        manager.session = self.worker_session
        manager.get = lambda assignment_id: self.worker_session if assignment_id == self.assignment.id else None
        actual = TurnExecutionService(self.host, binding_service=self.f.service,
            session_manager=manager, bootstrap_bindings=self.bindings, control_actor=self.actor)
        actual.session_custody = self.custody
        actual.automated_admission_allowed = lambda: False
        # Force the canonical isolated path; trusted-host fallback cannot fix a join.
        actual._trusted_local_codex_session_enabled = lambda **kwargs: False
        real_resume = CodexAgentRuntimeAdapter.resume_session
        observed = []
        async def observe_resume(adapter, native, request):
            observed.append(request)
            return await real_resume(adapter, native, request)
        with patch.object(CodexAgentRuntimeAdapter, 'resume_session', observe_resume):
            operation = actual.start_thread_turn_now('native-governor', project=self.f.project,
                message='bounded governor turn', sandbox='workspace-write', approval_policy='on-request',
                repository_resource_id=self.f.repository.id, execution_profile_id='project-governor', source='web')
            if outcome == 'success':
                response = await operation
                self.assertEqual(response['turn']['id'], 'governor-turn')
            elif outcome == 'failure':
                with self.assertRaisesRegex(RuntimeError, 'provider resume failed'):
                    await operation
            elif outcome == 'unknown':
                with self.assertRaisesRegex(TimeoutError, 'resume outcome unknown'):
                    await operation
            else:
                with self.assertRaises(asyncio.CancelledError):
                    await operation
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0].resource_ids, self.assignment.resource_ids)
        self.assertEqual(observed[0].assignment_id, self.assignment.id)
        self.assertEqual(observed[0].worker_id, self.f.worker.id)
        self.assertEqual(observed[0].execution_id, self.assignment.execution_id)
        if outcome == 'success':
            self.assertEqual(requests, ['thread/resume', 'turn/start'])
            queued, _ = await self.message(child, key='real-resume-hook-child')
            self.assertEqual(queued.project_coordination_dispatch.goal_binding_id, self.goal_binding.id)
        else:
            self.assertEqual(requests, ['thread/resume'])
            self.assertEqual(self.sessions.get(baseline.id, self.actor), baseline)
            with self.assertRaises(Exception):
                self.coordination.validate_delegation(proof, thread_id=child, project_id='home')

    async def test_actual_turn_resume_hook_precedes_input_and_goal_bound_child_admission(self):
        await self.exercise_actual_resume_path('success')

    async def test_actual_turn_failed_resume_does_not_reassociate_or_send_input(self):
        await self.exercise_actual_resume_path('failure')

    async def test_actual_turn_cancelled_resume_does_not_reassociate_or_send_input(self):
        await self.exercise_actual_resume_path('cancel')

    async def test_unknown_resume_does_not_reassociate_or_send_input(self):
        await self.exercise_actual_resume_path('unknown')

    async def test_legacy_adoption_is_idempotent_then_trusted_resume_repairs_custody(self):
        await self.exercise_actual_resume_path('success', legacy_adoption=True)

    async def test_worker_executor_future_stays_tracked_until_real_completion(self):
        entered, release, exited = threading.Event(), threading.Event(), threading.Event()
        pool = ThreadPoolExecutor(max_workers=1)
        owner = SimpleNamespace(_supervision_executor=pool, _executor_work=set())
        def writer():
            entered.set()
            try:
                release.wait(4)
            finally:
                exited.set()
        task = asyncio.create_task(TurnExecutionService._settle_custody_call(writer, work_owner=owner))
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(.005)
            self.assertEqual(len(owner._executor_work), 1)
            real_future = next(iter(owner._executor_work))
            self.assertFalse(real_future.done())
            task.cancel()
            task.cancel()
            await asyncio.sleep(.02)
            self.assertFalse(task.done())
            self.assertFalse(real_future.done())
        finally:
            release.set()
            try:
                await task
            except asyncio.CancelledError:
                pass
            pool.shutdown(wait=True)
        self.assertTrue(exited.is_set())
        self.assertEqual(owner._executor_work, set())

    def custody_rows(self):
        return (self.sessions.get(self.original_session.id, self.actor),
            self.f.sqlite.record_items(self.f.workers.store.event_namespace))

    def assert_refused_without_writes(self, proof):
        before = self.custody_rows()
        with self.assertRaises((AgentRuntimeError, ValueError)):
            self.complete(proof)
        self.assertEqual(self.custody_rows(), before)

    async def test_duplicate_native_identity_at_capture_and_commit_is_refused(self):
        proof = self.capture()
        duplicate = self.original_session.model_copy(update={'id': 'duplicate-native-session'})
        self.sessions.store.upsert(duplicate)
        with self.assertRaises(AgentRuntimeError):
            self.capture()
        self.assert_refused_without_writes(proof)
        self.assertEqual(self.sessions.get(duplicate.id, self.actor), duplicate)

    async def test_provider_runtime_and_runtime_type_mismatch_refuse(self):
        for field, value in (('provider_id', 'foreign-provider'), ('runtime_id', 'foreign-runtime'),
                             ('runtime_type', 'foreign-runtime-type')):
            with self.subTest(field=field):
                proof = self.capture()
                original = getattr(self.adapter, field)
                try:
                    setattr(self.adapter, field, value)
                    if field != 'runtime_type':
                        with self.assertRaises(AgentRuntimeError):
                            self.capture()
                    self.assert_refused_without_writes(proof)
                finally:
                    setattr(self.adapter, field, original)

    async def test_expired_assignment_and_foreign_worker_lease_refuse(self):
        store = self.f.workers.store
        original = store.assignment(self.assignment.id).model_dump(mode='json')
        cases = [dict(deadline_at=time.time()-1), dict(lease={**original['lease'], 'expires_at': time.time()-1}),
                 dict(lease={**original['lease'], 'worker_id': 'foreign-worker'}),
                 dict(assigned_worker_id='foreign-worker')]
        for changes in cases:
            with self.subTest(changes=changes):
                proof = self.capture()
                try:
                    self.f.sqlite.record_apply(store.assignment_namespace,
                        upserts={self.assignment.id: {**original, **changes}})
                    self.assert_refused_without_writes(proof)
                finally:
                    self.f.sqlite.record_apply(store.assignment_namespace, upserts={self.assignment.id: original})
        worker = self.f.sqlite.record_get(store.worker_namespace, self.f.worker.id)
        try:
            self.f.sqlite.record_apply(store.worker_namespace,
                upserts={self.f.worker.id: {**worker, 'organization_id': 'foreign-tenant'}})
            self.assert_refused_without_writes(self.capture())
        finally:
            self.f.sqlite.record_apply(store.worker_namespace, upserts={self.f.worker.id: worker})

    async def test_workspace_lease_scope_owner_expiry_and_binding_changes_refuse(self):
        store = self.f.workspaces.store
        workspace = store.workspace(self.assignment.execution_workspace_id)
        original = self.f.sqlite.record_get(store.lease_namespace, workspace.lease_id)
        for changes in ({'expires_at': time.time()-1}, {'owner_identity_id': 'foreign-owner'},
                        {'execution_workspace_id': 'foreign-workspace'},
                        {'organization_id': 'foreign-tenant'}, {'resource_ids': ['foreign-repository']}):
            with self.subTest(changes=changes):
                proof = self.capture()
                try:
                    self.f.sqlite.record_apply(store.lease_namespace, upserts={workspace.lease_id: {**original, **changes}})
                    self.assert_refused_without_writes(proof)
                finally:
                    self.f.sqlite.record_apply(store.lease_namespace, upserts={workspace.lease_id: original})
        proof = self.capture()
        state = self.bindings.store.load()
        self.bindings.store.update(lambda s: s.model_copy(update={'bindings': [
            b.model_copy(update={'execution_id': 'changed-during-resume'})
            if b.thread_id == 'native-governor' else b for b in s.bindings]}))
        try:
            self.assert_refused_without_writes(proof)
        finally:
            self.bindings.store.update(lambda _: state)

    def peer_backend(self):
        from codex_web.storage.sqlite_state import SQLiteStateStore
        return SQLiteStateStore(self.f.sqlite.path)

    def custody_on_backend(self, backend):
        # Separate domain-store instances on a separate database connection.
        # No lock, transaction or storage behavior is mocked.
        from codex_web.storage.agent_sessions import AgentSessionStore
        from codex_web.storage.execution_workers import ExecutionWorkerStore
        from codex_web.storage.execution_workspaces import ExecutionWorkspaceStateStore
        from codex_web.storage.thread_bootstrap_bindings import ThreadBootstrapBindingStore
        sessions, workers, bindings, workspaces = map(copy.copy,
            (self.sessions, self.f.workers, self.bindings, self.f.workspaces))
        sessions.store, workers.store = AgentSessionStore(backend), ExecutionWorkerStore(backend)
        bindings.store, workspaces.store = ThreadBootstrapBindingStore(backend), ExecutionWorkspaceStateStore(backend)
        return AgentSessionCustodyService(sessions, workers, bindings, workspaces, self.f.execution_profiles)

    async def test_two_connections_competing_resume_cas_has_one_atomic_winner(self):
        peer = self.peer_backend()
        other = self.custody_on_backend(peer)
        proofs = (self.capture(), other.capture('native-governor', self.adapter,
            self.worker_session, self.request, self.assignment.fence))
        before_events = self.custody_rows()[1]
        self.f.sqlite.record_apply('agent_runtime_usage.records.v1', upserts={'prior': {'input_tokens': 17}})
        usage = self.f.sqlite.record_items('agent_runtime_usage.records.v1')
        barrier = threading.Barrier(2)
        def resume(index):
            barrier.wait(timeout=5)
            try:
                return (self.custody, other)[index].complete(proofs[index], 'native-governor', self.adapter,
                    self.worker_session, self.request, self.assignment.fence,
                    AgentRuntimeResult(provider_native_session_id='native-governor'))
            except AgentRuntimeError as exc:
                return exc
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(resume, i) for i in range(2)]
            outcomes = [f.result(timeout=15) for f in futures]
        self.assertEqual(sum(isinstance(x, AgentRuntimeError) for x in outcomes), 1)
        session, events = self.custody_rows()
        self.assertEqual(session.assignment_id, self.assignment.id)
        added = set(events) - set(before_events)
        self.assertEqual(len(added), 1)
        self.assertEqual(events[added.pop()]['assignment_id'], session.assignment_id)
        self.assertEqual(self.f.sqlite.record_items('agent_runtime_usage.records.v1'), usage)
        self.assertEqual(other.sessions.get(session.id, self.actor), session)

    async def test_existing_event_conflict_cannot_partially_write_session(self):
        proof = self.capture()
        event_id = 'workerevent-session-custody-' + hashlib.sha256(
            f'{self.original_session.id}:{self.assignment.id}:{self.assignment.fence}'.encode()).hexdigest()
        self.f.sqlite.record_apply(self.f.workers.store.event_namespace,
            upserts={event_id: {'immutable': 'previous event must not be overwritten'}})
        self.assert_refused_without_writes(proof)
