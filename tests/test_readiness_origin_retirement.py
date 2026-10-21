"""Actual canonical writer interleavings, without provider mutation/replay."""
import asyncio
import threading
import time
import unittest
from unittest.mock import patch

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
