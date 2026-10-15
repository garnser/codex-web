from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from pydantic import ValidationError, create_model

from codex_web.execution_workers import ExecutionAssignment, AssignmentLease, NetworkPolicy, WorkerResourceLimits
from codex_web.identity import AuthenticationAssurance
from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.models import ExecutionPreflightAttempt, QueuedTurn, Project, QueuedSchedulingRetirement
from codex_web.resources import RepositoryExecutionScope
from codex_web.runtime.codex import CodexRuntime
from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSession
from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.queued_native_start import QueuedNativeStartCapture, reconcile_captured_start
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.turn_queue import TurnQueueRepository
from tests.test_codex_runtime import _Host
from tests.test_execution_preflight import actor


class QueuedNativeStartTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / 'state.sqlite'
        self.service = ExecutionPreflightService(ExecutionPreflightStore(SQLiteStateStore(self.db)))
        self.original = QueuedTurn(id='original', execution_id='requested', thread_id='thread', created_at=1,
            project_id='project', message='immutable original', work_item_ref='group/repo#43', agent_profile_actor_id='admin',
            agent_profile_id='profile', agent_profile_revision=10,
            repository_resource_id='repo', writable_repository_resource_ids=('repo',),
            execution_profile_id='repository-write')
        self.attempt = self.service.retain_queued(self.original, organization_id='org', workspace_id='ws')
        _, self.claim = self.service.claim_queued_admission(self.attempt.id)
        self.service.begin_queued_start(self.attempt.id, claim_id=self.claim)
        self.assignment = ExecutionAssignment(id='assignment', organization_id='org', workspace_id='ws',
            subject={'kind':'thread_bootstrap', 'ref':'bootstrap-test'},
            project_id='project', execution_id='bootstrap-execution', resource_ids=('repo',),
            execution_contract_version='1.0', required_capabilities=(), sandbox='workspace-write',
            approval_policy='on-request', network=NetworkPolicy(), limits=WorkerResourceLimits(),
            created_by='admin', assigned_worker_id='worker', fence=7,
            execution_profile_id='repository-write',
            agent_profile=AgentProfileExecutionBinding(profile_id='profile', profile_revision=10,
                                                      profile_record_id='profile-record'),
            repository_scope=RepositoryExecutionScope(organization_id='org', workspace_id='ws',
                project_id='project', source='explicit', writable_repository_ids=('repo',)),
            lease=AssignmentLease(worker_id='worker', fence=7, lease_token='private-lease-token-never-persist',
                                  acquired_at=1, expires_at=10))
        self.capture = QueuedNativeStartCapture(self.service, self.attempt.id, self.claim,
            'requested', 'thread', 'admin').bind_assignment(self.assignment, worker_id='worker', fence=7,
                                                          validator=lambda:self.assignment)
        self.runtime = CodexRuntime(_Host(), restart_on_timeout=False)
        self.runtime.ensure_started = AsyncMock()
        self.runtime._send = AsyncMock()

    def saved(self):
        return ExecutionPreflightStore(SQLiteStateStore(self.db)).get(self.attempt.id)

    def prepare(self):
        return self.capture.prepare(generation='generation', rpc_request_id='rpc-original', native_session_id='thread')

    def response(self, rpc_id='rpc-original', **turn):
        return {'id':rpc_id, 'result':{'thread':{'id':'thread'},
            'turn':{'id':'native-turn', 'threadId':'thread', 'status':'inProgress', **turn}}}

    def retirement_fixture(self):
        from tests.test_queued_scheduling_retirement import QueuedSchedulingRetirementTests
        fixture = QueuedSchedulingRetirementTests()
        fixture.setUp()
        self.addCleanup(fixture.temp.cleanup)
        fixture.service.preflight = self.service
        fixture.queues = fixture.service.queues = TurnQueueRepository(
            self.service.store.store, Path(self.directory.name)/'queues.json')
        fixture.queues.put('thread', [self.original, fixture.later])
        prepared = QueuedSchedulingRetirement(queued_turn_id=self.original.id,
            queued_snapshot_sha256=self.service.queued_snapshot_digest(self.original.model_dump(mode='json')),
            work_item_ref='group/repo#43', source_identity=fixture.item.source_identity,
            source_state='merged', retired_by='admin', prepared_at=200)
        return fixture, prepared

    def capture_without_projection(self, correlation):
        with patch('codex_web.services.queued_native_start.reconcile_captured_start', side_effect=RuntimeError('projection crash')):
            with self.assertRaises(RuntimeError):
                self.capture.observe(correlation, generation='generation', message=self.response())

    async def test_pending_positive_evidence_blocks_retirement_in_service_and_atomic_prepare(self):
        fixture, prepared = self.retirement_fixture()
        self.capture_without_projection(self.prepare())
        source = fixture.service.source_detail = AsyncMock(wraps=fixture.service.source_detail)
        with self.assertRaises(HTTPException):
            await fixture.service.retire(self.attempt.id, thread_id='thread', actor=actor(admin=True))
        source.assert_not_awaited()
        with self.assertRaises(HTTPException):
            self.service.prepare_scheduling_retirement(self.attempt.id, evidence=prepared,
                expected_snapshot=self.original.model_dump(mode='json'))
        self.assertEqual(fixture.queues.get('thread'), [self.original, fixture.later])
        self.assertIsNone(self.saved().scheduling_retirement)
        self.assertEqual(fixture.audit, [])
        self.assertEqual(self.service.reconcile_prestart_refusal(self.attempt.id,
            actor=actor(admin=True)).status, 'started')
        self.runtime._send.assert_not_called()

    async def test_positive_capture_during_retirement_provider_await_preserves_queue_and_reconciliation(self):
        fixture, _ = self.retirement_fixture()
        entered, release = asyncio.Event(), asyncio.Event()
        source = fixture.service.source_detail
        async def held_source(ref):
            entered.set()
            await release.wait()
            return await source(ref)
        fixture.service.source_detail = held_source
        task = asyncio.create_task(fixture.service.retire(self.attempt.id,
            thread_id='thread', actor=actor(admin=True)))
        await asyncio.wait_for(entered.wait(), 2)
        try:
            self.capture_without_projection(self.prepare())
            release.set()
            with self.assertRaises(HTTPException) as caught:
                await task
            self.assertEqual(caught.exception.detail['code'], 'scheduling_retirement_conflict')
        finally:
            release.set()
            if not task.done():
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(fixture.queues.get('thread'), [self.original, fixture.later])
        self.assertIsNone(self.saved().scheduling_retirement)
        self.assertIsNotNone(self.saved().queued_start_evidence)
        self.assertEqual(self.service.reconcile_prestart_refusal(self.attempt.id,
            actor=actor(admin=True)).status, 'started')
        self.runtime._send.assert_not_called()

    async def test_retirement_prepared_first_prevents_native_reservation_and_send(self):
        fixture, prepared = self.retirement_fixture()
        self.service.prepare_scheduling_retirement(self.attempt.id, evidence=prepared,
            expected_snapshot=self.original.model_dump(mode='json'))
        with self.assertRaises(ValueError):
            await self.runtime.request('turn/start', {'threadId':'thread'}, start_capture=self.capture)
        self.runtime._send.assert_not_called()
        self.assertIsNone(self.saved().queued_start_correlation)
        self.assertIsNone(self.saved().queued_start_evidence)
        self.assertEqual(fixture.queues.get('thread'), [self.original, fixture.later])

    async def test_correlated_unknown_keeps_retirement_and_later_exact_positive_never_requeues(self):
        fixture, _ = self.retirement_fixture()
        correlation = self.prepare()
        self.assertTrue(self.service.public(self.saved(), actor=actor(admin=True))['can_retire_scheduling'])
        source = fixture.service.source_detail = AsyncMock(wraps=fixture.service.source_detail)
        retired = await fixture.service.retire(self.attempt.id, thread_id='thread', actor=actor(admin=True))
        proof = retired.scheduling_retirement
        self.assertEqual(proof.native_outcome, 'unknown')
        self.capture.observe(correlation, generation='generation', message=self.response())
        saved = self.saved()
        self.assertEqual(saved.scheduling_retirement, proof)
        self.assertEqual(saved.status, 'started')
        self.assertEqual(saved.queued_start_receipt.provider_native_turn_id, 'native-turn')
        self.assertEqual(saved.queued_turn_snapshot, self.original.model_dump(mode='json'))
        self.assertEqual(fixture.queues.get('thread'), [fixture.later])
        source.assert_awaited_once()
        self.runtime._send.assert_not_called()

    async def test_late_positive_between_retirement_prepare_and_queue_removal_preserves_both_facts(self):
        fixture, _ = self.retirement_fixture()
        correlation = self.prepare()
        remove = fixture.queues.remove_exact
        def late_positive(thread, queued):
            prepared = self.saved().scheduling_retirement
            self.assertEqual(prepared.state, 'prepared')
            self.capture.observe(correlation, generation='generation', message=self.response())
            self.assertEqual(self.saved().scheduling_retirement, prepared)
            return remove(thread, queued)
        with patch.object(fixture.queues, 'remove_exact', late_positive):
            retired = await fixture.service.retire(self.attempt.id, thread_id='thread', actor=actor(admin=True))
        self.assertEqual(retired.status, 'started')
        self.assertEqual(retired.queued_start_receipt.provider_native_turn_id, 'native-turn')
        self.assertEqual(retired.scheduling_retirement.state, 'retired')
        self.assertEqual(retired.scheduling_retirement.native_outcome, 'unknown')
        self.assertEqual(fixture.audit[0].payload['nativeOutcomeAtAudit'], 'started')
        self.assertEqual(fixture.audit[0].payload['nativeOutcomeAsOf'], retired.scheduling_retirement.prepared_at)
        self.assertEqual(fixture.queues.get('thread'), [fixture.later])
        self.runtime._send.assert_not_called()

    async def test_prepared_retirement_with_late_pending_positive_can_reconcile_then_finish_scheduling(self):
        fixture, _ = self.retirement_fixture()
        correlation = self.prepare()
        source = fixture.service.source_detail = AsyncMock(wraps=fixture.service.source_detail)
        with patch.object(fixture.queues, 'remove_exact', side_effect=RuntimeError('interrupted queue removal')):
            with self.assertRaises(RuntimeError):
                await fixture.service.retire(self.attempt.id, thread_id='thread', actor=actor(admin=True))
        proof = self.saved().scheduling_retirement
        self.assertEqual(proof.state, 'prepared')
        self.capture_without_projection(correlation)
        self.assertTrue(self.service.public(self.saved(), actor=actor(admin=True))['can_reconcile_prestart'])
        self.service.reconcile_prestart_refusal(self.attempt.id, actor=actor(admin=True))
        source.assert_awaited_once()
        self.assertEqual(fixture.queues.get('thread'), [self.original, fixture.later])
        self.assertEqual(self.saved().scheduling_retirement, proof)
        result = await fixture.service.retire(self.attempt.id, thread_id='thread', actor=actor(admin=True))
        self.assertEqual(result.status, 'started')
        self.assertEqual(result.scheduling_retirement.state, 'retired')
        self.assertEqual(fixture.queues.get('thread'), [fixture.later])
        self.runtime._send.assert_not_called()

    async def test_positive_and_retirement_cas_ordering_always_preserves_exact_positive(self):
        _, prepared = self.retirement_fixture()
        correlation = self.prepare()
        def retire():
            return self.service.prepare_scheduling_retirement(self.attempt.id, evidence=prepared,
                expected_snapshot=self.original.model_dump(mode='json'))
        def observe():
            return self.capture.observe(correlation, generation='generation', message=self.response())
        results = await asyncio.gather(asyncio.to_thread(observe), asyncio.to_thread(retire),
                                      return_exceptions=True)
        self.assertIs(results[0], True)
        saved = self.saved()
        self.assertEqual(saved.status, 'started')
        self.assertIsNotNone(saved.queued_start_evidence)
        if saved.scheduling_retirement is None:
            self.assertIsInstance(results[1], HTTPException)
        else:
            self.assertEqual(saved.scheduling_retirement, prepared)
            self.assertNotIsInstance(results[1], Exception)
        self.runtime._send.assert_not_called()

    async def timed_out_request(self, capture=None):
        with patch('codex_web.runtime.codex.request_timeout', return_value=.001):
            with self.assertRaises(HTTPException) as caught:
                await self.runtime.request('turn/start', {'threadId':'thread'},
                                           start_capture=capture or self.capture)
        self.assertEqual(caught.exception.status_code, 504)
        self.assertEqual(self.runtime.pending, {})
        return self.runtime._send.call_args.args[0]['id']

    async def test_exact_positive_after_waiter_timeout_reconciles_once_without_native_replay(self):
        rpc_id = await self.timed_out_request()
        self.assertIsNone(self.saved().queued_start_receipt)
        await self.runtime._dispatch_message(self.response(rpc_id))
        saved = self.saved()
        self.assertEqual(saved.status, 'started')
        self.assertEqual(saved.queued_start_receipt.provider_native_turn_id, 'native-turn')
        self.assertEqual(saved.queued_start_receipt.execution_id, 'bootstrap-execution')
        self.assertEqual(saved.queued_turn_snapshot, self.original.model_dump(mode='json'))
        before = saved.model_dump(mode='json')
        await self.runtime._dispatch_message(self.response(rpc_id))
        reconcile_captured_start(self.service, saved.id)
        self.assertEqual(self.saved().model_dump(mode='json'), before)
        self.runtime._send.assert_awaited_once()
        serialized = json.dumps(before)
        self.assertNotIn('private-lease-token', serialized)
        self.assertNotIn('lease_token', serialized)

    async def test_captured_positive_survives_projection_failure_and_independent_interpreter(self):
        correlation = self.prepare()
        with patch('codex_web.services.queued_native_start.reconcile_captured_start',
                   side_effect=RuntimeError('controlled receipt projection failure')):
            with self.assertRaises(RuntimeError):
                self.capture.observe(correlation, generation='generation', message=self.response())
        saved = self.saved()
        self.assertEqual(saved.status, 'reconciliation_required')
        self.assertIsNotNone(saved.queued_start_evidence)
        self.assertTrue(self.service.public(saved, actor=actor(admin=True))['can_reconcile_prestart'])
        # Fresh interpreter has no live transport, worker lease, or capture object.
        code = """
import sys
from pathlib import Path
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.services.execution_preflight import ExecutionPreflightService
from tests.test_execution_preflight import actor
s = ExecutionPreflightService(ExecutionPreflightStore(SQLiteStateStore(Path(sys.argv[1]))))
a = s.reconcile_prestart_refusal(sys.argv[2], actor=actor(admin=True))
assert a.status == 'started'
assert a.queued_start_receipt.provider_native_turn_id == 'native-turn'
"""
        result = await asyncio.to_thread(subprocess.run, [sys.executable, '-c', code, str(self.db), saved.id],
                                        capture_output=True, text=True, timeout=20, env=os.environ.copy())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.saved().status, 'started')
        self.runtime._send.assert_not_called()

    async def test_concurrent_evidence_and_reconciliation_are_idempotent(self):
        correlation = self.prepare()
        with ThreadPoolExecutor(max_workers=4) as executor:
            outcomes = await asyncio.gather(*(asyncio.get_running_loop().run_in_executor(executor,
                lambda:self.capture.observe(correlation, generation='generation', message=self.response()))
                for _ in range(4)))
        self.assertTrue(all(outcomes))
        self.assertEqual(self.saved().status, 'started')
        self.runtime._send.assert_not_called()

    async def test_event_or_session_read_is_not_positive_request_evidence(self):
        rpc_id = await self.timed_out_request()
        # Native notification naming and identical turn/session IDs carry no RPC association.
        for message in (
            {'method':'turn/started', 'params':{'threadId':'thread', 'turn':{'id':'native-turn'}}},
            {'id':'unassociated-read', 'result':{'thread':{'id':'thread', 'turns':[{'id':'native-turn'}]}}},
            {'id':rpc_id, 'result':{'thread':{'id':'thread', 'turns':[{'id':'native-turn'}]}}}):
            await self.runtime._dispatch_message(message)
        self.assertEqual(self.saved().status, 'reconciliation_required')
        self.assertIsNone(self.saved().queued_start_evidence)
        notification_task = getattr(self.runtime, 'notification_task', None)
        if notification_task:
            await self.runtime.notification_queue.join()
            notification_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await notification_task

    async def test_error_malformed_and_foreign_positive_payloads_are_not_start_proof(self):
        correlation = self.prepare()
        for result in (None, {}, {'turn':None}, {'turn':{'id':1}}, {'turn':{'id':' '}},
            {'turn':{'id':'native-turn', 'threadId':'foreign'}},
            {'thread':{'id':'foreign'}, 'turn':{'id':'native-turn'}},
            {'threadId':'foreign', 'turn':{'id':'native-turn'}},
            {'turn':{'id':'native-turn', 'status':'invented'}},
            {'turn':{'id':'native-turn', 'error':{'message':'failed'}}}):
            with self.subTest(result=result):
                self.assertFalse(self.capture.observe(correlation, generation='generation',
                    message={'id':'rpc-original', 'result':result}))
        self.assertFalse(self.capture.observe(correlation, generation='generation',
            message={**self.response(), 'error':{'message':'turn/started'}}))
        self.assertIsNone(self.saved().queued_start_evidence)

    async def test_changed_original_scope_claim_or_correlation_cannot_accept_response(self):
        correlation = self.prepare()
        baseline = self.saved().model_dump(mode='json')
        changes = {'organization_id':'foreign', 'workspace_id':'foreign', 'project_id':'foreign',
            'repository_resource_id':'foreign', 'writable_repository_resource_ids':['foreign'],
            'read_only_repository_resource_ids':['foreign'], 'agent_profile_actor_id':'foreign',
            'agent_profile_id':'foreign', 'agent_profile_revision':11, 'execution_profile_id':'foreign',
            'queued_admission_claim_id':'foreign'}
        for key, value in changes.items():
            with self.subTest(key=key):
                self.service.store.store.record_apply(self.service.store.NAMESPACE, upserts={
                    'a/'+self.attempt.id:{**baseline, key:value}})
                with self.assertRaises(ValueError):
                    self.capture.observe(correlation, generation='generation', message=self.response())
        for field, value in (('fence', 8), ('worker_id','foreign'), ('assignment_id','foreign'),
                             ('provider_native_session_id','foreign'), ('runtime_generation','foreign')):
            with self.subTest(field=field):
                changed = {**baseline['queued_start_correlation'], field:value}
                self.service.store.store.record_apply(self.service.store.NAMESPACE, upserts={
                    'a/'+self.attempt.id:{**baseline, 'queued_start_correlation':changed}})
                with self.assertRaises(ValueError):
                    self.capture.observe(correlation, generation='generation', message=self.response())
        self.service.store.store.record_apply(self.service.store.NAMESPACE, upserts={'a/'+self.attempt.id:baseline})
        self.assertFalse(self.capture.observe(correlation, generation='foreign', message=self.response()))
        self.assertFalse(self.capture.observe(correlation, generation='generation', message=self.response('reused-rpc')))

    async def test_expired_current_lease_does_not_erase_exact_historical_fact(self):
        correlation = self.prepare()
        self.assignment.fence = 8
        self.assignment.lease = None
        # No current assignment lookup, lease renewal or native call is needed.
        self.capture.observe(correlation, generation='generation', message=self.response())
        self.assertEqual(self.saved().queued_start_receipt.provider_native_turn_id, 'native-turn')
        self.assertEqual(self.saved().queued_start_correlation.fence, 7)
        self.runtime._send.assert_not_called()

    async def test_generation_retirement_ttl_and_rpc_id_reuse_never_attach_late_response(self):
        rpc_id = await self.timed_out_request()
        old_generation = self.runtime.generation
        self.runtime._fail_pending(RuntimeError('retired'))
        self.runtime.generation = 'new-generation'
        future = asyncio.get_running_loop().create_future()
        self.runtime.pending[rpc_id] = future
        await self.runtime._dispatch_message(self.response(rpc_id), generation=old_generation)
        self.assertFalse(future.done())
        self.assertIsNone(self.saved().queued_start_evidence)
        future.cancel()
        self.runtime.pending.clear()
        # Expiration also loses the opportunity to capture, never proves nonstart.
        self.runtime._start_captures[rpc_id] = (0, self.capture, self.saved().queued_start_correlation)
        await self.runtime._dispatch_message(self.response(rpc_id))
        self.assertEqual(self.saved().status, 'reconciliation_required')
        self.assertFalse(self.runtime._start_captures)

    async def test_storage_failure_and_capacity_prevent_send_and_preserve_unknown(self):
        with patch.object(self.capture.service.store, 'update', side_effect=RuntimeError('storage unavailable')):
            with self.assertRaises(RuntimeError):
                await self.runtime.request('turn/start', {'threadId':'thread'}, start_capture=self.capture)
        self.runtime._send.assert_not_called()
        with patch.object(self.runtime, 'START_CAPTURE_MAX_SLOTS', 0):
            with self.assertRaises(RuntimeError):
                await self.runtime.request('turn/start', {'threadId':'thread'}, start_capture=self.capture)
        self.runtime._send.assert_not_called()
        self.assertEqual(self.saved().status, 'reconciliation_required')

    async def test_transport_write_failure_keeps_unknown_without_replay_or_refusal_from_text(self):
        self.runtime._send.side_effect = RuntimeError('upgrade_maintenance turn/started')
        with self.assertRaises(RuntimeError):
            await self.runtime.request('turn/start', {'threadId':'thread'}, start_capture=self.capture)
        self.assertFalse(self.runtime.pending)
        self.assertIsNotNone(self.saved().queued_start_correlation)
        self.assertIsNone(self.saved().queued_start_evidence)
        self.service.queued_disposition(self.attempt.id, status='queued', claim_id=self.claim)
        self.assertEqual(self.saved().status, 'reconciliation_required')
        _, acquired = self.service.store.claim_retry(self.attempt.id, claim_id='retry', now=999)
        self.assertFalse(acquired)
        self.assertFalse(self.service.public(self.saved(), actor=actor(admin=True))['can_retry'])
        self.runtime._send.assert_awaited_once()

    async def test_pending_positive_projection_uses_existing_admin_authority_without_provider_calls(self):
        correlation = self.prepare()
        with patch('codex_web.services.queued_native_start.reconcile_captured_start', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                self.capture.observe(correlation, generation='generation', message=self.response())
        with self.assertRaises(HTTPException):
            self.service.reconcile_prestart_refusal(self.attempt.id, actor=actor(admin=False))
        with self.assertRaises(HTTPException):
            self.service.reconcile_prestart_refusal(self.attempt.id, actor=actor(admin=True).model_copy(
                update={'assurance':AuthenticationAssurance.PRIMARY}))
        saved = self.service.reconcile_prestart_refusal(self.attempt.id, actor=actor(admin=True))
        self.assertEqual(saved.status, 'started')
        public = self.service.public(saved, actor=actor(admin=True))
        self.assertFalse(public['can_retry'])
        self.assertFalse(public['can_reconcile_prestart'])
        self.assertFalse(public['can_retire_scheduling'])
        self.runtime._send.assert_not_called()

    async def test_generation_retirement_during_preparation_prevents_send(self):
        release, entered = threading.Event(), threading.Event()
        prepare = QueuedNativeStartCapture.prepare
        def held(capture, **kwargs):
            correlation = prepare(capture, **kwargs)
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test preparation gate timed out')
            return correlation
        with patch.object(QueuedNativeStartCapture, 'prepare', held):
            task = asyncio.create_task(self.runtime.request('turn/start', {'threadId':'thread'},
                                                           start_capture=self.capture))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                await self.runtime.stop()
                release.set()
                with self.assertRaises(RuntimeError):
                    await task
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
        self.runtime._send.assert_not_called()
        self.assertFalse(self.runtime._start_captures)
        self.assertEqual(self.saved().status, 'reconciliation_required')

    async def test_turn_execution_keeps_positive_receipt_when_caller_loses_already_captured_response(self):
        from tests.test_turn_execution import TurnExecutionStartTests
        host, _, sessions, execution = TurnExecutionStartTests()._service(bootstrap_thread_id='t1')
        project = Project(id='p1', name='Project', path='/workspace/project',
                          sandbox='workspace-write', approval_policy='on-request')
        host._project = lambda _:project
        execution.queued_preflight = self.service
        execution.schedule_queue_drain = Mock()
        assignment = self.assignment.model_copy(update={'id':'assignment-1', 'organization_id':'local',
            'workspace_id':'default', 'project_id':'p1', 'agent_profile':None,
            'execution_profile_id':None, 'execution_id':'bootstrap-exec',
            'execution_workspace_id':'workspace-1'})
        sessions.session.validate_current = lambda:assignment
        sessions.session.status = lambda:SimpleNamespace(worker_id='worker', fence=7)
        sessions.session.runtime = self.runtime
        self.runtime.proc = SimpleNamespace(poll=lambda:None, stdin=io.StringIO())
        send = self.runtime._send = Mock(wraps=CodexRuntime._send.__get__(self.runtime))
        async def deliver(message, **kwargs):
            await send(message, **kwargs)
            response = ({'thread':{'id':'t1'}} if message['method']=='thread/resume'
                        else {'turn':{'id':'native-captured', 'threadId':'t1'}})
            await self.runtime._dispatch_message({'id':message['id'], 'result':response})
            if message['method']=='turn/start':
                raise RuntimeError('controlled caller response loss after reader capture')
        self.runtime._send = deliver
        sessions.session.request = self.runtime.request
        async def capture_request(method, params, capture):
            bound = capture.bind_assignment(assignment, worker_id='worker', fence=7,
                                             validator=sessions.session.validate_current)
            return await self.runtime.request(method, params, start_capture=bound)
        sessions.session.request_with_start_capture = capture_request
        original = execution.enqueue_turn(thread_id='t1', project_id='p1', message='captured original',
                                          execution_id='captured-execution')
        original_snapshot = original.model_dump(mode='json')
        await execution.drain_thread_queue('t1')
        retained = self.service.store.get('preflight-queued-'+original.id)
        self.assertEqual(retained.status, 'started', (retained.last_error, host.events))
        self.assertEqual(retained.queued_start_receipt.provider_native_turn_id, 'native-captured')
        self.assertEqual(retained.queued_turn_snapshot, original_snapshot)
        self.assertEqual(sessions.completed, [])
        self.assertEqual(sum(c.args[0]['method']=='turn/start' for c in send.call_args_list), 1)

    async def test_encrypted_recovery_artifact_retains_exact_positive_evidence_and_original(self):
        from tests.test_recovery import RecoveryTests
        fixture = RecoveryTests()
        fixture.setUp()
        try:
            service = ExecutionPreflightService(ExecutionPreflightStore(fixture.state))
            original = self.original.model_copy(update={'id':'recovery-original'})
            retained = service.retain_queued(original, organization_id='org-a', workspace_id='ws-a')
            _, claim = service.claim_queued_admission(retained.id)
            service.begin_queued_start(retained.id, claim_id=claim)
            assignment = self.assignment.model_copy(update={'organization_id':'org-a','workspace_id':'ws-a'})
            capture = QueuedNativeStartCapture(service, retained.id, claim, original.execution_id,
                original.thread_id, 'admin').bind_assignment(assignment, worker_id='worker', fence=7,
                                                             validator=lambda:assignment)
            correlation = capture.prepare(generation='recovery-generation', rpc_request_id='rpc-original',
                                          native_session_id='thread')
            with patch('codex_web.services.queued_native_start.reconcile_captured_start', side_effect=RuntimeError('crash')):
                with self.assertRaises(RuntimeError):
                    capture.observe(correlation, generation='recovery-generation', message=self.response())
            backup = fixture.service.create_backup(actor=fixture.actor)
            target = SQLiteStateStore(Path(self.directory.name)/'recovery-target.sqlite')
            verified = fixture.service.restore_into_isolated(backup.id, target, actor=fixture.actor)
            self.assertEqual(verified.status, 'pass')
            self.assertFalse(verified.side_effects_enabled)
            restored_service = ExecutionPreflightService(ExecutionPreflightStore(target))
            restored = restored_service.store.get(retained.id)
            self.assertEqual(restored.queued_start_correlation, correlation)
            self.assertIsNotNone(restored.queued_start_evidence)
            self.assertEqual(restored.queued_turn_snapshot, original.model_dump(mode='json'))
            self.assertEqual(reconcile_captured_start(restored_service, retained.id).status, 'started')
            self.runtime._send.assert_not_called()
        finally:
            fixture.tearDown()

    async def test_session_boundary_binds_actual_assignment_and_keeps_timeout_retirement(self):
        session = object.__new__(AssignmentBoundAgentProcessSession)
        session.local_worker = SimpleNamespace(worker=SimpleNamespace(id='worker'))
        session.fence, session.runtime = 7, self.runtime
        session.validate_current = Mock(return_value=self.assignment)
        session._retire_failed_runtime, session.stop = AsyncMock(), AsyncMock()
        unbound = replace(self.capture, assignment=None, worker_id=None, fence=None)
        with patch('codex_web.runtime.codex.request_timeout', return_value=.001):
            with self.assertRaises(HTTPException):
                await session.request_with_start_capture('turn/start', {'threadId':'thread'}, unbound)
        session._retire_failed_runtime.assert_awaited_once()
        session.stop.assert_awaited_once()
        self.assertEqual(self.saved().queued_start_correlation.assignment_id, 'assignment')

    async def test_held_prepare_rechecks_canonical_assignment_and_admission_before_native_write(self):
        from tests.test_codex_worker_session import AssignmentBoundCodexSessionTests
        for change in ('fence', 'lease', 'profile', 'claim', 'retired'):
            with self.subTest(change=change):
                fixture = AssignmentBoundCodexSessionTests()
                await fixture.asyncSetUp()
                session = None
                release, entered = threading.Event(), threading.Event()
                task = None
                try:
                    assignment = fixture._create_assignment()
                    session = await fixture._session(assignment)
                    old_runtime = session.runtime
                    native = CodexRuntime(_Host(), restart_on_timeout=False)
                    native.ensure_started = AsyncMock()
                    wire = io.StringIO()
                    native.proc = SimpleNamespace(poll=lambda:None, stdin=wire)
                    session.runtime = native
                    original = QueuedTurn(id='race-'+change, execution_id='race-request',
                        thread_id='race-thread', project_id='home', message='race original', created_at=1)
                    service = ExecutionPreflightService(ExecutionPreflightStore(
                        SQLiteStateStore(Path(fixture.temp.name)/'preflight.sqlite')))
                    retained = service.retain_queued(original, organization_id=fixture.admin.organization_id,
                                                     workspace_id=fixture.admin.workspace_id)
                    _, claim = service.claim_queued_admission(retained.id)
                    service.begin_queued_start(retained.id, claim_id=claim)
                    capture = QueuedNativeStartCapture(service, retained.id, claim,
                        'race-request', 'race-thread', fixture.admin.identity_id)
                    prepare = QueuedNativeStartCapture.prepare
                    def held_prepare(captured, **kwargs):
                        result = prepare(captured, **kwargs)
                        entered.set()
                        if not release.wait(5):
                            raise RuntimeError('test preparation gate timed out')
                        return result
                    with patch.object(QueuedNativeStartCapture, 'prepare', held_prepare):
                        task = asyncio.create_task(session.request_with_start_capture(
                            'turn/start', {'threadId':'race-thread'}, capture))
                        self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                        self.assertIsNotNone(service.store.get(retained.id).queued_start_correlation)
                        if change in ('fence','lease','profile'):
                            def mutate(state):
                                current = state.assignments[0]
                                update = ({'fence':current.fence+1} if change=='fence'
                                          else {'lease':None} if change=='lease' else {
                                              'agent_profile':self.assignment.agent_profile})
                                state.assignments[0] = current.model_copy(update=update)
                                return state
                            fixture.worker_service.store.update_assignment(fixture.worker.id, assignment.id, mutate)
                        else:
                            service.store.update(retained.id, lambda current:current.model_copy(update={
                                'queued_admission_claim_id':'different-claim'} if change=='claim'
                                else {'status':'blocked'}))
                        release.set()
                        with self.assertRaises((ValueError, RuntimeError)):
                            await task
                    self.assertEqual(wire.getvalue(), '')
                    self.assertIsNone(service.store.get(retained.id).queued_start_receipt)
                finally:
                    release.set()
                    if task is not None and not task.done():
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    if session is not None:
                        session.runtime = old_runtime
                        await session.stop()
                    await fixture.asyncTearDown()

    async def test_new_payload_version_and_old_reader_restrictions(self):
        legacy = self.saved().model_dump(mode='json', exclude={'queued_start_correlation','queued_start_evidence'})
        self.assertEqual(self.saved().model_dump(mode='json'), legacy)
        self.assertIsNone(ExecutionPreflightAttempt.model_validate(legacy).queued_start_correlation)
        self.prepare()
        payload = self.saved().model_dump(mode='json')
        old_fields = {name:(field.annotation, field) for name,field in ExecutionPreflightAttempt.model_fields.items()
                      if name not in {'queued_start_correlation','queued_start_evidence'}}
        old_reader = create_model('OldPreflightReader', __config__={'extra':'forbid'}, **old_fields)
        old_reader.model_validate(legacy)
        with self.assertRaises(ValidationError):
            old_reader.model_validate(payload)
        payload['queued_start_correlation']['schema_version'] = '2.0'
        with self.assertRaises(ValidationError):
            ExecutionPreflightAttempt.model_validate(payload)
