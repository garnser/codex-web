import unittest
from unittest.mock import patch

from codex_web.execution_workers import AssignmentClaimRequest
from codex_web.procedure_discovery import ProcedureDiscovery
from codex_web.services.execution_workers import WorkerLeaseError, ProcedureHistoryExhaustedError
from codex_web.services.identity import AuthorizationError
from tests import test_execution_workers as fixtures


class ProcedureAssignmentEvidenceTests(unittest.TestCase):
    setUp = fixtures.ExecutionWorkerServiceTests.setUp
    tearDown = fixtures.ExecutionWorkerServiceTests.tearDown
    _assignment = fixtures.ExecutionWorkerServiceTests._assignment

    def test_evidence_append_requires_exact_live_scope_and_fence_without_changing_authority(self):
        assignment = self._assignment()
        claimed = self.service.claim(self.worker.id, AssignmentClaimRequest(lease_seconds=30), actor=self.worker_actor, assignment_id=assignment.id)
        discovery = ProcedureDiscovery(actor_identity_id=self.admin.identity_id, organization_id='local', workspace_id='default', project_id='home',
            resource_ids=('repo-1',), operation='repository.read', context_checksum='context', evaluated_at=1,
            outcome='no_match', policy_source='canonical:existing-policy', policy_allows=True)
        with self.assertRaises(WorkerLeaseError):
            self.service.record_procedure_discovery(assignment.id, discovery, expected_fence=claimed.fence+1, actor=self.admin)
        with self.assertRaises(AuthorizationError):
            self.service.record_procedure_discovery(assignment.id, discovery.model_copy(update={'workspace_id':'other'}), expected_fence=claimed.fence, actor=self.admin)
        updated = self.service.record_procedure_discovery(assignment.id, discovery, expected_fence=claimed.fence, actor=self.admin)
        self.assertEqual(updated.procedure_discoveries,(discovery,))
        self.assertEqual(updated.lease,claimed.lease)
        self.assertEqual(updated.fence,claimed.fence)
        self.assertEqual(updated.required_capabilities,claimed.required_capabilities)
        self.assertEqual(updated.status,claimed.status)

    def test_procedure_evidence_append_is_bounded_and_preserves_unrelated_rows(self):
        assignment = self._assignment()
        claimed = self.service.claim(self.worker.id, AssignmentClaimRequest(lease_seconds=30), actor=self.worker_actor, assignment_id=assignment.id)
        discovery = ProcedureDiscovery(actor_identity_id=self.admin.identity_id, organization_id='local', workspace_id='default', project_id='home',
            resource_ids=('repo-1',), operation='repository.read', context_checksum='bounded-context', evaluated_at=1,
            outcome='no_match', policy_source='canonical:existing-policy', policy_allows=True)
        store = self.service.store.store
        unrelated = {f'unrelated-{index}': {'opaque': index} for index in range(1000)}
        namespaces = (self.service.store.worker_namespace, self.service.store.assignment_namespace)
        for namespace in namespaces:
            store.record_apply(namespace, upserts=unrelated)
        with patch.object(store, 'record_items', side_effect=AssertionError('procedure append scanned global records')), \
                patch.object(store, '_decode', wraps=store._decode) as decode:
            updated = self.service.record_procedure_discovery(assignment.id, discovery, expected_fence=claimed.fence, actor=self.admin)
            self.assertEqual(updated.procedure_discoveries, (discovery,))
            self.assertLessEqual(decode.call_count, 8)
        for namespace in namespaces:
            for key in ('unrelated-0', 'unrelated-999'):
                self.assertEqual(store.record_get(namespace, key), unrelated[key])

    def test_exact_history_limit_preserves_original_evidence_and_lease(self):
        assignment = self._assignment()
        claimed = self.service.claim(self.worker.id, AssignmentClaimRequest(lease_seconds=30),
            actor=self.worker_actor, assignment_id=assignment.id)
        discoveries = tuple(ProcedureDiscovery(actor_identity_id=self.admin.identity_id,
            organization_id='local', workspace_id='default', project_id='home',
            resource_ids=('repo-1',), operation='repository.read', context_checksum=f'original-{index}',
            evaluated_at=1, outcome='no_match', policy_source='canonical:existing-policy', policy_allows=True)
            for index in range(65))
        for discovery in discoveries[:63]:
            self.service.record_procedure_discovery(assignment.id, discovery,
                expected_fence=claimed.fence, actor=self.admin)
        last = self.service.record_procedure_discovery(assignment.id, discoveries[63],
            expected_fence=claimed.fence, actor=self.admin)
        self.assertEqual(last.procedure_discoveries, discoveries[:64])
        with self.assertRaises(ProcedureHistoryExhaustedError):
            self.service.record_procedure_discovery(assignment.id, discoveries[64],
                expected_fence=claimed.fence, actor=self.admin)
        current = self.service.store.assignment(assignment.id)
        self.assertEqual(current.procedure_discoveries, discoveries[:64])
        self.assertEqual(current.lease, claimed.lease)
        self.assertEqual(current.fence, claimed.fence)

    def _claimed_with_discovery(self):
        assignment = self._assignment()
        claimed = self.service.claim(self.worker.id, AssignmentClaimRequest(lease_seconds=30),
            actor=self.worker_actor, assignment_id=assignment.id)
        discovery = ProcedureDiscovery(actor_identity_id=self.admin.identity_id,
            organization_id='local', workspace_id='default', project_id='home',
            resource_ids=('repo-1',), operation='repository.read', context_checksum='same-context',
            evaluated_at=1, outcome='no_match', policy_source='canonical:existing-policy', policy_allows=True)
        self.service.record_procedure_discovery(assignment.id, discovery,
            expected_fence=claimed.fence, actor=self.admin)
        return claimed, discovery

    def test_adjacent_identical_checkpoints_retain_original_evidence_and_timestamp(self):
        claimed, discovery = self._claimed_with_discovery()
        before = self.service.store.assignment(claimed.id)
        for evaluated_at in range(2, 80):
            current = self.service.record_procedure_discovery(claimed.id,
                discovery.model_copy(update={'evaluated_at':evaluated_at}),
                expected_fence=claimed.fence, actor=self.admin)
        self.assertEqual(current, before)
        self.assertEqual(current.procedure_discoveries, (discovery,))

    def test_identical_at_capacity_is_allowed_but_every_semantic_change_still_refuses(self):
        claimed, discovery = self._claimed_with_discovery()
        for index in range(1, 64):
            discovery = discovery.model_copy(update={'context_checksum':f'context-{index}'})
            self.service.record_procedure_discovery(claimed.id, discovery,
                expected_fence=claimed.fence, actor=self.admin)
        before = self.service.store.assignment(claimed.id)
        updated = self.service.record_procedure_discovery(claimed.id,
            discovery.model_copy(update={'evaluated_at':99}),
            expected_fence=claimed.fence, actor=self.admin)
        self.assertEqual(updated, before)
        changes = [{'context_checksum':'different'}, {'actor_identity_id':'different-requester'},
                   {'outcome':'failed'}, {'policy_source':'changed'}, {'policy_allows':False},
                   {'resource_ids':('repo-2',)}, {'operation':'repository.write'},
                   {'blockers':('changed',)}, {'supporting_sources':('changed',)},
                   {'capability_sources':('changed',)}, {'instruction_checksum':'different'}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ProcedureHistoryExhaustedError):
                self.service.record_procedure_discovery(claimed.id, discovery.model_copy(update=change),
                    expected_fence=claimed.fence, actor=self.admin)
        self.assertEqual(self.service.store.assignment(claimed.id), before)

    def test_nonadjacent_repeat_and_changed_context_both_append(self):
        claimed, discovery = self._claimed_with_discovery()
        changed = discovery.model_copy(update={'context_checksum':'changed'})
        self.service.record_procedure_discovery(claimed.id, changed,
            expected_fence=claimed.fence, actor=self.admin)
        current = self.service.record_procedure_discovery(claimed.id, discovery,
            expected_fence=claimed.fence, actor=self.admin)
        self.assertEqual(current.procedure_discoveries, (discovery, changed, discovery))

    def test_duplicate_never_bypasses_current_requester_fence_scope_status_or_lease(self):
        from codex_web.execution_workers import AssignmentStatus
        claimed, discovery = self._claimed_with_discovery()
        before = self.service.store.assignment(claimed.id)
        with self.assertRaises(AuthorizationError):
            self.service.record_procedure_discovery(claimed.id, discovery,
                expected_fence=claimed.fence, actor=self.worker_actor)
        with self.assertRaises(WorkerLeaseError):
            self.service.record_procedure_discovery(claimed.id, discovery,
                expected_fence=claimed.fence+1, actor=self.admin)
        with self.assertRaises(AuthorizationError):
            self.service.record_procedure_discovery(claimed.id,
                discovery.model_copy(update={'project_id':'changed'}),
                expected_fence=claimed.fence, actor=self.admin)
        for change in [{'lease':None},
                       {'lease':claimed.lease.model_copy(update={'expires_at':0})},
                       {'status':AssignmentStatus.CANCELLED},
                       {'project_id':'changed'}]:
            with self.subTest(change=change):
                def mutate(state):
                    item = state.assignments[0]
                    for key, value in change.items():
                        setattr(item, key, value)
                    return state
                self.service.store.update_assignment(self.worker.id, claimed.id, mutate)
                expected_error = AuthorizationError if 'project_id' in change else WorkerLeaseError
                with self.assertRaises(expected_error):
                    self.service.record_procedure_discovery(claimed.id, discovery,
                        expected_fence=claimed.fence, actor=self.admin)
                def restore(state):
                    state.assignments[0] = before.model_copy(deep=True)
                    return state
                self.service.store.update_assignment(self.worker.id, claimed.id, restore)
        self.assertEqual(self.service.store.assignment(claimed.id), before)
