"""Exact worker-expiry selection against private real canonical worker/store owners."""
import unittest
from codex_web.execution_workers import AssignmentClaimRequest, AssignmentStartRequest
from tests import test_execution_workers as worker_fixture


class ExactCustodyExpiryTests(unittest.TestCase):
    def setUp(self):
        self.env=worker_fixture.ExecutionWorkerServiceTests()
        self.env.setUp()
        self.addCleanup(self.env.tearDown)

    def targets(self):
        f=self.env
        target=f._assignment()
        claim=f.service.claim(f.worker.id,AssignmentClaimRequest(lease_seconds=10),
            actor=f.worker_actor,assignment_id=target.id)
        f.service.start(f.worker.id,target.id,AssignmentStartRequest(
            lease_token=claim.lease.lease_token,fence=claim.fence),actor=f.worker_actor)
        unrelated=f._assignment(execution_id='unrelated-execution', deadline_at=claim.lease.expires_at)
        foreign=unrelated.model_copy(update={'id':'foreign-target','organization_id':'foreign-tenant',
            'execution_id':'foreign-execution'})
        def seed(state):
            state.assignments.append(foreign)
            return state
        f.service.store.update(seed)
        return target,claim,unrelated,foreign

    def test_exact_expiry_leaves_other_eligible_and_foreign_targets_untouched(self):
        target,claim,other,foreign=self.targets()
        f=self.env
        before={i:f.service.store.assignment(i).model_dump(mode='json') for i in (other.id,foreign.id)}
        recovered=f.service.recover_expired(actor=f.admin,now=claim.lease.expires_at+1,assignment_id=target.id)
        self.assertEqual(recovered,[target.id])
        self.assertEqual(f.service.store.assignment(target.id).status,'lost')
        self.assertIsNone(f.service.store.assignment(target.id).lease)
        self.assertEqual({i:f.service.store.assignment(i).model_dump(mode='json') for i in before},before)

    def test_renewed_or_missing_target_has_no_effect(self):
        target,claim,other,foreign=self.targets()
        f=self.env
        before={i:f.service.store.assignment(i).model_dump(mode='json') for i in (target.id,other.id,foreign.id)}
        self.assertEqual(f.service.recover_expired(actor=f.admin,now=claim.lease.expires_at-1,
            assignment_id=target.id),[])
        self.assertEqual(f.service.recover_expired(actor=f.admin,now=claim.lease.expires_at+1,
            assignment_id='missing-target'),[])
        self.assertEqual({i:f.service.store.assignment(i).model_dump(mode='json') for i in before},before)

    def test_foreign_exact_target_is_not_adopted_or_expired(self):
        target,claim,other,foreign=self.targets()
        f=self.env
        before=f.service.store.assignment(foreign.id).model_dump(mode='json')
        self.assertEqual(f.service.recover_expired(actor=f.admin,now=claim.lease.expires_at+1,
            assignment_id=foreign.id),[])
        self.assertEqual(f.service.store.assignment(foreign.id).model_dump(mode='json'),before)

    def test_expected_fence_is_rechecked_inside_canonical_writer(self):
        target, claim, other, foreign = self.targets()
        f = self.env
        transition = f.service.store.transition_assignment
        def successor_before_writer(worker_id, assignment_id, updater, **kwargs):
            def advance(state):
                for index, item in enumerate(state.assignments):
                    if item.id == target.id:
                        state.assignments[index] = item.model_copy(update={
                            'fence': item.fence + 1,
                            'lease': item.lease.model_copy(update={'fence': item.fence + 1})})
                return state
            f.service.store.update(advance)
            return transition(worker_id, assignment_id, updater, **kwargs)
        f.service.store.transition_assignment = successor_before_writer
        self.assertEqual(f.service.recover_expired(actor=f.admin,
            now=claim.lease.expires_at+1, assignment_id=target.id, expected_fence=claim.fence), [])
        successor = f.service.store.assignment(target.id)
        self.assertEqual(successor.fence, claim.fence+1)
        self.assertEqual(successor.status, 'running')
        self.assertIsNotNone(successor.lease)

    def test_stale_fence_and_unscoped_fence_do_not_mutate(self):
        target, claim, other, foreign = self.targets()
        f = self.env
        before = f.service.store.assignment(target.id).model_dump(mode='json')
        self.assertEqual(f.service.recover_expired(actor=f.admin,
            now=claim.lease.expires_at+1, assignment_id=target.id, expected_fence=claim.fence+1), [])
        with self.assertRaises(ValueError):
            f.service.recover_expired(actor=f.admin, now=claim.lease.expires_at+1, expected_fence=claim.fence)
        self.assertEqual(f.service.store.assignment(target.id).model_dump(mode='json'), before)


    def test_known_pre_effect_busy_then_real_expiry_uses_same_canonical_target(self):
        self._waiting_expiry_case(known=True)

    def test_unknown_previous_owner_never_expires_target(self):
        self._waiting_expiry_case(known=False)

    def _waiting_expiry_case(self, *, known):
        import asyncio
        import time
        from types import SimpleNamespace
        from codex_web.models import WorkItemHandoff
        from codex_web.procedure_control_plane import CanonicalPreviousStep
        from codex_web.services.procedure_custody_recovery import CustodyInput, CustodyOperation
        target, claim, other, foreign = self.targets()
        f = self.env
        target = f.service.store.assignment(target.id)
        state = SimpleNamespace(ref=target.work_item_ref, organization_id=target.organization_id,
            workspace_id=target.workspace_id, project_id=target.project_id, resource_ids=target.resource_ids,
            current_owner='sender', next_owner='recipient', handoff=WorkItemHandoff(
                from_agent='sender', to_agent='recipient', requested_at=10))
        binding = SimpleNamespace(assignment_id=target.id, execution_id=target.execution_id,
            execution_workspace_id=target.execution_workspace_id, thread_id='target-thread')
        async def runner(_):
            return {'runner':{'assignmentId':target.id,'fence':target.fence,'pendingExecutorWork':1},
                'activity':{'state':'active','turnId':'genuine-busy'}}
        owner = CustodyOperation(operation='work.custody.recover.v1', workers=f.service,
            routing=SimpleNamespace(snapshot=lambda _:SimpleNamespace(owner=lambda role:role),
                bootstraps=SimpleNamespace(get_by_bootstrap=lambda *_:binding)),
            threads=SimpleNamespace(runner=runner), continuity=SimpleNamespace(thread_is_active=lambda _:True),
            get_work=lambda _:state, preflight=None)
        context = SimpleNamespace(assignment=target.model_copy(update={'id':'authorized-supervisor'}),
            resource_ids=target.resource_ids, requester_actor=f.admin, validate_current=lambda:None,
            previous_step=None)
        value = CustodyInput(work_item_ref=state.ref, target_assignment_id=target.id,
            expected_target_fence=target.fence, expected_handoff_requested_at=10)
        first = asyncio.run(owner.execute(context,value))
        self.assertEqual(first.disposition,'waiting')
        self.assertEqual(first.code,'custody_before_effect:busy_or_completion_pending')
        def expire(store):
            for index,item in enumerate(store.assignments):
                if item.id == target.id:
                    store.assignments[index] = item.model_copy(update={
                        'lease':item.lease.model_copy(update={'expires_at':time.time()-1})})
            return store
        f.service.store.update(expire)
        context.previous_step = CanonicalPreviousStep(status='waiting' if known else 'reconciliation_required',
            failure_code=first.code if known else 'native_unknown',
            diagnostic_evidence_ids=('engine-validated-diagnostic',) if known else (),
            operation_code=first.code if known else None)
        result = asyncio.run(owner.reconcile(context,value))
        actual = f.service.store.assignment(target.id)
        self.assertEqual(actual.status,'lost' if known else 'running')
        self.assertEqual(actual.fence,target.fence)
        self.assertEqual(result.disposition,'succeeded' if known else 'waiting')
        self.assertEqual(state.handoff.requested_at,10)



class ExactWorkDeliveryProofTests(unittest.TestCase):
    """Real SQLite retained/index owners; native response itself is a typed fixture edge."""
    def setUp(self):
        from tests import test_execution_preflight as fixture
        self.env = fixture.ExecutionPreflightServiceTests()
        self.env.setUp()
        self.addCleanup(self.env.tearDown)
        self.env.store.prepare_work_item_index()

    def delivery(self):
        from types import SimpleNamespace
        from codex_web.models import (QueuedTurn, QueuedNativeStartCorrelation,
            QueuedNativeStartEvidence, QueuedNativeStartReceipt, WorkItemHandoff, WorkItemHandoffDelivery)
        from codex_web.services.procedure_custody_recovery import CustodyOperation
        f = self.env
        # More than the former thread-history cap, all unrelated canonical Work.
        for index in range(55):
            f.service.retain_queued(QueuedTurn(id=f'unrelated-{index}', thread_id='recipient',
                project_id='project', message='unrelated', work_item_ref=f'other!{index}',
                sandbox=None, approval_policy=None, created_at=index+1),
                organization_id='org', workspace_id='ws')
        q = QueuedTurn(id='original-handoff', thread_id='recipient', project_id='project',
            execution_id='requested-execution', message='exact handoff', work_item_ref='repo!1',
            execution_profile_id='repository-write', repository_resource_id='repo',
            writable_repository_resource_ids=('repo',), sandbox=None, approval_policy=None, created_at=100)
        attempt = f.service.retain_queued(q, organization_id='org', workspace_id='ws')
        _, claim = f.service.claim_queued_admission(attempt.id)
        self.assertTrue(f.service.begin_queued_start(attempt.id, claim_id=claim))
        correlation = QueuedNativeStartCorrelation(snapshot_sha256=f.service.queued_snapshot_digest(q.model_dump(mode='json')),
            admission_claim_id=claim, requested_execution_id=q.execution_id, organization_id='org',
            workspace_id='ws', project_id='project', actor_identity_id='requester',
            execution_profile_id='repository-write', repository_resource_id='repo',
            writable_repository_resource_ids=('repo',), assignment_resource_ids=('repo',),
            assignment_id='recipient-assignment', execution_id='recipient-bootstrap', worker_id='worker', fence=1,
            thread_id='recipient', provider_native_session_id='native-session', runtime_generation='generation',
            rpc_request_id='rpc', prepared_at=101)
        evidence = QueuedNativeStartEvidence(runtime_generation='generation', rpc_request_id='rpc',
            provider_native_session_id='native-session', provider_native_turn_id='native-turn', observed_at=102)
        receipt = QueuedNativeStartReceipt(admission_claim_id=claim, requested_execution_id=q.execution_id,
            execution_id='recipient-bootstrap', thread_id='recipient', provider_native_session_id='native-session',
            provider_native_turn_id='native-turn', provider_id='openai', runtime_id='codex', observed_at=102)
        attempt = f.store.update(attempt.id, lambda current: current.model_copy(update={
            'status':'started', 'started_at':102, 'queued_start_correlation':correlation,
            'queued_start_evidence':evidence, 'queued_start_receipt':receipt}))
        state = SimpleNamespace(ref='repo!1', handoff=WorkItemHandoff(from_agent='sender', to_agent='recipient',
            requested_at=100, acknowledged_at=103, status='accepted', delivery=WorkItemHandoffDelivery(
                status='delivered', started_at=102, thread_id='recipient', turn_id='native-turn',
                execution_id='recipient-bootstrap', scope_verified=True, execution_profile_id='repository-write',
                repository_resource_id='repo', writable_repository_resource_ids=('repo',))))
        context = SimpleNamespace(assignment=SimpleNamespace(organization_id='org', workspace_id='ws',
            project_id='project'), resource_ids=('repo',), requester_actor=f.admin)
        owner = CustodyOperation(operation='work.handoff.inspect.v1', workers=None, routing=None,
            threads=None, continuity=None, get_work=None, preflight=f.service)
        return owner, context, state, attempt

    def test_exact_work_index_proves_start_after_unrelated_lifetime_thread_history(self):
        owner, context, state, attempt = self.delivery()
        result = owner._verify_delivery(context, None, state, {})
        self.assertEqual(result.disposition, 'succeeded')
        self.assertEqual(result.original_references, (attempt.id,))
        self.assertTrue(result.facts['actual_start'])
        self.assertTrue(result.facts['actual_ack'])

    def test_exact_delivery_tuple_drift_and_unacknowledged_delivery_remain_waiting(self):
        owner, context, state, attempt = self.delivery()
        for field, value in [('turn_id','foreign-turn'), ('execution_id','foreign-execution'),
                ('thread_id','foreign-thread'), ('writable_repository_resource_ids',('foreign',))]:
            with self.subTest(field=field):
                from types import SimpleNamespace
                changed = SimpleNamespace(ref=state.ref, handoff=state.handoff.model_copy(update={
                    'delivery':state.handoff.delivery.model_copy(update={field:value})}))
                self.assertEqual(owner._verify_delivery(context,None,changed,{}).disposition, 'waiting')
        state.handoff = state.handoff.model_copy(update={'acknowledged_at':None})
        self.assertEqual(owner._verify_delivery(context,None,state,{}).disposition,'waiting')

    def test_incomplete_work_index_cannot_prove_delivery(self):
        owner, context, state, attempt = self.delivery()
        self.env.store.store.record_apply(self.env.store.NAMESPACE, upserts={
            'metadata/work-item-index-1.0': {'schema_version':'1.0','complete':False}})
        result = owner._verify_delivery(context,None,state,{})
        self.assertEqual(result.disposition,'waiting')
        self.assertEqual(result.code,'custody_start_inventory_incomplete')
