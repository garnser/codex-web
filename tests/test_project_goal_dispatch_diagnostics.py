"""Canonical retained delivery diagnostics, without importing the application."""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from codex_web.api.goal_execution_bindings import build_goal_execution_bindings_router
from codex_web.compatibility import ContractCompatibilityError, ContractSpec
from codex_web.goal_execution_bindings import GoalExecutionBindingEvent, GoalExecutionBindingState, ProjectGoalDispatchFailure
from codex_web.goals import GoalBudget
from codex_web.observability import correlated
from codex_web.runtime.execution import ExecutionPrestartRefusal
from codex_web.services.project_goal_continuation import _dispatch_failure
from codex_web.storage.goal_execution_bindings import GoalExecutionBindingStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests.test_project_goal_worker_admission import ProjectGoalWorkerAdmissionTests


class ProjectGoalDispatchDiagnosticTests(ProjectGoalWorkerAdmissionTests):
    goal_budget = GoalBudget()

    def current(self):
        return self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant)

    def failures(self):
        return [event for event in self.goal_bindings.events(self.goal.id, scope=self.actor.tenant)
                if event.dispatch_failure is not None]

    def fail_project_after_reservation(self, error):
        original = self.turns.projects.get
        def get(*args, **kwargs):
            progress = self.current().project_continuation
            if progress is not None and progress.delivery is not None and progress.delivery.phase == 'reserved':
                raise error
            return original(*args, **kwargs)
        return patch.object(self.turns.projects, 'get', side_effect=get)

    async def test_before_queue_failure_is_durable_scoped_and_private(self):
        with self.fail_project_after_reservation(ValueError('credential-value-must-not-persist')):
            with correlated(correlation_id='actual-dispatch-observation', causation_id='upstream-observation'):
                result = await self.goal_continuation.dispatch_once(self.goal_binding.id, scope=self.actor.tenant)
        self.assertEqual(result.outcome, 'unknown')
        current = self.current()
        self.assertEqual(current.status, 'unknown')
        self.assertEqual(current.project_continuation.reserved_calls, 1)
        self.assertEqual(current.project_continuation.model_calls, 0)
        self.assertEqual(current.project_continuation.delivery.phase, 'unknown')
        self.assertEqual(self.queue_store.get(self.thread), [])
        event, = self.failures()
        self.assertEqual(event.dispatch_failure.stage, 'project_lookup')
        self.assertEqual(event.dispatch_failure.exception_type, 'value_error')
        self.assertIsNone(event.dispatch_failure.code)
        self.assertEqual(event.correlation_id, 'actual-dispatch-observation')
        self.assertEqual(event.causation_id, current.project_continuation.delivery.id)
        self.assertEqual(event.reason, 'Project Goal original held; no implicit replay')

        # The existing scoped public event path exposes the same typed record.
        router = build_goal_execution_bindings_router(self.goal_bindings, self.sessions, self.goals)
        endpoint = next(route.endpoint for route in router.routes
                        if route.path.endswith('/{goal_id}/execution-binding-events'))
        request = Request({'type': 'http', 'state': {'identity_actor': self.actor}})
        payload = await endpoint(self.goal.id, request, project_id='data')
        row = next(item for item in payload['items'] if item['id'] == event.id)
        self.assertEqual(row['dispatch_failure'], event.dispatch_failure.model_dump(mode='json'))
        self.assertNotIn('credential-value', json.dumps(self.goal_bindings.store.load().model_dump(mode='json')))
        await self.goal_continuation.dispatch_once(current.id, scope=self.actor.tenant)
        self.assertEqual(self.current(), current)
        self.assertEqual(len(self.failures()), 1)
        self.assertEqual(self.starts(), [])

    async def test_after_durable_enqueue_response_loss_keeps_same_unknown_original(self):
        sink = self.turns.event_sink
        def lose_response(event):
            sink(event)
            raise TimeoutError('private-response-body')
        with patch.object(self.turns, 'event_sink', side_effect=lose_response):
            result = await self.goal_continuation.dispatch_once(self.goal_binding.id, scope=self.actor.tenant)
        self.assertEqual(result.outcome, 'unknown')
        current = self.current()
        delivery = current.project_continuation.delivery
        queued, = self.queue_store.get(self.thread)
        self.assertEqual(queued.execution_id, delivery.id)
        self.assertEqual(delivery.phase, 'unknown')
        self.assertIsNone(delivery.queued_id)
        self.assertIsNone(delivery.admission_attempt_id)
        event, = self.failures()
        self.assertEqual(event.dispatch_failure.stage, 'queue_call')
        self.assertEqual(event.dispatch_failure.exception_type, 'timeout')
        # No ambient observation: never borrow the previous claim correlation.
        self.assertIsNone(event.correlation_id)
        self.assertEqual(event.causation_id, delivery.id)
        before_queue = queued.model_dump(mode='json')
        before_preflights = [row.model_dump(mode='json') for row in self.project_goal.preflight.store.for_thread(self.thread)]
        await self.goal_continuation.dispatch_once(current.id, scope=self.actor.tenant)
        self.assertEqual(self.current(), current)
        self.assertEqual([row.model_dump(mode='json') for row in self.queue_store.get(self.thread)], [before_queue])
        self.assertEqual([row.model_dump(mode='json') for row in self.project_goal.preflight.store.for_thread(self.thread)], before_preflights)
        self.assertEqual(current.project_continuation.reserved_calls, 1)
        self.assertEqual(len(self.failures()), 1)
        self.assertEqual(self.starts(), [])

    async def test_cancellation_joins_queue_owner_and_reraises_with_same_retained_input(self):
        entered, joined = asyncio.Event(), asyncio.Event()
        async def hold_after_write(_thread):
            self.assertEqual(len(self.queue_store.get(self.thread)), 1)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                joined.set()
        with patch.object(self.execution, 'publish_queue_status', side_effect=hold_after_write):
            task = asyncio.create_task(self.goal_continuation.dispatch_once(self.goal_binding.id, scope=self.actor.tenant))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(joined.is_set())
        current = self.current()
        queued, = self.queue_store.get(self.thread)
        self.assertEqual(current.project_continuation.delivery.phase, 'unknown')
        self.assertEqual(queued.execution_id, current.project_continuation.delivery.id)
        event, = self.failures()
        self.assertEqual(event.dispatch_failure.exception_type, 'cancelled')
        self.assertEqual(event.dispatch_failure.stage, 'queue_call')
        self.assertEqual(event.causation_id, queued.execution_id)
        self.assertEqual(self.starts(), [])

    async def test_typed_known_refusal_code_remains_diagnostic_not_retry_authority(self):
        error = ExecutionPrestartRefusal(status_code=409, detail={
            'code': 'agent_profile_unavailable', 'message': 'private-provider-details'})
        with self.fail_project_after_reservation(error):
            result = await self.goal_continuation.dispatch_once(self.goal_binding.id, scope=self.actor.tenant)
        self.assertEqual(result.outcome, 'unknown')
        event, = self.failures()
        self.assertEqual(event.dispatch_failure.code, 'agent_profile_unavailable')
        self.assertEqual(event.dispatch_failure.exception_type, 'prestart_refusal')
        self.assertNotIn('private-provider-details', json.dumps(event.model_dump(mode='json')))
        self.assertEqual(self.current().project_continuation.delivery.phase, 'unknown')
        self.assertEqual(self.queue_store.get(self.thread), [])

    async def test_reservation_commit_then_lost_ack_appends_diagnostic_without_changing_original(self):
        update = self.goal_bindings.store.update
        committed = []
        def commit_then_lose_ack(updater):
            state = update(updater)
            if not committed and state.events[-1].reason == 'Original Project Goal input reserved':
                committed.append(next(row for row in state.bindings if row.id == self.goal_binding.id).model_copy(deep=True))
                raise OSError('private-reservation-ack')
            return state
        with patch.object(self.goal_bindings.store, 'update', side_effect=commit_then_lose_ack):
            await self.goal_continuation.dispatch_once(self.goal_binding.id, scope=self.actor.tenant)
        before, = committed
        self.assertEqual(self.current(), before)
        self.assertEqual(before.project_continuation.delivery.phase, 'reserved')
        self.assertEqual(before.project_continuation.reserved_calls, 1)
        self.assertEqual(self.queue_store.get(self.thread), [])
        event, = self.failures()
        self.assertEqual(event.dispatch_failure.stage, 'reservation')
        self.assertEqual(event.dispatch_failure.exception_type, 'os_error')
        self.assertEqual(event.causation_id, before.project_continuation.delivery.id)
        self.assertIn('current state preserved', event.reason)
        self.assertNotIn('private-reservation-ack', event.model_dump_json())

        # Same ID alone cannot attribute an event: the full immutable owner
        # and input fingerprint must match the locally prepared delivery.
        different_owner = before.project_continuation.delivery.model_copy(update={'continuation_owner_id': 'another-owner'})
        self.project_goal.mutate(before.id, self.actor.tenant, lambda row: row, 'Do not attribute another operation',
            dispatch_failure=_dispatch_failure('reservation', OSError()), failure_delivery=different_owner)
        self.assertEqual(len(self.failures()), 1)
        self.assertEqual(self.current(), before)
        await self.goal_continuation.dispatch_once(before.id, scope=self.actor.tenant)
        self.assertEqual(self.current(), before)
        self.assertEqual(len(self.failures()), 1)
        self.assertEqual(self.starts(), [])

    async def test_fast_real_start_then_acceptance_failure_preserves_receipt_and_records_diagnostic(self):
        mutate = self.project_goal.mutate
        started = []
        def lose_acceptance(binding_id, scope, updater, reason, **kwargs):
            if reason == 'Original Project Goal input queued':
                # The real queued start observer already committed its receipt.
                started.append(self.current().model_copy(deep=True))
                raise OSError('private-acceptance-write')
            return mutate(binding_id, scope, updater, reason, **kwargs)
        publish = self.execution.publish_queue_status
        drained = False
        async def drain_before_response(thread_id):
            nonlocal drained
            await publish(thread_id)
            if not drained:
                drained = True
                await self.execution.drain_thread_queue(thread_id)
        with patch.object(self.execution, 'publish_queue_status', side_effect=drain_before_response), \
                patch.object(self.project_goal, 'mutate', side_effect=lose_acceptance):
            await self.goal_continuation.dispatch_once(self.goal_binding.id, scope=self.actor.tenant)
        before, = started
        self.assertEqual(before.project_continuation.delivery.phase, 'started', self.native_diagnostics())
        self.assertEqual(len(self.starts()), 1, self.native_diagnostics())
        self.assertEqual(self.current(), before)
        event, = self.failures()
        self.assertEqual(event.dispatch_failure.stage, 'queue_acceptance')
        self.assertEqual(event.causation_id, before.project_continuation.delivery.id)
        self.assertEqual(event.dispatch_failure.exception_type, 'os_error')
        self.assertIn('current state preserved', event.reason)
        receipt = self.project_goal.preflight.store.get(before.project_continuation.delivery.admission_attempt_id)
        self.assertIsNotNone(receipt.queued_start_receipt)
        before_receipt = receipt.model_dump(mode='json')
        await self.goal_continuation.dispatch_once(before.id, scope=self.actor.tenant)
        self.assertEqual(self.current(), before)
        self.assertEqual(self.project_goal.preflight.store.get(receipt.id).model_dump(mode='json'), before_receipt)
        self.assertEqual(len(self.failures()), 1)
        self.assertEqual(len(self.starts()), 1)


class DispatchDiagnosticContractTests(unittest.TestCase):
    def test_legacy_read_normalizes_without_rewriting_or_adding_absent_diagnostics(self):
        event = GoalExecutionBindingEvent(binding_id='binding', goal_id='goal', event_type='binding_created',
            actor_id='owner', reason='Original reason', correlation_id='old-correlation', causation_id='old-cause')
        legacy = {'schema_version': '1.3', 'bindings': [], 'events': [event.model_dump(mode='json')]}
        before = copy.deepcopy(legacy)
        # Exercise the real store read: normalization must not write the backend.
        class Backend:
            def get(self, namespace):
                self.namespace = namespace
                return legacy
            def update(self, *_args, **_kwargs):
                raise AssertionError('Read migration must not write')
        state = GoalExecutionBindingStore(Backend()).load()
        self.assertEqual(legacy, before)
        self.assertEqual(state.schema_version, '1.4')
        self.assertEqual(state.events[0].model_dump(mode='json'), before['events'][0])
        self.assertNotIn('dispatch_failure', state.events[0].model_dump(mode='json'))
        with self.assertRaises(ContractCompatibilityError):
            ContractSpec('goal-execution-binding-state', '1.3', ('1.0', '1.1', '1.2', '1.3')).require(state.schema_version)
        with tempfile.TemporaryDirectory() as root:
            backend = SQLiteStateStore(Path(root) / 'state.sqlite3')
            backend.put(GoalExecutionBindingStore.namespace, before)
            store = GoalExecutionBindingStore(backend)
            self.assertEqual(store.load().schema_version, '1.4')
            self.assertEqual(backend.get(store.namespace), before)
            store.update(lambda current: current)
            written = backend.get(store.namespace)
            self.assertEqual(written['schema_version'], '1.4')
            self.assertEqual(written['events'], before['events'])
            with self.assertRaises(ContractCompatibilityError):
                ContractSpec('goal-execution-binding-state', '1.3', ('1.0', '1.1', '1.2', '1.3')).require(written['schema_version'])
        failure = ProjectGoalDispatchFailure(stage='queue_call', exception_type='timeout')
        state.events.append(event.model_copy(update={'dispatch_failure': failure}))
        restored = GoalExecutionBindingState.model_validate(state.model_dump(mode='json'))
        self.assertEqual(restored.events[-1].dispatch_failure, failure)

    def test_untrusted_exception_names_prose_and_codes_never_enter_diagnostic(self):
        private_type = type('credential_class_name', (Exception,), {})
        for error in (private_type('private-prose'),
                HTTPException(status_code=500, detail={'code': 'credential-unlisted-code', 'message': 'private-prose'})):
            failure = _dispatch_failure('queue_call', error)
            self.assertIn(failure.exception_type, {'exception', 'http_error'})
            self.assertIsNone(failure.code)
            self.assertNotIn('credential', failure.model_dump_json())
            self.assertNotIn('private-prose', failure.model_dump_json())
        with self.assertRaises(ValueError):
            ProjectGoalDispatchFailure(stage='arbitrary', exception_type='value_error')
        with self.assertRaises(ValueError):
            ProjectGoalDispatchFailure(stage='queue_call', exception_type='arbitrary-class')


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for cls in (ProjectGoalDispatchDiagnosticTests, DispatchDiagnosticContractTests):
        for name in cls.__dict__:
            if name.startswith('test_'):
                suite.addTest(cls(name))
    return suite
