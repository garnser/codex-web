import unittest
from unittest.mock import patch

from codex_web.execution_workers import AssignmentClaimRequest
from codex_web.procedure_discovery import ProcedureDiscovery
from codex_web.services.execution_workers import WorkerLeaseError
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
