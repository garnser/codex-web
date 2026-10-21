"""Real READY receipts, canonical authority and append-only post-turn recovery."""
import asyncio
import threading
import unittest
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from codex_web.action_intents import ActionIntentStatus
from codex_web.autonomy import AutonomyScopedPauseCreate
from codex_web.reconciliation_gates import ReconcilerDeclaration, ReconcilerStartupClass
from codex_web.services.action_providers import ActionRequirementError
from codex_web.services.autonomy_controller import AutonomyController
from codex_web.services.post_turn_readiness import PostTurnReadinessService, require_post_turn_admission
from codex_web.services.reconciliation_gates import ReconciliationGateService
from codex_web.storage.autonomy import AutonomyStateStore
from codex_web.runtime.execution import TurnExecutionService
from codex_web.models import ActiveThreadTurn
from codex_web.resources import ResourceUpdate
from tests import test_readonly_reconciliation_runtime as fixture


class PostTurnReadinessTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ReadOnlyReconciliationRuntimeTests.asyncSetUp
    asyncTearDown = fixture.ReadOnlyReconciliationRuntimeTests.asyncTearDown
    _assignment = staticmethod(fixture.ReadOnlyReconciliationRuntimeTests._assignment)
    _publish_worker_authority = fixture.ReadOnlyReconciliationRuntimeTests._publish_worker_authority
    _publish_profile = fixture.ReadOnlyReconciliationRuntimeTests._publish_profile
    _request = fixture.ReadOnlyReconciliationRuntimeTests._request
    change_assignment = fixture.ReadOnlyReconciliationRuntimeTests.change_assignment
    submit = fixture.ReadOnlyReconciliationRuntimeTests.submit
    respond = fixture.ReadOnlyReconciliationRuntimeTests.respond
    prepare_target = fixture.ReadOnlyReconciliationRuntimeTests.prepare_target
    build = fixture.ReadOnlyReconciliationRuntimeTests.build

    async def prepare(self):
        self.intent_id = await self.prepare_target()
        self.runtime = self.build(self.intent_id)
        self.controller = AutonomyController(AutonomyStateStore(self.sqlite))
        self.gates = ReconciliationGateService(self.sqlite, identity=self.identity,
            readiness=lambda *_args: {})
        self.gates.register(ReconcilerDeclaration(service_id='queue-recovery',
            startup_class=ReconcilerStartupClass.PRE_READINESS_BOUNDED))
        self.events = []
        self.admission = partial(require_post_turn_admission, controller=self.controller,
            identity=self.identity, runtime_enabled=lambda: True, gates=self.gates)
        self.callback = self.new_callback()
        self.active = SimpleNamespace(assignment_id=self.assignment.id,
            execution_id=self.assignment.execution_id, worker_id=self.worker.id,
            fence=self.assignment.fence, thread_id='canonical-thread', turn_id='terminal-turn-1')
        self.originals = self.workers.store.assignment(self.assignment.id).procedure_discoveries

    def new_callback(self):
        return PostTurnReadinessService(self.runtime.intents,
            admission=self.admission, append_event=self.events.append)

    async def settle(self):
        return await self.callback.settle(self.active, actor=self.runtime.worker_actor)

    async def test_completed_turn_settles_same_real_receipt_without_model_or_provider_write(self):
        await self.prepare()
        before = self.runtime.intents.store.get(self.intent_id)
        calls = len(self.calls)
        self.assertTrue(await self.settle())
        result = self.runtime.intents.store.get(self.intent_id)
        self.assertEqual(result.status, ActionIntentStatus.SUCCEEDED)
        self.assertEqual(result.last_receipt_id, before.last_receipt_id)
        self.assertEqual(result.attempt, before.attempt)
        self.assertTrue(all(method == 'GET' for method, _ in self.calls[calls:]))
        self.assertEqual(sum(method == 'PUT' for method, _ in self.calls), 1)
        current = self.workers.store.assignment(self.assignment.id)
        self.assertEqual(current.procedure_discoveries[:64], self.originals)
        history = self.runtime.intents.history(self.intent_id, self.admin)
        self.assertTrue(history['verifications'][-1]['verified'])

    async def test_cold_large_origin_index_finishes_bounded_preparation_before_callback(self):
        await self.prepare()
        current = self.runtime.intents.store.get(self.intent_id)
        terminal = current.model_copy(update={'status': ActionIntentStatus.SUCCEEDED,
            'readiness_origin': None}).model_dump(mode='json')
        self.sqlite.record_apply(self.runtime.intents.store.records_namespace, upserts={
            'intents:terminal-%04d' % index: {**terminal, 'id': 'terminal-%04d' % index}
            for index in range(1100)})
        with patch('codex_web.storage.action_intents._ORIGIN_INDEX_EPOCH', 'b' * 32):
            self.assertTrue(await self.settle())
        self.assertEqual(sum(method == 'PUT' for method, _ in self.calls), 1)

    async def test_origin_index_preparation_stops_without_cursor_progress(self):
        from codex_web.storage.action_intents import ReadinessOriginIndexProgressing
        await self.prepare()
        calls = len(self.calls)
        with patch.object(self.runtime.intents.store, 'unsettled_readiness_origin',
                side_effect=ReadinessOriginIndexProgressing('same-cursor', 'epoch')) as prepare:
            with self.assertRaises(ReadinessOriginIndexProgressing):
                await self.settle()
            self.assertEqual(prepare.call_count, 2)
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.workers.store.assignment(self.assignment.id).procedure_discoveries,
            self.originals)

    async def test_normal_budget_callback_crosses_63_without_rewriting_prefix(self):
        await self.prepare()
        prefix = self.originals[:63]
        self.change_assignment(procedure_discoveries=prefix)
        self.assertTrue(await self.settle())
        current = self.workers.store.assignment(self.assignment.id)
        self.assertEqual(current.procedure_discoveries[:63], prefix)
        self.assertGreater(len(current.procedure_discoveries), 63)
        from codex_web.procedure_reconciliation import RECOVERY_PREFIX
        self.assertTrue(any(value.startswith(RECOVERY_PREFIX)
            for value in current.procedure_discoveries[63].supporting_sources))
        self.assertEqual(sum(method == 'PUT' for method, _ in self.calls), 1)

    async def test_early_marked_attempts_share_later_exhaustion_limit(self):
        await self.prepare()
        prefix = self.originals[:2]
        self.change_assignment(procedure_discoveries=prefix)
        self.pr['sha'] = 'c' * 40
        for index in range(4):
            self.active.turn_id = 'early-terminal-' + str(index)
            self.assertFalse(await self.settle())
        current = self.workers.store.assignment(self.assignment.id)
        self.assertLess(len(current.procedure_discoveries), 64)
        self.assertEqual(current.procedure_discoveries[:2], prefix)
        calls = len(self.calls)
        self.active.turn_id = 'overflow-terminal'
        self.assertFalse(await self.settle())
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.workers.store.assignment(self.assignment.id).procedure_discoveries,
            current.procedure_discoveries)

    async def test_duplicate_concurrent_and_reopened_callback_preserves_failed_verification(self):
        await self.prepare()
        self.pr['sha'] = 'c' * 40
        results = await asyncio.gather(self.settle(), self.settle())
        self.assertEqual(results, [False, False])
        before = self.runtime.intents.history(self.intent_id, self.admin)
        self.assertFalse(before['verifications'][-1]['verified'])
        calls = len(self.calls)
        self.callback = self.new_callback()  # same canonical ledger, new engine instance
        self.assertFalse(await self.settle())
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.runtime.intents.history(self.intent_id, self.admin), before)
        self.assertEqual(self.workers.store.assignment(self.assignment.id).procedure_discoveries[:64], self.originals)

    async def test_global_pause_refuses_before_provider_and_reserve(self):
        await self.prepare()
        self.controller.pause(actor_id=self.admin.identity_id)
        calls = len(self.calls)
        with self.assertRaisesRegex(ActionRequirementError, 'paused'):
            await self.settle()
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.workers.store.assignment(self.assignment.id).procedure_discoveries, self.originals)

    async def test_project_scoped_pause_and_queue_gate_refuse(self):
        await self.prepare()
        pause = self.controller.add_scoped_pause(AutonomyScopedPauseCreate(scope='project',
            scope_id=self.assignment.project_id, reason='Human pause'), actor_id=self.admin.identity_id)
        calls = len(self.calls)
        with self.assertRaisesRegex(ActionRequirementError, 'paused'):
            await self.settle()
        self.controller.remove_scoped_pause(pause.id, actor_id=self.admin.identity_id)
        self.gates.pause('queue-recovery', self.assignment.project_id,
            actor=self.admin, reason='Canonical queue recovery pause')
        with self.assertRaisesRegex(ActionRequirementError, 'paused'):
            await self.settle()
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.workers.store.assignment(self.assignment.id).procedure_discoveries, self.originals)

    async def test_changed_fence_refuses_before_provider(self):
        await self.prepare()
        self.change_assignment(fence=self.assignment.fence + 1)
        calls = len(self.calls)
        with self.assertRaisesRegex(ActionRequirementError, 'custody changed'):
            await self.settle()
        self.assertEqual(len(self.calls), calls)

    async def test_terminal_runtime_callback_advances_actual_canonical_verification(self):
        await self.prepare()
        runtime, active = self.terminal_runtime()
        runtime._schedule_assignment_completion(active, succeeded=True,
            message={'method': 'turn/completed', 'params': {'turn': {'id': active.turn_id}}})
        self.assertIn(active.thread_id, runtime.thread_completion_tasks)
        await asyncio.gather(*runtime.assignment_completion_tasks.values())
        self.assertEqual(runtime.thread_completion_tasks, {})
        self.assertEqual(self.runtime.intents.store.get(self.intent_id).status, ActionIntentStatus.SUCCEEDED)
        self.assertTrue(self.runtime.intents.history(self.intent_id, self.admin)['verifications'][-1]['verified'])
        self.assertEqual(sum(method == 'PUT' for method, _ in self.calls), 1)

    def terminal_runtime(self):
        host = SimpleNamespace(_append_bot_event=self.events.append)
        runtime = TurnExecutionService(host)
        manager = SimpleNamespace(local_worker=SimpleNamespace(worker_actor=self.runtime.worker_actor),
            checkpoint=AsyncMock(return_value=()))
        session = SimpleNamespace(validate_current=lambda:
            self.workers.store.assignment(self.assignment.id))
        runtime._session_for_assignment = lambda _id: (manager, session)
        runtime._bootstrap_binding_for_thread = lambda _thread: SimpleNamespace(
            assignment_id=self.assignment.id, execution_id=self.assignment.execution_id,
            execution_workspace_id=self.assignment.execution_workspace_id)
        runtime.post_turn_readiness = self.callback.settle
        active = ActiveThreadTurn(**vars(self.active), started_at=1, updated_at=1)
        # No native turn is active during this canonical completion boundary.
        runtime._active_turn = lambda _thread: None
        return runtime, active

    async def delayed_writer(self, *, cancel):
        await self.prepare()
        if not cancel:
            def shorten(state):
                state.intents = [row.model_copy(update={'timeout_seconds': 0.03})
                    if row.id == self.intent_id else row for row in state.intents]
                return state
            self.runtime.intents.store.update(shorten, intent_ids=(self.intent_id,))
        entered, release = threading.Event(), threading.Event()
        def write_after_delay():
            entered.set()
            if not release.wait(5):
                raise AssertionError('Test did not release the owned synchronous operation')
            self.sqlite.put('post-turn-delayed-write', {'unexpected': True})
        async def delayed(*_args, **_kwargs):
            await asyncio.to_thread(write_after_delay)
            return self.runtime.intents.store.get(self.intent_id)
        self.runtime.intents.reconcile = delayed
        runtime, active = self.terminal_runtime()
        runtime._schedule_assignment_completion(active, succeeded=True,
            message={'method': 'turn/completed', 'params': {'turn': {'id': active.turn_id}}})
        completion = runtime.thread_completion_tasks[active.thread_id]
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            if cancel:
                completion.cancel()
            await asyncio.sleep(0.07)
            self.assertFalse(completion.done())
            self.assertTrue(runtime.thread_is_active(active.thread_id))
            self.assertIsNone(self.sqlite.get('post-turn-delayed-write'))
        finally:
            release.set()
            if cancel:
                with self.assertRaises(asyncio.CancelledError):
                    await completion
            else:
                await completion
        self.assertFalse(runtime.thread_is_active(active.thread_id))
        self.assertIsNone(self.sqlite.get('post-turn-delayed-write'))
        self.assertNotEqual(self.runtime.intents.store.get(self.intent_id).status, ActionIntentStatus.SUCCEEDED)

    async def test_timeout_holds_native_admission_until_actual_storage_owner_settles(self):
        await self.delayed_writer(cancel=False)

    async def test_cancellation_holds_native_admission_until_actual_storage_owner_settles(self):
        await self.delayed_writer(cancel=True)

    async def test_revoked_provider_credential_cannot_be_recovered_by_callback(self):
        await self.prepare()
        self.secret_broker.revoke(self.secret.id, actor=self.admin)
        calls = len(self.calls)
        self.assertFalse(await self.settle())
        self.assertEqual(len(self.calls), calls)
        self.assertNotEqual(self.runtime.intents.store.get(self.intent_id).status, ActionIntentStatus.SUCCEEDED)
        self.assertEqual(self.workers.store.assignment(self.assignment.id).procedure_discoveries[:64], self.originals)

    async def test_changed_worker_identity_refuses_before_provider(self):
        await self.prepare()
        other = self.identity.bootstrap_service_actor(identity_id='other-worker',
            name='Other private worker', scope=self.admin.tenant,
            service_scopes=('action-intent:worker', 'secret:use'))
        calls = len(self.calls)
        with self.assertRaisesRegex(ActionRequirementError, 'originating custody'):
            await self.callback.settle(self.active, actor=other)
        self.assertEqual(len(self.calls), calls)

    async def test_expired_lease_refuses_before_provider(self):
        await self.prepare()
        self.change_assignment(lease=self.assignment.lease.model_copy(update={'expires_at': 0}))
        calls = len(self.calls)
        with self.assertRaises(ActionRequirementError):
            await self.settle()
        self.assertEqual(len(self.calls), calls)

    async def test_changed_definition_refuses_before_provider(self):
        await self.prepare()
        self._publish_profile(self.profile_payload, '1.1', project='project-a')
        calls = len(self.calls)
        with self.assertRaises(ActionRequirementError):
            await self.settle()
        self.assertEqual(len(self.calls), calls)

    async def test_disabled_target_resource_cannot_settle(self):
        await self.prepare()
        self.resources.update(self.target.id, ResourceUpdate(lifecycle='disabled'), actor=self.admin)
        calls = len(self.calls)
        self.assertFalse(await self.settle())
        self.assertEqual(len(self.calls), calls)
        self.assertNotEqual(self.runtime.intents.store.get(self.intent_id).status, ActionIntentStatus.SUCCEEDED)

    async def test_distinct_terminal_events_do_not_bypass_four_attempt_reserve(self):
        await self.prepare()
        self.pr['sha'] = 'c' * 40
        for index in range(4):
            self.active.turn_id = 'terminal-turn-' + str(index)
            self.assertFalse(await self.settle())
        current = self.workers.store.assignment(self.assignment.id)
        calls = len(self.calls)
        self.active.turn_id = 'terminal-turn-overflow'
        self.assertFalse(await self.settle())
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.workers.store.assignment(self.assignment.id).procedure_discoveries,
            current.procedure_discoveries)
        self.assertEqual(current.procedure_discoveries[:64], self.originals)
