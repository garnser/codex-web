"""Original serving context and canonical preinput refusals, without a provider."""
import asyncio
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from codex_web.models import QueuedTurn
from codex_web.model_providers import ModelProviderOutcomeUnknownError
from codex_web.services.codex_reasoning_policy import NativeReasoningPolicyError
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.native_model_provider import (
    NativeModelInvocationContext, NativeProcessOwnership, current_native_model_invocation,
    _current_native_model_invocation,
)
from codex_web.services.native_reasoning_bridge import (
    CanonicalNativeReasoningBridge, NativeReasoningPrestartRefused,
)
from codex_web.services.native_reasoning_runtime import ReasoningSessionManager
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class NativeQueuedPreparationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.preflight = ExecutionPreflightService(
            ExecutionPreflightStore(SQLiteStateStore(Path(directory.name) / 'state.sqlite')))
        self.queued = QueuedTurn(id='original', execution_id='executive:one:cpo',
            thread_id='thread', project_id='project', message='one original request',
            created_at=time.time(), source='web', sandbox='read-only', approval_policy='never',
            model='model', agent_profile_actor_id='actor', agent_profile_id='reasoning',
            agent_profile_revision=2, execution_profile_id='reasoning-only')
        self.attempt = self.preflight.retain_queued(self.queued,
            organization_id='org', workspace_id='ws')
        _, self.claim = self.preflight.claim_queued_admission(self.attempt.id)
        self.assertTrue(self.preflight.begin_queued_start(self.attempt.id, claim_id=self.claim))
        owner = NativeProcessOwnership(deadline_at=time.monotonic() + 30)
        owner.attach('assignment', lambda: None)
        owner.bind_native_session('thread')
        self.context = NativeModelInvocationContext(organization_id='org', workspace_id='ws',
            actor_id='actor', project_id='project', activation_id='one', role_id='cpo',
            execution_id=self.queued.execution_id, max_input_tokens=None, max_output_tokens=None,
            validate_current=lambda: None, process_owner=owner, concrete_model='model',
            input_message_sha256=hashlib.sha256(self.queued.message.encode()).hexdigest())
        self.serving = NS(assignment_id='assignment', fence=1,
            _native_invocation_fence=1, _native_invocation_context=self.context)
        self.manager = object.__new__(ReasoningSessionManager)
        self.manager.sessions = {'assignment':self.serving}
        self.manager.reasoning_binding = NS(agent_profile_id='reasoning', agent_profile_revision=2,
                                           execution_profile_id='reasoning-only')
        self.bridge = object.__new__(CanonicalNativeReasoningBridge)
        self.bridge.preflight = self.preflight
        self.bridge._admission_waiters = {}
        self.preflight.queued_disposition_observer = self.bridge.observe_admission

    def refused(self, **changes):
        values = dict(status='blocked', claim_id=self.claim, preinput_refusal=True,
            detail={'code':'native_reasoning_preparation_unavailable',
                    'message':'Closed local refusal', 'native_preparation_stage':'invocation_context',
                    'native_exception_type':'RuntimeError'})
        values.update(changes)
        return self.preflight.queued_disposition(self.attempt.id, **values)

    async def wait_original(self, terminal, refused, cancelled):
        return await self.bridge._wait_original_terminal(terminal, refused, cancelled,
            context=self.context, thread_id='thread', queued_id='original')

    async def test_serving_context_covers_threaded_guards_and_is_restored_after_exception(self):
        with self.assertRaises(NativeReasoningPolicyError):
            current_native_model_invocation()
        other = NS(original_ambient_context=True)
        token = _current_native_model_invocation.set(other)
        try:
            with self.manager.queued_input_context(self.queued):
                self.assertIs(current_native_model_invocation(), self.context)
            self.assertIs(_current_native_model_invocation.get(), other)
        finally:
            _current_native_model_invocation.reset(token)
        with self.assertRaisesRegex(ValueError, 'fixture'):
            with self.manager.queued_input_context(self.queued):
                self.assertIs(current_native_model_invocation(), self.context)
                self.assertIs(await asyncio.to_thread(current_native_model_invocation), self.context)
                raise ValueError('fixture')
        with self.assertRaises(NativeReasoningPolicyError):
            current_native_model_invocation()

    async def test_scope_binding_drift_and_cancelled_owner_cannot_supply_context(self):
        for key, value in {'execution_id':'other', 'project_id':'foreign',
                'agent_profile_actor_id':'other', 'agent_profile_id':'other',
                'agent_profile_revision':3, 'execution_profile_id':'other',
                'model':'other', 'sandbox':'workspace-write', 'approval_policy':'on-request',
                'message':'different'}.items():
            with self.subTest(key=key), self.assertRaises(NativeReasoningPolicyError):
                with self.manager.queued_input_context(self.queued.model_copy(update={key:value})):
                    self.fail('Changed queue request acquired private context')
        self.serving.fence = 2
        with self.assertRaises(NativeReasoningPolicyError):
            with self.manager.queued_input_context(self.queued):
                self.fail('Changed serving fence acquired private context')
        self.serving.fence = 1
        self.context.process_owner.cancelled.set()
        with self.assertRaises(NativeReasoningPolicyError):
            with self.manager.queued_input_context(self.queued):
                self.fail('Cancelled owner acquired private context')
        self.context.process_owner.cancelled.clear()
        self.context.process_owner.deadline_at = time.monotonic() - 1
        with self.assertRaises(NativeReasoningPolicyError):
            with self.manager.queued_input_context(self.queued):
                self.fail('Expired owner acquired private context')

    async def test_unowned_thread_cannot_borrow_another_live_invocation(self):
        with self.manager.queued_input_context(self.queued.model_copy(update={'thread_id':'other'})):
            with self.assertRaises(NativeReasoningPolicyError):
                current_native_model_invocation()

    async def test_known_original_refusal_returns_without_waiting_for_model_deadline(self):
        loop = asyncio.get_running_loop()
        terminal, refused = loop.create_future(), loop.create_future()
        self.bridge._admission_waiters[self.context.execution_id] = (loop, refused)
        waiting = asyncio.create_task(self.wait_original(terminal, refused, asyncio.Event()))
        original = self.refused()
        self.assertTrue(self.preflight.recorded_prestart_refusal(original))
        with self.assertRaises(NativeReasoningPrestartRefused):
            await asyncio.wait_for(waiting, 1)
        self.assertFalse(terminal.done())
        self.assertIsNone(original.queued_start_receipt)
        self.assertEqual(original.queued_admission_claim_id, self.claim)

    async def test_unwitnessed_blocked_label_does_not_become_known_noninput(self):
        original = self.refused(preinput_refusal=False)
        self.assertFalse(self.preflight.recorded_reasoning_refusal(original))
        self.assertFalse(self.preflight.recorded_prestart_refusal(original))

    async def test_claim_snapshot_and_partial_start_invalidate_refusal_witness(self):
        original = self.refused()
        for update in ({'queued_admission_claim_id':'different'},
                {'queued_turn_snapshot':{**original.queued_turn_snapshot, 'message':'changed'}},
                {'started_at':time.time()}, {'queued_start_correlation':object()},
                {'queued_start_receipt':object()}, {'queued_start_evidence':object()},
                {'queued_resume_correlation':object()}, {'queued_resume_evidence':object()},
                {'status':'reconciliation_required'}):
            with self.subTest(update=tuple(update)):
                self.assertFalse(self.preflight.recorded_reasoning_refusal(
                    original.model_copy(update=update)))

    async def test_notification_cannot_override_new_canonical_unknown_or_tenant_drift(self):
        original = self.refused()
        for update in ({'status':'reconciliation_required'}, {'organization_id':'foreign'},
                {'started_at':time.time()},
                {'queued_turn_snapshot':{**original.queued_turn_snapshot, 'message':'different'}}):
            with self.subTest(update=update):
                self.preflight.store.update(original.id, lambda row:original.model_copy(update=update))
                loop = asyncio.get_running_loop()
                terminal, refused = loop.create_future(), loop.create_future()
                refused.set_result(original.id)
                with self.assertRaises(ModelProviderOutcomeUnknownError):
                    await self.wait_original(terminal, refused, asyncio.Event())

    async def test_possible_start_without_terminal_retains_unknown_until_original_cancellation(self):
        loop = asyncio.get_running_loop()
        terminal, refused = loop.create_future(), loop.create_future()
        cancelled = asyncio.Event()
        waiting = asyncio.create_task(self.wait_original(terminal, refused, cancelled))
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        cancelled.set()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertFalse(terminal.done())
        self.assertFalse(refused.done())
