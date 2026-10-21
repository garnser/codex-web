"""A bounded append-only reserve never grants execution or changes custody."""
import time
import unittest
import uuid
from dataclasses import replace

from codex_web.execution_workers import AssignmentClaimRequest, AssignmentRenewRequest, ExecutionAssignment
from codex_web.procedure_discovery import ProcedureDiscovery
from codex_web.procedure_reconciliation import ReadOnlyReconciliationScope, read_only_reconciliation, RECOVERY_PREFIX
from codex_web.services.execution_workers import WorkerLeaseError, ProcedureHistoryExhaustedError
from tests import test_execution_workers as fixtures


class ReadOnlyReconciliationBudgetTests(unittest.TestCase):
    setUp = fixtures.ExecutionWorkerServiceTests.setUp
    tearDown = fixtures.ExecutionWorkerServiceTests.tearDown
    _assignment = fixtures.ExecutionWorkerServiceTests._assignment

    def full(self):
        assignment = self._assignment()
        self.claimed = self.service.claim(self.worker.id, AssignmentClaimRequest(lease_seconds=300),
            actor=self.worker_actor, assignment_id=assignment.id)
        self.discovery = ProcedureDiscovery(actor_identity_id=self.admin.identity_id,
            organization_id='local', workspace_id='default', project_id='home', resource_ids=('repo-1',),
            operation='action_intent.reconcile', context_checksum='current', evaluated_at=time.time(),
            outcome='no_match', policy_source='canonical:test', policy_allows=True)
        for index in range(64):
            self.record(self.discovery.model_copy(update={'operation':'repository.read', 'context_checksum':str(index)}))
        self.originals = self.service.store.assignment(assignment.id).procedure_discoveries
        return ReadOnlyReconciliationScope(id=uuid.uuid4().hex, intent_id='action-intent-unit',
            assignment_id=assignment.id, execution_id=assignment.execution_id,
            worker_id=self.worker.id, fence=self.claimed.fence, requester_identity_id=self.admin.identity_id,
            organization_id='local', workspace_id='default', project_id='home', resource_ids=('repo-1',),
            agent_profile=self.claimed.agent_profile, execution_profile_definition=self.claimed.execution_profile_definition,
            expires_at=time.time()+300)

    def record(self, discovery):
        return self.service.record_procedure_discovery(self.claimed.id, discovery,
            expected_fence=self.claimed.fence, actor=self.admin)

    def test_normal_limit_originals_unchanged_and_readonly_reserve_is_durable_bounded(self):
        scope = self.full()
        with self.assertRaises(ProcedureHistoryExhaustedError):
            self.record(self.discovery)
        for attempt in range(4):
            selected = replace(scope, id=uuid.uuid4().hex)
            with read_only_reconciliation(selected):
                for decision in range(8):
                    current = self.record(self.discovery.model_copy(update={'context_checksum':str(decision)}))
                    self.assertEqual(current.procedure_discoveries[:64], self.originals)
                # Fresh identical decision/evaluation time is retained once.
                self.assertEqual(self.record(self.discovery.model_copy(update={
                    'context_checksum':'7','evaluated_at':time.time()+1})).procedure_discoveries,
                    current.procedure_discoveries)
                with self.assertRaises(ProcedureHistoryExhaustedError):
                    self.record(self.discovery.model_copy(update={'context_checksum':'overflow'}))
        with read_only_reconciliation(replace(scope, id=uuid.uuid4().hex)):
            with self.assertRaisesRegex(ProcedureHistoryExhaustedError, 'attempt limit'):
                self.record(self.discovery)
        current = self.service.store.assignment(self.claimed.id)
        self.assertEqual(len(current.procedure_discoveries), 96)
        self.assertEqual(current.procedure_discoveries[:64], self.originals)
        self.assertEqual(current.lease, self.claimed.lease)
        self.assertEqual(current.fence, self.claimed.fence)
        with self.assertRaises(ProcedureHistoryExhaustedError):
            self.record(self.discovery.model_copy(update={'operation':'repository.pull-request.merge'}))

    def test_legacy_model_and_ordinary_renewal_roundtrip_append_only_evidence(self):
        scope = self.full()
        with read_only_reconciliation(scope):
            current = self.record(self.discovery)
        raw = current.model_dump(mode='json')
        self.assertEqual(ExecutionAssignment.model_validate(raw).model_dump(mode='json'), raw)
        renewed = self.service.renew(self.worker.id, self.claimed.id,
            AssignmentRenewRequest(fence=self.claimed.fence, lease_token=self.claimed.lease.lease_token,
                lease_seconds=300), actor=self.worker_actor)
        self.assertEqual(renewed.procedure_discoveries, current.procedure_discoveries)
        self.assertEqual(renewed.procedure_discoveries[:64], self.originals)

    def test_concurrent_identical_checkpoints_are_atomic_and_cold_reopen_keeps_budget(self):
        from concurrent.futures import ThreadPoolExecutor
        from codex_web.services.execution_workers import ExecutionWorkerService
        from codex_web.storage.execution_workers import ExecutionWorkerStore
        from codex_web.storage.sqlite_state import SQLiteStateStore
        from pathlib import Path
        scope = self.full()
        def checkpoint(_):
            with read_only_reconciliation(scope):
                return self.record(self.discovery)
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(checkpoint, range(12)))
        current = self.service.store.assignment(self.claimed.id)
        self.assertEqual(len(current.procedure_discoveries), 65)
        self.assertEqual(current.procedure_discoveries[:64], self.originals)
        self.service = ExecutionWorkerService(ExecutionWorkerStore(SQLiteStateStore(
            Path(self.temp.name)/'state.sqlite3')), identity=self.identity)
        with read_only_reconciliation(scope):
            self.assertEqual(self.record(self.discovery).procedure_discoveries,
                current.procedure_discoveries)
        with self.assertRaises(ProcedureHistoryExhaustedError):
            self.record(self.discovery.model_copy(update={'operation':'repository.pull-request.merge'}))

    def test_frozen_installed_reader_and_writer_preserve_extension(self):
        import json
        import os
        import subprocess
        import sys
        legacy = os.environ.get('CODEX_WEB_LEGACY_CHECKOUT')
        if not legacy:
            self.skipTest('set CODEX_WEB_LEGACY_CHECKOUT for frozen installed-reader qualification')
        scope = self.full()
        with read_only_reconciliation(scope):
            retained = self.record(self.discovery)
        script = """
import json, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.execution_workers import ExecutionWorkerStore
from codex_web.services.identity import IdentityService
from codex_web.services.execution_workers import ExecutionWorkerService
from codex_web.identity import AuthenticationActor
from codex_web.execution_workers import AssignmentRenewRequest
payload = json.load(sys.stdin)
store = SQLiteStateStore(Path(payload['database']))
service = ExecutionWorkerService(ExecutionWorkerStore(store), identity=IdentityService(IdentityStateStore(store)))
actor = AuthenticationActor.model_validate(payload['actor'])
before = service.store.assignment(payload['assignment_id'])
after = service.renew(payload['worker_id'], before.id, AssignmentRenewRequest(
    fence=before.fence, lease_token=before.lease.lease_token, lease_seconds=300), actor=actor)
assert before.procedure_discoveries == after.procedure_discoveries
print(json.dumps([item.model_dump(mode='json') for item in after.procedure_discoveries]))
"""
        result = subprocess.run([sys.executable, '-c', script, legacy], input=json.dumps({
            'database':str(__import__('pathlib').Path(self.temp.name)/'state.sqlite3'),
            'actor':self.worker_actor.model_dump(mode='json'), 'worker_id':self.worker.id,
            'assignment_id':self.claimed.id}), text=True, capture_output=True, timeout=30, check=True,
            cwd=self.temp.name)
        self.assertEqual(json.loads(result.stdout), [entry.model_dump(mode='json')
            for entry in retained.procedure_discoveries])
        self.assertEqual(self.service.store.assignment(self.claimed.id).procedure_discoveries,
            retained.procedure_discoveries)

    def test_fresh_actor_scope_fence_lease_resources_and_provider_mutation_refuse(self):
        scope = self.full()
        for changes in ({'fence':scope.fence+1}, {'worker_id':'foreign-worker'},
                        {'requester_identity_id':'foreign-actor'}, {'expires_at':0},
                        {'assignment_id':'foreign-assignment'}, {'project_id':'foreign-project'}):
            with self.subTest(changes=changes), read_only_reconciliation(replace(scope, **changes)):
                with self.assertRaises(WorkerLeaseError):
                    self.record(self.discovery)
        with read_only_reconciliation(scope):
            for changes in ({'resource_ids':()}, {'resource_ids':('foreign-repo',)},
                            {'operation':'repository.pull-request.ready'},
                            {'operation':'code-host.pull-request.ready.rollback'}):
                with self.subTest(changes=changes), self.assertRaises(WorkerLeaseError):
                    self.record(self.discovery.model_copy(update=changes))
        self.assertEqual(self.service.store.assignment(self.claimed.id).procedure_discoveries, self.originals)

    def test_scoped_assignment_lookup_is_fresh_exact_and_normal_ambiguity_is_unchanged(self):
        from unittest.mock import patch
        from codex_web.services.execution_workers import WorkerConflictError
        scope = self.full()
        current = self.service.store.assignment(self.claimed.id)
        # A large retained catalog must not be decoded just to verify one origin.
        def history(state):
            state.assignments.extend(current.model_copy(update={
                'id':'foreign-%04d'%index,'execution_id':'other-%04d'%index,'status':'cancelled',
                'lease':None,'organization_id':'foreign'}) for index in range(500))
            return state
        self.service.store.update(history)
        with read_only_reconciliation(scope), patch.object(self.service.store,'list_assignments',
                side_effect=AssertionError('full history read')):
            self.assertEqual(self.service.assignment_for_execution(scope.execution_id,actor=self.admin),current)
            with self.assertRaises(WorkerConflictError):
                self.service.assignment_for_execution('foreign-execution',actor=self.admin)
            with self.assertRaises(WorkerConflictError):
                self.service.assignment_for_execution(scope.execution_id,
                    actor=self.admin.model_copy(update={'workspace_id':'foreign'}))
        for changed in (replace(scope,fence=scope.fence+1),replace(scope,expires_at=0)):
            with read_only_reconciliation(changed),self.assertRaises(WorkerConflictError):
                self.service.assignment_for_execution(scope.execution_id,actor=self.admin)
        from codex_web.resources import RepositoryExecutionScope
        competing = current.model_copy(update={'id':'competing','repository_scope':RepositoryExecutionScope(
            organization_id=current.organization_id,workspace_id=current.workspace_id,project_id=current.project_id,
            writable_repository_ids=('different-repo',),source='explicit')})
        # Outside recovery, the original loader and its ambiguity checks remain.
        with patch.object(self.service.store,'list_assignments',return_value=[current,competing]) as loader:
            with self.assertRaisesRegex(WorkerConflictError,'disagree on repository authority'):
                self.service.assignment_for_execution(scope.execution_id,actor=self.admin)
            loader.assert_called_once()

    def test_replaced_lease_and_empty_bound_scope_refuse_even_with_current_marker(self):
        scope = self.full()
        for change in ({'worker_id':'different-worker'}, {'fence':scope.fence+1}):
            def replace_lease(state):
                state.assignments[0].lease = self.claimed.lease.model_copy(update=change)
                return state
            self.service.store.update(replace_lease)
            with read_only_reconciliation(scope), self.assertRaises(WorkerLeaseError):
                self.record(self.discovery)
        def restore(state):
            state.assignments[0].lease = self.claimed.lease
            return state
        self.service.store.update(restore)
        with read_only_reconciliation(replace(scope, resource_ids=())), self.assertRaises(WorkerLeaseError):
            self.record(self.discovery.model_copy(update={'resource_ids':()}))
        self.assertEqual(self.service.store.assignment(self.claimed.id).procedure_discoveries, self.originals)

    def test_serialized_marker_cannot_claim_an_early_callback_slot(self):
        scope = self.full()
        def retain_prefix(state):
            state.assignments[0].procedure_discoveries = self.originals[:1]
            return state
        self.service.store.update(retain_prefix)
        forged = self.discovery.model_copy(update={'supporting_sources': (scope.marker,)})
        before = self.service.store.assignment(self.claimed.id).procedure_discoveries
        with self.assertRaisesRegex(WorkerLeaseError, 'cannot supply recovery provenance'):
            self.record(forged)
        self.assertEqual(self.service.store.assignment(self.claimed.id).procedure_discoveries, before)

    def test_serialized_marker_does_not_create_admission_and_unknown_extras_fail_closed(self):
        scope = self.full()
        forged = self.discovery.model_copy(update={'supporting_sources':(scope.marker,)})
        with self.assertRaises(ProcedureHistoryExhaustedError):
            self.record(forged)
        with read_only_reconciliation(scope):
            with self.assertRaisesRegex(WorkerLeaseError, 'cannot supply recovery provenance'):
                self.record(forged)
        # An old/corrupted extra cannot be reinterpreted as a reservation.
        def corrupt(state):
            state.assignments[0].procedure_discoveries = (*self.originals,
                self.discovery.model_copy(update={'supporting_sources':(RECOVERY_PREFIX+'malformed',)}))
            return state
        self.service.store.update(corrupt)
        with read_only_reconciliation(scope):
            with self.assertRaisesRegex(ProcedureHistoryExhaustedError, 'unrecognized'):
                self.record(self.discovery)
