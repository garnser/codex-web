"""Actual canonical writer interleavings, without provider mutation/replay."""
import asyncio
import hashlib
import json
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from codex_web.action_intents import ActionIntentClaimRequest
from codex_web.execution_workers import AssignmentCancelRequest, AssignmentCompleteRequest
from codex_web.procedure_discovery import ProcedureDiscovery
from codex_web.services.action_intents import ActionRequirementError
from codex_web.services.execution_workers import AssignmentReadinessOriginError, ExecutionWorkerService
from codex_web.storage.execution_workers import ExecutionWorkerStore
from tests import test_pull_request_ready as ready_fixture


class ReadinessOriginRetirementTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ready_fixture.PullRequestReadyTests.asyncSetUp
    asyncTearDown = ready_fixture.PullRequestReadyTests.asyncTearDown
    _assignment = staticmethod(ready_fixture.PullRequestReadyTests._assignment)
    _publish_worker_authority = ready_fixture.PullRequestReadyTests._publish_worker_authority
    _publish_profile = ready_fixture.PullRequestReadyTests._publish_profile
    _request = ready_fixture.PullRequestReadyTests._request
    respond = ready_fixture.PullRequestReadyTests.respond
    change_assignment = ready_fixture.PullRequestReadyTests.change_assignment
    public_ready = ready_fixture.PullRequestReadyTests.public_ready
    submit = ready_fixture.PullRequestReadyTests.submit

    async def pending_ready(self):
        response = await self.public_ready('retirement-race')
        self.assertEqual(response.status_code, 200, response.text)
        intent_id = response.json()['item']['id']
        claimed = self.intents.claim(ActionIntentClaimRequest(worker_id=self.worker.id,
            lease_seconds=180), actor=self.worker_actor, intent_id=intent_id)
        self.assertIsNotNone(claimed)
        discovery = ProcedureDiscovery(actor_identity_id=self.admin.identity_id,
            organization_id='local', workspace_id='default', project_id='project-a',
            resource_ids=(self.target.id,), operation='repository.read',
            context_checksum='exhausted', evaluated_at=time.time(), outcome='no_match',
            policy_source='canonical:retirement-fixture', policy_allows=True)
        while len(self.workers.store.assignment(self.assignment.id).procedure_discoveries) < 64:
            self.workers.record_procedure_discovery(self.assignment.id, discovery.model_copy(update={
                'context_checksum': str(len(self.workers.store.assignment(self.assignment.id).procedure_discoveries))}),
                expected_fence=self.assignment.fence, actor=self.admin)
        self.intents.store.unsettled_readiness_origin(self.assignment.id)
        return intent_id

    def cancel(self):
        return self.workers.cancel_bootstrap(self.assignment.id,
            AssignmentCancelRequest(expected_fence=self.assignment.fence,
                reason='idle exhausted assignment renewal'), actor=self.admin)

    async def test_attempt_commits_after_last_inspection_and_retirement_preserves_origin(self):
        intent_id = await self.pending_ready()
        before = self.workers.store.assignment(self.assignment.id)
        prepared = threading.Event()
        proceed = threading.Event()
        original = self.workers.store.transition_assignment
        def transition(*args, **kwargs):
            prepared.set()
            if not proceed.wait(10):
                raise AssertionError('retirement interleaving did not proceed')
            return original(*args, **kwargs)
        with patch.object(self.workers.store, 'transition_assignment', side_effect=transition):
            cancellation = asyncio.create_task(asyncio.to_thread(self.cancel))
            self.assertTrue(await asyncio.to_thread(prepared.wait, 10))
            try:
                attempted = await asyncio.to_thread(self.intents._mark_executing,
                    intent_id, self.worker.id, self.worker_actor)
                self.assertEqual(attempted.status.value, 'executing')
            finally:
                proceed.set()
            with self.assertRaises(AssignmentReadinessOriginError) as stopped:
                await cancellation
        self.assertEqual(stopped.exception.intent_ids, (intent_id,))
        self.assertEqual(self.workers.store.assignment(self.assignment.id), before)
        self.assertEqual(len(before.procedure_discoveries), 64)
        self.assertFalse(any(method == 'POST' for method, _ in self.calls))
        self.assertEqual(self.intents.get(intent_id, self.admin).attempt, 1)

    async def test_retirement_owns_writer_first_and_attempt_cannot_begin_provider_execution(self):
        intent_id = await self.pending_ready()
        # Independent worker facade proves database serialization, not a shared
        # Python repository mutex. All services still use this canonical DB.
        self.intents.execution_workers = ExecutionWorkerService(
            ExecutionWorkerStore(self.sqlite), identity=self.identity)
        writer_owned = threading.Event()
        release_writer = threading.Event()
        attempt_reached_storage = threading.Event()
        original_transition = self.workers.store.transition_assignment
        original_update = self.intents.store.update
        def transition(worker_id, assignment_id, updater, **kwargs):
            def paused(state):
                writer_owned.set()
                if not release_writer.wait(10):
                    raise AssertionError('writer interleaving did not proceed')
                return updater(state)
            return original_transition(worker_id, assignment_id, paused, **kwargs)
        def update(*args, **kwargs):
            attempt_reached_storage.set()
            return original_update(*args, **kwargs)
        with patch.object(self.workers.store, 'transition_assignment', side_effect=transition), \
                patch.object(self.intents.store, 'update', side_effect=update):
            cancellation = asyncio.create_task(asyncio.to_thread(self.cancel))
            self.assertTrue(await asyncio.to_thread(writer_owned.wait, 10))
            attempt = asyncio.create_task(asyncio.to_thread(self.intents._mark_executing,
                intent_id, self.worker.id, self.worker_actor))
            try:
                self.assertTrue(await asyncio.to_thread(attempt_reached_storage.wait, 10))
            finally:
                release_writer.set()
            cancelled = await cancellation
            with self.assertRaises(ActionRequirementError):
                await attempt
        self.assertEqual(cancelled.status.value, 'cancelled')
        self.assertEqual(cancelled.fence, self.assignment.fence + 1)
        self.assertIsNone(cancelled.lease)
        self.assertEqual(len(cancelled.procedure_discoveries), 64)
        current = self.intents.get(intent_id, self.admin)
        self.assertEqual(current.status.value, 'claimed')
        self.assertEqual(current.attempt, 0)
        self.assertFalse(any(method == 'POST' for method, _ in self.calls))

    async def test_ordinary_completion_also_preserves_unsettled_origin_and_exposes_owning_intent(self):
        from codex_web.api.execution_workers import _error
        intent_id = await self.pending_ready()
        self.intents._mark_executing(intent_id, self.worker.id, self.worker_actor)
        before = self.workers.store.assignment(self.assignment.id)
        with self.assertRaises(AssignmentReadinessOriginError) as stopped:
            self.workers.complete(self.worker.id, self.assignment.id,
                AssignmentCompleteRequest(fence=self.assignment.fence,
                    lease_token=self.assignment.lease.lease_token, succeeded=True),
                actor=self.worker_actor)
        self.assertEqual(self.workers.store.assignment(self.assignment.id), before)
        error = _error(stopped.exception)
        self.assertEqual(error.status_code, 409)
        self.assertEqual(error.detail['code'], 'assignment_readiness_origin_unsettled')
        self.assertEqual(error.detail['action_intent_ids'], [intent_id])
        self.assertEqual(error.detail['assignment_id'], self.assignment.id)

    async def test_genuinely_verified_ready_allows_ordinary_retirement(self):
        status, _, body = await self.submit('verified-before-retirement')
        self.assertEqual(status, 200, body)
        self.assertEqual(body['item']['status'], 'succeeded', body)
        history = self.intents.history(body['item']['id'], self.admin)
        self.assertTrue(history['verifications'][-1]['verified'])
        completed = self.workers.complete(self.worker.id, self.assignment.id,
            AssignmentCompleteRequest(fence=self.assignment.fence,
                lease_token=self.assignment.lease.lease_token, succeeded=True), actor=self.worker_actor)
        self.assertEqual(completed.status.value, 'succeeded')
        self.assertIsNone(completed.lease)

    async def test_incomplete_projection_refuses_retirement_without_backfill_under_writer(self):
        await self.pending_ready()
        before = self.workers.store.assignment(self.assignment.id)
        original = self.workers.store.transition_assignment
        def transition(*args, **kwargs):
            keys = tuple(key for key in self.sqlite.record_items(self.intents.store.records_namespace)
                if key.startswith('ready-origin-index:1.0:'))
            self.sqlite.record_apply(self.intents.store.records_namespace, upserts={}, deletes=keys)
            with patch.object(self.sqlite, 'record_mutate', side_effect=AssertionError(
                    'retirement must not perform migration writes under its guard')):
                return original(*args, **kwargs)
        with patch.object(self.workers.store, 'transition_assignment', side_effect=transition):
            with self.assertRaises(AssignmentReadinessOriginError) as stopped:
                self.cancel()
        self.assertEqual(stopped.exception.code, 'assignment_readiness_origin_unavailable')
        self.assertEqual(self.workers.store.assignment(self.assignment.id), before)

    async def test_native_supersession_exposes_known_before_input_refusal_without_retiring_session(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from codex_web.runtime.execution import TurnExecutionService, ExecutionPrestartRefusal
        intent_id = await self.pending_ready()
        self.intents._mark_executing(intent_id, self.worker.id, self.worker_actor)
        before = self.workers.store.assignment(self.assignment.id)
        async def complete(*args, **kwargs):
            return self.cancel()
        manager = SimpleNamespace(get=lambda assignment_id: object(), complete=complete)
        runtime = object.__new__(TurnExecutionService)
        runtime.host = SimpleNamespace(_append_bot_event=Mock())
        runtime._assignment_record = lambda assignment_id: before
        runtime.session_managers = {'openai/codex': manager}
        with self.assertRaises(ExecutionPrestartRefusal) as stopped:
            await runtime._supersede_thread_bootstrap(thread_id='fixture-native',
                project=SimpleNamespace(id='project-a'),
                runtime_binding=SimpleNamespace(provider_id='openai', runtime_id='codex'),
                sandbox='workspace-write', approval_policy='on-request',
                execution_profile_id='orchestration-admin', agent_profile=before.agent_profile,
                explicit_repository_id=None, previous_assignment_id=before.id,
                retirement_reason='procedure_assignment_exhausted')
        self.assertEqual(stopped.exception.detail['phase'], 'before_bootstrap_retirement')
        self.assertEqual(stopped.exception.detail['action_intent_ids'], [intent_id])
        self.assertEqual(self.workers.store.assignment(before.id), before)
        runtime.host._append_bot_event.assert_not_called()

    async def test_foreign_cancel_cannot_prime_or_mutate_readiness_projection(self):
        from codex_web.services.execution_workers import AssignmentNotFoundError
        await self.pending_ready()
        before_assignment = self.workers.store.assignment(self.assignment.id)
        before_records = self.sqlite.record_items(self.intents.store.records_namespace)
        foreign_admin = self.admin.model_copy(update={'organization_id': 'foreign-tenant'})
        with patch.object(self.workers, '_prepare_readiness_retirement',
                side_effect=AssertionError('foreign caller must not prime indexes')):
            with self.assertRaises(AssignmentNotFoundError):
                self.workers.cancel_bootstrap(self.assignment.id,
                    AssignmentCancelRequest(expected_fence=self.assignment.fence,
                        reason='foreign cancellation'), actor=foreign_admin)
        self.assertEqual(self.workers.store.assignment(self.assignment.id), before_assignment)
        self.assertEqual(self.sqlite.record_items(self.intents.store.records_namespace), before_records)

    async def test_cold_large_catalog_progresses_boundedly_before_retirement(self):
        from codex_web.action_intents import ActionIntentStatus
        from codex_web.services.execution_workers import AssignmentReadinessIndexProgressing
        response = await self.public_ready('cold-retirement')
        self.assertEqual(response.status_code, 200, response.text)
        current = self.intents.store.get(response.json()['item']['id'])
        terminal = current.model_copy(update={'status': ActionIntentStatus.SUCCEEDED,
            'organization_id': 'foreign-organization', 'workspace_id': 'foreign-workspace',
            'readiness_origin': None}).model_dump(mode='json')
        self.sqlite.record_apply(self.intents.store.records_namespace, upserts={
            'intents:terminal-%04d' % index: {**terminal, 'id': 'terminal-%04d' % index}
            for index in range(1100)})
        before = self.workers.store.assignment(self.assignment.id)
        with patch('codex_web.storage.action_intents._ORIGIN_INDEX_EPOCH', 'e' * 32):
            with patch.object(self.sqlite, 'record_items', side_effect=AssertionError(
                    'retirement must not load an unbounded catalog')):
                with self.assertRaises(AssignmentReadinessIndexProgressing) as progressing:
                    self.cancel()
                self.assertEqual(self.workers.store.assignment(self.assignment.id), before)
                detail = progressing.exception.detail()
                self.assertEqual(detail['code'], 'readiness_origin_index_progressing')
                self.assertTrue(detail['retryable'])
                self.assertNotIn('cursor', detail)
                self.assertEqual(detail['cursor_sha256'], hashlib.sha256(
                    progressing.exception.cursor.encode()).hexdigest())
                # The global migration cursor can identify a different tenant's
                # retained intent. Public conflicts expose only a progress hash.
                self.assertNotIn(progressing.exception.cursor, json.dumps(detail))
                self.assertEqual(detail['epoch'], 'e' * 32)
                # A distinct bounded preparation resumes the persisted cursor;
                # it performs no native/provider retry and no retirement yet.
                self.workers._prepare_readiness_retirement(self.assignment.id)
                self.assertEqual(self.workers.store.assignment(self.assignment.id), before)
                retired = self.cancel()
        self.assertEqual(retired.status.value, 'cancelled')
        self.assertEqual(retired.fence, before.fence + 1)
        self.assertIsNone(retired.lease)

    async def test_cold_index_queue_preserves_original_and_continues_same_process_cursor(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from codex_web.action_intents import ActionIntentStatus
        from codex_web.models import Project
        from codex_web.runtime.execution import TurnExecutionService
        from codex_web.services.execution_preflight import ExecutionPreflightService
        from codex_web.storage.execution_preflight import ExecutionPreflightStore
        from tests.test_turn_execution import _Host
        response=await self.public_ready('queued-cold-index')
        current=self.intents.store.get(response.json()['item']['id'])
        terminal=current.model_copy(update={'status':ActionIntentStatus.SUCCEEDED,'readiness_origin':None}).model_dump(mode='json')
        self.sqlite.record_apply(self.intents.store.records_namespace,upserts={
            'intents:terminal-%04d'%i:{**terminal,'id':'terminal-%04d'%i} for i in range(1100)})
        host=_Host();host._project=lambda _:Project(id='project-a',name='Project',path='/private')
        runtime=TurnExecutionService(host);runtime.schedule_queue_drain=Mock()
        preflight=ExecutionPreflightService(ExecutionPreflightStore(self.sqlite));runtime.queued_preflight=preflight
        original=runtime.enqueue_turn(thread_id='same-native',project_id='project-a',message='useful same request',source='web')
        snapshot=original.model_dump(mode='json');attempt=preflight.retain_queued(original,organization_id='local',workspace_id='default')
        before=self.workers.store.assignment(self.assignment.id);wakes=[];native=[];tries=0
        async def complete(*args,**kwargs):return self.cancel()
        runtime.session_managers={'openai/codex':SimpleNamespace(get=lambda _:object(),complete=complete)}
        runtime._assignment_record=lambda _:before
        async def start(*args,**kwargs):
            nonlocal tries
            tries+=1
            if tries==1:
                await runtime._supersede_thread_bootstrap(thread_id='same-native',project=host._project(None),
                    runtime_binding=SimpleNamespace(provider_id='openai',runtime_id='codex'),sandbox='workspace-write',
                    approval_policy='on-request',execution_profile_id='orchestration-admin',agent_profile=before.agent_profile,
                    explicit_repository_id=None,previous_assignment_id=before.id,retirement_reason='procedure_assignment_exhausted')
            self.cancel()  # Same process finishes the real index and canonical retirement.
            from codex_web.execution_workers import ExecutionAssignmentCreate, AssignmentClaimRequest, AssignmentStartRequest
            from codex_web.runtime.codex import CodexRuntime
            from codex_web.services.queued_native_start import QueuedNativeStartCapture
            from tests import test_codex_runtime as wire_fixture
            renewed=self.workers.create_assignment(ExecutionAssignmentCreate(
                subject=before.subject,execution_id=before.execution_id+'-renewed',project_id=before.project_id,
                resource_ids=before.resource_ids,execution_contract_version=before.execution_contract_version,
                required_capabilities=before.required_capabilities,sandbox=before.sandbox,approval_policy=before.approval_policy,
                execution_profile_id=before.execution_profile_id,execution_profile_definition=before.execution_profile_definition,
                agent_profile=before.agent_profile),actor=self.admin)
            claimed=self.workers.claim(self.worker.id,AssignmentClaimRequest(),actor=self.worker_actor,assignment_id=renewed.id)
            renewed=self.workers.start(self.worker.id,renewed.id,AssignmentStartRequest(
                fence=claimed.fence,lease_token=claimed.lease.lease_token),actor=self.worker_actor)
            admission=preflight.store.get(attempt.id)
            capture=QueuedNativeStartCapture(preflight,attempt.id,admission.queued_admission_claim_id,
                admission.execution_id,'same-native',self.admin.identity_id).bind_assignment(renewed,
                    worker_id=self.worker.id,fence=renewed.fence,
                    validator=lambda:self.workers.assignment_for_execution(renewed.execution_id,actor=self.admin))
            wire=CodexRuntime(wire_fixture._Host(),restart_on_timeout=False);wire.ensure_started=AsyncMock()
            async def send(message,*,start_guard,expected_generation):
                start_guard();native.append(kwargs['message'])
                await wire._dispatch_message({'id':message['id'],'result':{'thread':{'id':'same-native'},
                    'turn':{'id':'actual-original-start','threadId':'same-native','status':'inProgress'}}})
            wire._send=send
            return await wire.request('turn/start',{'threadId':'same-native','input':[{'type':'text','text':kwargs['message']}]},start_capture=capture)
        runtime.start_thread_turn_now=start
        with patch('codex_web.storage.action_intents._ORIGIN_INDEX_EPOCH','d'*32):
            with patch.object(asyncio.get_running_loop(),'call_later',side_effect=lambda *a:wakes.append(a)):
                await runtime.drain_thread_queue('same-native')
            self.assertEqual(self.workers.store.assignment(self.assignment.id),before)
            self.assertEqual([q.model_dump(mode='json') for q in host._thread_queue('same-native')],[snapshot])
            self.assertEqual(preflight.store.get(attempt.id).status,'queued');self.assertEqual(len(wakes),1)
            self.assertEqual(native,[])
            await runtime.drain_thread_queue('same-native');await runtime.drain_thread_queue('same-native')
        self.assertEqual(native,[original.message]);self.assertEqual(preflight.store.get(attempt.id).status,'started')
        self.assertEqual(preflight.store.get(attempt.id).queued_turn_snapshot,snapshot)
        self.assertEqual(preflight.store.get(attempt.id).queued_start_receipt.provider_native_turn_id,'actual-original-start')
        self.assertIsNotNone(preflight.store.get(attempt.id).queued_start_evidence)
        self.assertEqual(self.workers.store.assignment(self.assignment.id).status.value,'cancelled')
