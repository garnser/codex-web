"""Canonical pending custody and retained queue; external native input is a stub.

No application import, process, credentials, HTTP, model, or provider effects.
The preparation gate and preflight disposition are the production owners.
"""
import copy
import unittest
from unittest.mock import Mock, patch

from codex_web.execution_workers import AssignmentCancelRequest, AssignmentClaimRequest, AssignmentStartRequest
from codex_web.models import QueuedTurn
from codex_web.runtime.execution import ExecutionPrestartRefusal, TurnExecutionService
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.skills import SkillUpdate, SkillPublish
from codex_web.services.turn_execution_binding import TurnExecutionBindingService
from tests.test_turn_execution_binding import _Projects
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from tests import test_empty_native_preparation_sealing as empty
from tests import test_turn_execution as turns


class EmptyScopeCredentialPrestartRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = empty.EmptyNativePreparationSealingTests()
        await self.env.asyncSetUp()
        self.addAsyncCleanup(self.env.env.asyncTearDown)
        self.f = self.env.f

    def current(self):
        return self.f.workers.store.assignment(self.f.native_assignment.id)

    async def rejected_then_reviewed(self):
        """Retain real rejected discovery, then publish a genuinely fresh draft."""
        f = self.f
        original_clock = f.skills.clock
        f.skills.clock = lambda: self.env.env.content['review_due_at'] + 1
        try:
            with self.assertRaises(ExecutionPrestartRefusal) as caught:
                await self.env.env.admit()
        finally:
            f.skills.clock = original_clock
        self.assertIn('procedure_expired', caught.exception.detail['procedure_discovery']['blockers'])
        before = self.current().model_dump(mode='json')
        self.assertEqual(len(before['procedure_discoveries']), 1)
        self.assertIsNone(before['native_credential_custody']['admitted_at'])
        reviewed = copy.deepcopy(self.env.env.content)
        now = float(original_clock())
        reviewed.update(reviewed_at=now-1, review_due_at=now+3600, expires_at=now+7200)
        draft = f.skills.update('native-provider-required', SkillUpdate(
            operational_procedure=reviewed, reason='Private actual review; preserve rejected history'), actor=f.actor)
        f.skills.publish(draft['skillId'], draft['recordId'], SkillPublish(), actor=f.actor)
        return before

    def queued_runtime(self, gate=None):
        f = self.f
        host = turns._Host()
        host._project = lambda _id: f.project
        runtime = TurnExecutionService(host, actor_resolver=lambda *_args: f.actor)
        preflight = ExecutionPreflightService(ExecutionPreflightStore(f.sqlite))
        runtime.queued_preflight = preflight
        runtime.schedule_queue_drain = Mock()
        sent = Mock()
        async def native_boundary(*args, **kwargs):
            # Actual canonical preparation completes BEFORE the external send.
            if gate is None:
                await self.env.env.admit()
            else:
                await gate()
            sent('turn/start')
            return {'turn': {'id': 'private-external-native-turn'}}
        runtime.start_thread_turn_now = native_boundary
        queued = runtime.enqueue_turn(thread_id='native-provider-thread', project_id=f.project.id,
            message='Preserve this exact original', sandbox='read-only', approval_policy='on-request',
            execution_profile_id='orchestration-only', agent_profile_actor_id=f.actor.identity_id,
            execution_id='private-original-turn')
        return runtime, host, preflight, queued, sent

    async def test_closed_empty_seal_retains_known_prestart_original_without_send(self):
        before = await self.rejected_then_reviewed()
        runtime, host, preflight, original, sent = self.queued_runtime()
        snapshot = original.model_dump(mode='json')
        await runtime.drain_thread_queue(original.thread_id)
        saved = preflight.store.get('preflight-queued-'+original.id)
        self.assertEqual(saved.status, 'blocked')
        self.assertEqual(saved.blockers[0].code, 'procedure_credential_initial_admission_closed')
        self.assertEqual(saved.queued_turn_snapshot, snapshot)
        self.assertIsNone(saved.started_at)
        self.assertIsNone(saved.queued_start_correlation)
        self.assertIsNone(saved.queued_start_evidence)
        self.assertIsNone(saved.queued_start_receipt)
        self.assertIsNone(saved.queued_resume_correlation)
        self.assertEqual(self.current().model_dump(mode='json'), before)
        sent.assert_not_called()
        self.assertEqual(self.f.ops.calls, [])
        self.assertEqual(host._thread_queue_depth(original.thread_id), 0)
        runtime.schedule_queue_drain.assert_not_called()

    async def test_genuine_zero_candidate_closed_seal_is_also_typed_without_resealing(self):
        await self.rejected_then_reviewed()
        self.f.definition_registry.store.update(lambda rows: [r for r in rows if r.kind != 'agent.skill'])
        before = self.current().model_dump(mode='json')
        error = None
        try:
            await self.env.env.admit()
        except Exception as caught:
            error = caught
        self.assertIsInstance(error, ExecutionPrestartRefusal)
        self.assertEqual(error.detail['code'], 'procedure_credential_initial_admission_closed')
        self.assertEqual(self.current().model_dump(mode='json'), before)

    async def test_unexpected_seal_failure_stays_unknown_and_preserves_original(self):
        marker = RuntimeError('private storage boundary unavailable')
        def fail(*args, **kwargs):
            raise marker
        runtime, _, preflight, original, sent = self.queued_runtime()
        snapshot = original.model_dump(mode='json')
        with patch.object(self.env.env.owner, 'admit_initial_native_credentials', side_effect=fail):
            await runtime.drain_thread_queue(original.thread_id)
        saved = preflight.store.get('preflight-queued-'+original.id)
        self.assertEqual(saved.status, 'reconciliation_required')
        self.assertEqual(saved.queued_turn_snapshot, snapshot)
        self.assertIsNone(saved.queued_start_receipt)
        sent.assert_not_called()

    async def test_sent_unknown_transport_is_not_classified_as_prestart(self):
        runtime, _, preflight, original, sent = self.queued_runtime()
        async def sent_unknown(*args, **kwargs):
            await self.env.env.admit()
            sent('turn/start')
            raise TimeoutError('private response unobserved after send')
        runtime.start_thread_turn_now = sent_unknown
        snapshot = original.model_dump(mode='json')
        await runtime.drain_thread_queue(original.thread_id)
        saved = preflight.store.get('preflight-queued-'+original.id)
        self.assertEqual(saved.status, 'reconciliation_required')
        sent.assert_called_once_with('turn/start')
        self.assertEqual(saved.queued_turn_snapshot, snapshot)
        retained, retry_id, acquired = preflight.claim_retry(saved.id, actor=self.f.actor)
        self.assertFalse(acquired)
        self.assertIsNone(retry_id)
        self.assertEqual(retained.model_dump(mode="json"), saved.model_dump(mode="json"))
        self.assertIsNotNone(self.current().native_credential_custody.admitted_at)

    def fresh_bootstrap(self, profile='orchestration-only'):
        f = self.f
        previous = self.current()
        cancelled = f.workers.cancel_bootstrap(previous.id, AssignmentCancelRequest(
            expected_fence=previous.fence, reason='Private known preinput custody retirement'), actor=f.actor)
        self.assertEqual(cancelled.fence, previous.fence+1)
        self.assertIsNone(cancelled.lease)
        from codex_web.services.configuration import ConfigurationService
        from codex_web.storage.configuration_registry import ConfigurationRegistryStore
        if profile == 'repository-write':
            f.resources.bind_project(project=f.project, resource_id=f.repo.id, actor=f.actor)
        builder = TurnExecutionBindingService(ConfigurationService(ConfigurationRegistryStore(f.sqlite)),
            _Projects(f.project), f.resources, f.workspaces, f.workers, control_actor=f.actor,
            execution_profiles=f.profiles, runtime_binding=previous.runtime_binding)
        options = ({'explicit_repository_id':f.repo.id, 'writable_repository_ids':(f.repo.id,)}
                   if profile == 'repository-write' else {})
        bound = builder.prepare_bootstrap(bootstrap_id='private-fresh-'+profile,
            execution_id='thread-bootstrap-private-fresh-'+profile, project_id=f.project.id,
            sandbox='workspace-write' if options else 'read-only', approval_policy='on-request',
            execution_profile_id=profile, **options)
        claim = f.workers.claim(f.procedure_worker.id, AssignmentClaimRequest(lease_seconds=3600),
            actor=f.procedure_worker_actor, assignment_id=bound.assignment_id)
        self.assertIsNotNone(claim)
        f.native_assignment = f.workers.start(f.procedure_worker.id, bound.assignment_id,
            AssignmentStartRequest(fence=claim.fence, lease_token=claim.lease.lease_token),
            actor=f.procedure_worker_actor)
        return cancelled

    async def test_fresh_canonical_custody_retry_preserves_original_and_sends_once(self):
        before = await self.rejected_then_reviewed()
        runtime, host, preflight, original, sent = self.queued_runtime()
        await runtime.drain_thread_queue(original.thread_id)
        saved = preflight.store.get('preflight-queued-'+original.id)
        self.assertEqual(saved.status, 'blocked')
        old = self.f.native_assignment.id
        self.fresh_bootstrap()
        retained = self.f.workers.store.assignment(old)
        self.assertEqual(retained.procedure_discoveries[0].model_dump(mode='json'), before['procedure_discoveries'][0])
        claimed, claim_id, owned = preflight.claim_retry(saved.id, actor=self.f.actor)
        self.assertTrue(owned)
        # Match TurnService.retry_preflight: reconstruct the immutable retained
        # request, enqueue it, then commit the retry claim. The drained object
        # has already had its local attempt counter incremented.
        retry_original = QueuedTurn.model_validate(claimed.queued_turn_snapshot)
        runtime.requeue_turn_front(retry_original)
        committed = preflight.queue_retry_committed(saved.id, claim_id=claim_id)
        self.assertEqual(committed.queued_turn_snapshot, saved.queued_turn_snapshot)
        await runtime.drain_thread_queue(original.thread_id)
        final = preflight.store.get(saved.id)
        self.assertEqual(final.status, 'started')
        self.assertEqual(final.queued_turn_snapshot, saved.queued_turn_snapshot)
        sent.assert_called_once_with('turn/start')
        self.assertEqual(host._thread_queue_depth(original.thread_id), 0)
        self.assertEqual(self.f.workers.store.assignment(old), retained)
        self.assertIsNotNone(self.current().native_credential_custody.admitted_at)
        self.assertEqual(self.current().native_credential_custody.procedures, ())

    async def test_selected_credential_error_keeps_existing_prestart_contract(self):
        self.fresh_bootstrap('repository-write')
        # Hold a genuine prior discovery on NEW pending selected custody. The
        # selected path must keep its already-existing typed refusal unchanged.
        context = self.env.env.admission._preparation_context(self.current(), self.f.actor,
            self.f.project, self.current().sandbox, 'a'*64)
        discovery, _ = self.f.discovery.discover(context)
        self.f.workers.record_procedure_discovery(self.current().id, discovery,
            expected_fence=self.current().fence, actor=self.f.actor)
        before = self.current().model_dump(mode='json')
        error = None
        try:
            await self.env.env.admit()
        except Exception as caught:
            error = caught
        self.assertIsInstance(error, ExecutionPrestartRefusal)
        self.assertEqual(error.detail['code'], 'procedure_credential_initial_admission_closed')
        self.assertEqual(self.current().model_dump(mode='json'), before)
        self.assertEqual(self.f.ops.calls, [])
