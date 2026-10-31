"""Durable web ingress and existing idle-renewal composition; no native I/O."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from codex_web.identity import AuthenticationActor, AuthenticationAssurance, MembershipRole
from codex_web.models import Project, TurnCreate, QueuedNativeStartReceipt, QueuedTurn
from codex_web.runtime.execution import ExecutionPrestartRefusal, TurnExecutionService
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.execution_workers import ProcedureHistoryExhaustedError
from codex_web.services.turn_queue_policy import TurnQueuePolicy
from codex_web.services.turns import TurnService
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests.test_turn_execution import _Host
from tests.turn_service_fixture_helpers import _Settings, _Recovery, _Resume, _Bindings


class ImmediateGuardedTurnAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Project(id='project', name='Project', path='/workspace')
        self.actor = AuthenticationActor(identity_id='admin', principal_kind='human',
            organization_id='local', workspace_id='default', roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA)
        self.host = _Host()
        self.host._project = lambda _id: self.project
        self.runtime = TurnExecutionService(self.host)
        self.runtime.queued_preflight = ExecutionPreflightService(ExecutionPreflightStore(
            SQLiteStateStore(Path(temporary.name) / 'state.sqlite')))
        self.runtime.procedure_admission = AsyncMock()
        self.runtime.actor_resolver = Mock(return_value=self.actor)
        self.runtime.start_thread_turn_now = AsyncMock(return_value={'turn': {'id': 'new-turn'}})
        self.runtime.schedule_queue_drain = Mock()
        self.queue = TurnQueuePolicy(lambda: self.host.queues, get_queue=self.host._thread_queue)
        self.service = TurnService(projects=SimpleNamespace(get=lambda _id: self.project),
            settings=_Settings(), recovery=_Recovery(), resume_runtime=_Resume(),
            bindings=_Bindings(), queue_policy=self.queue, execution=self.runtime,
            event_sink=self.host._append_bot_event, truncate_text=lambda value, limit: str(value)[:limit],
            binding_public=lambda binding: binding.model_dump())

    async def accept(self, *, allow_assignment_renewal=True, **kwargs):
        return await self.service.start('thread', TurnCreate(message='distinct original',
            project_id='project', **{'defer_start': False, **kwargs}), actor=self.actor,
            execution_id='same-execution', work_item_ref='project#340',
            allow_assignment_renewal=allow_assignment_renewal)

    def exhausted(self, *, typed=True):
        refusal = ExecutionPrestartRefusal(status_code=409, detail={
            'code': 'execution_preflight_blocked', 'message': 'History exhausted', 'retryable': True,
            'blockers': [{'code': 'procedure_assignment_exhausted', 'message': 'History exhausted',
                'retryable': True, 'target_type': 'execution_assignment', 'target_id': 'old-assignment'}]})
        if typed:
            refusal.__cause__ = ProcedureHistoryExhaustedError('History exhausted')
        return refusal

    async def test_immediate_profiled_original_is_queued_once_before_native_work(self):
        profile = SimpleNamespace(revision=6, sandbox_requirement='danger-full-access',
                                  execution_profile_id='repository-write')
        self.service.agent_profiles = SimpleNamespace(resolve_for_execution=Mock(return_value=(profile, None)))
        result = await self.accept(agent_profile_id='james', agent_profile_revision=6,
            sandbox='danger-full-access', repository_resource_id='repo',
            writable_repository_resource_ids=('repo',), read_only_repository_resource_ids=())
        self.assertTrue(result.get('queued'))
        queued, = self.queue.queue('thread')
        self.assertEqual((queued.id, queued.execution_id, queued.message, queued.work_item_ref),
                         (result['queuedId'], 'same-execution', 'distinct original', 'project#340'))
        self.assertEqual((queued.agent_profile_id, queued.agent_profile_revision, queued.agent_profile_actor_id),
                         ('james', 6, 'admin'))
        self.assertEqual(queued.writable_repository_resource_ids, ('repo',))
        self.assertEqual(queued.read_only_repository_resource_ids, ())
        self.runtime.start_thread_turn_now.assert_not_awaited()
        self.runtime.procedure_admission.assert_not_awaited()
        self.runtime.schedule_queue_drain.assert_called_once_with('thread')

    async def test_unprofiled_requester_is_resolved_fresh_and_same_original_gets_claim(self):
        result = await self.accept()
        queued, = self.queue.queue('thread')
        self.assertIsNone(queued.agent_profile_id)
        self.assertEqual(queued.agent_profile_actor_id, self.actor.identity_id)
        await self.runtime.drain_thread_queue('thread')
        self.runtime.actor_resolver.assert_called_once_with('admin', self.project)
        kwargs = self.runtime.start_thread_turn_now.await_args.kwargs
        self.assertIs(kwargs['actor'], self.actor)
        self.assertEqual(kwargs['execution_id'], 'same-execution')
        self.assertEqual(kwargs['message'], queued.message)
        self.assertEqual(kwargs['queued_admission_attempt_id'], 'preflight-queued-' + result['queuedId'])
        self.assertTrue(kwargs['queued_admission_claim_id'])
        saved = self.runtime.queued_preflight.store.get(kwargs['queued_admission_attempt_id'])
        self.assertEqual(saved.status, 'started')
        self.assertEqual(saved.queued_turn_snapshot['id'], queued.id)

    async def test_guard_factory_also_requires_queue_but_unwired_runtime_stays_immediate(self):
        self.runtime.procedure_admission = None
        self.runtime.native_input_guard_factory = Mock()
        self.assertTrue((await self.accept())['queued'])
        self.runtime.native_input_guard_factory.assert_not_called()
        self.host.queues.clear()
        self.runtime.native_input_guard_factory = None
        self.assertEqual((await self.accept())['turn']['id'], 'new-turn')
        self.runtime.start_thread_turn_now.assert_awaited_once()

    async def test_first_local_exhaustion_reserves_once_and_renews_same_original(self):
        result = await self.accept()
        before = self.queue.queue('thread')[0].model_dump(mode='json')
        self.runtime.start_thread_turn_now.side_effect = self.exhausted()
        async def renew(*args, **kwargs):
            saved = self.runtime.queued_preflight.store.get('preflight-queued-' + result['queuedId'])
            self.assertEqual(saved.status, 'retrying')
            self.assertEqual(saved.attempt_number, 2)
            self.assertTrue(saved.retry_claim_id)
            self.assertEqual(saved.queued_turn_snapshot, before)
        self.runtime.renew_exhausted_assignment = AsyncMock(side_effect=renew)
        await self.runtime.drain_thread_queue('thread')
        self.runtime.renew_exhausted_assignment.assert_awaited_once_with('thread', project=self.project,
            actor=self.actor, expected_assignment_id='old-assignment')
        self.assertEqual(self.queue.queue('thread')[0].model_dump(mode='json'), before)
        # Another identical exhaustion stays blocked instead of looping renewal.
        await self.runtime.drain_thread_queue('thread')
        self.runtime.renew_exhausted_assignment.assert_awaited_once()
        saved = self.runtime.queued_preflight.store.get('preflight-queued-' + result['queuedId'])
        self.assertEqual(saved.status, 'blocked')
        self.assertEqual(saved.attempt_number, 2)
        self.assertEqual(saved.queued_turn_snapshot, before)

    async def test_http_code_without_local_typed_cause_cannot_renew(self):
        result = await self.accept()
        self.runtime.start_thread_turn_now.side_effect = self.exhausted(typed=False)
        self.runtime.renew_exhausted_assignment = AsyncMock()
        await self.runtime.drain_thread_queue('thread')
        self.runtime.renew_exhausted_assignment.assert_not_awaited()
        self.assertEqual(self.runtime.queued_preflight.store.get('preflight-queued-' + result['queuedId']).status,
                         'blocked')

    async def test_cancelled_renewal_preserves_uncertain_original_without_requeue(self):
        result = await self.accept()
        self.runtime.start_thread_turn_now.side_effect = self.exhausted()
        self.runtime.renew_exhausted_assignment = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.runtime.drain_thread_queue('thread')
        saved = self.runtime.queued_preflight.store.get('preflight-queued-' + result['queuedId'])
        self.assertEqual(saved.status, 'reconciliation_required')
        self.assertEqual(saved.attempt_number, 2)
        self.assertEqual(saved.queued_turn_snapshot['execution_id'], 'same-execution')
        self.assertEqual(self.queue.queue('thread'), [])
        self.runtime.start_thread_turn_now.assert_awaited_once()

    async def test_automation_cannot_take_human_automatic_renewal(self):
        queued = self.runtime.enqueue_turn(thread_id='thread', project_id='project', message='automation',
            source='project-coordination', agent_profile_actor_id='admin')
        self.runtime.start_thread_turn_now.side_effect = self.exhausted()
        self.runtime.renew_exhausted_assignment = AsyncMock()
        await self.runtime.drain_thread_queue('thread')
        self.runtime.renew_exhausted_assignment.assert_not_awaited()
        self.assertEqual(self.runtime.queued_preflight.store.get('preflight-queued-' + queued.id).status, 'blocked')

    async def test_human_original_keeps_priority_without_dropping_automation(self):
        other = self.runtime.enqueue_turn(thread_id='thread', project_id='project',
            source='scheduled', message='older automation')
        await self.accept()
        await self.runtime.drain_thread_queue('thread')
        self.assertEqual(self.runtime.start_thread_turn_now.await_args.kwargs['message'], 'distinct original')
        self.assertEqual([item.id for item in self.queue.queue('thread')], [other.id])

    async def test_committed_positive_start_wins_over_local_exhaustion_exception(self):
        result = await self.accept()
        attempt_id = 'preflight-queued-' + result['queuedId']
        async def started(_thread, **kwargs):
            receipt = QueuedNativeStartReceipt(admission_claim_id=kwargs['queued_admission_claim_id'],
                requested_execution_id=kwargs['execution_id'], execution_id=kwargs['execution_id'],
                thread_id='thread', provider_native_session_id='thread', provider_native_turn_id='actual',
                provider_id='openai', runtime_id='codex', observed_at=100)
            self.runtime.queued_preflight.store.update(attempt_id,
                lambda current: current.model_copy(update={'status': 'started',
                    'started_at': 100, 'queued_start_receipt': receipt}))
            raise self.exhausted()
        self.runtime.start_thread_turn_now.side_effect = started
        self.runtime.renew_exhausted_assignment = AsyncMock()
        await self.runtime.drain_thread_queue('thread')
        self.runtime.renew_exhausted_assignment.assert_not_awaited()
        saved = self.runtime.queued_preflight.store.get(attempt_id)
        self.assertEqual(saved.status, 'started')
        self.assertEqual(saved.queued_start_receipt.provider_native_turn_id, 'actual')
        self.assertEqual(saved.attempt_number, 1)
        self.assertEqual(self.queue.queue('thread'), [])

    async def test_explicit_deferred_and_restricted_callers_never_auto_renew(self):
        for index, options in enumerate(({'defer_start': True}, {'allow_assignment_renewal': False})):
            with self.subTest(options=options):
                self.host.queues.clear()
                # Separate original per case; previously blocked tasks stay retained.
                result = await self.service.start('restricted-' + str(index),
                    TurnCreate(message='restricted original ' + str(options), project_id='project',
                        defer_start=options.get('defer_start', False)), actor=self.actor,
                    allow_assignment_renewal=options.get('allow_assignment_renewal', True))
                thread = result['threadId']
                queued, = self.queue.queue(thread)
                self.assertFalse(queued.automatic_assignment_renewal)
                self.assertNotIn('automatic_assignment_renewal', queued.model_dump(mode='json'))
                self.runtime.start_thread_turn_now.side_effect = self.exhausted()
                self.runtime.renew_exhausted_assignment = AsyncMock()
                await self.runtime.drain_thread_queue(thread)
                self.runtime.renew_exhausted_assignment.assert_not_awaited()
                saved = self.runtime.queued_preflight.store.get('preflight-queued-' + result['queuedId'])
                self.assertEqual(saved.status, 'blocked')
                self.assertEqual(saved.attempt_number, 1)

    async def test_policy_flag_cannot_bypass_existing_unknown_original_identity(self):
        await self.accept()
        queued, = self.queue.queue('thread')
        legacy = queued.model_copy(update={'automatic_assignment_renewal': False})
        legacy_bytes = legacy.model_dump(mode='json')
        self.assertNotIn('automatic_assignment_renewal', legacy_bytes)
        self.assertEqual(QueuedTurn.model_validate(legacy_bytes).model_dump(mode='json'), legacy_bytes)
        preflight = self.runtime.queued_preflight
        saved = preflight.retain_queued(legacy, organization_id='local', workspace_id='default')
        _, claim = preflight.claim_queued_admission(saved.id)
        self.assertTrue(preflight.begin_queued_start(saved.id, claim_id=claim))
        self.host.queues.clear()
        with self.assertRaises(ExecutionPrestartRefusal) as caught:
            await self.accept()
        self.assertEqual(caught.exception.detail['attemptId'], saved.id)
        self.assertEqual(preflight.store.get(saved.id).queued_turn_snapshot, legacy_bytes)
        self.runtime.start_thread_turn_now.assert_not_awaited()

    async def test_existing_deferred_copy_is_not_upgraded_to_renewal_permission(self):
        queued = self.runtime.enqueue_turn(thread_id='thread', project_id='project',
            message='distinct original', execution_id='existing-execution', source='web',
            work_item_ref='project#340', sandbox=self.project.sandbox,
            approval_policy=self.project.approval_policy, agent_profile_actor_id='admin')
        result = await self.accept()
        self.assertEqual(result['queuedId'], queued.id)
        self.assertEqual(len(self.queue.queue('thread')), 1)
        self.assertFalse(self.queue.queue('thread')[0].automatic_assignment_renewal)
