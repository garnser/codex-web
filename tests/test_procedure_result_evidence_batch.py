"""Canonical two-proof append transactions; no provider/runtime is involved."""
import sqlite3
import unittest
from types import SimpleNamespace
from pathlib import Path

from codex_web.models import WorkItemState
from codex_web.storage.runtime_state import ModelMapRepository
from unittest.mock import patch

from codex_web.artifact_evidence import ArtifactCreate, EvidenceCreate
from codex_web.services.artifact_evidence import ArtifactEvidenceConflictError, ArtifactNotFoundError
from tests import test_artifact_evidence as fixture


class ProcedureResultEvidenceBatchTests(unittest.TestCase):
    setUp = fixture.ArtifactEvidenceTests.setUp
    tearDown = fixture.ArtifactEvidenceTests.tearDown

    def specs(self, *, work=False):
        return tuple((EvidenceCreate(project_id='home',
            work_item_ref=self.work_item.ref if work else None,
            execution_id='same-original-execution', evidence_type=kind, result='pass',
            source='action-intent:same-original', external_id='same-original-receipt',
            observed_at=123.0, metadata={'receipt_id': 'same-original-receipt',
                'verification_id': 'same-original-verification' if index else None}),
            f'procedure-result:same-original-receipt:{name}')
            for index, (name, kind) in enumerate((('receipt', 'provider_receipt'),
                ('verification', 'artifact_verification'))))

    def snapshot(self):
        backend = self.artifact_store.store
        with backend._connect() as connection:
            return {namespace: connection.execute(
                'SELECT payload, updated_at FROM state_documents WHERE namespace = ?',
                (namespace,)).fetchone() for namespace in
                (self.artifact_store.namespace, self.governance.store.namespace)}

    def test_two_proofs_one_transaction_without_full_preloads(self):
        backend = self.artifact_store.store
        before = backend._keyed_mutation_metrics.snapshot()['count']
        original_get = backend.get
        reads = []
        def get(namespace):
            reads.append(namespace)
            return original_get(namespace)
        with patch.object(backend, 'get', get):
            self.assertTrue(self.service.can_batch_evidence_creation(self.specs()))
            proofs = self.service.create_evidence_batch(self.specs(), actor=self.producer)
        self.assertEqual(backend._keyed_mutation_metrics.snapshot()['count'] - before, 1)
        self.assertNotIn(self.artifact_store.namespace, reads)
        self.assertEqual(len({proof.id for proof in proofs}), 2)
        self.assertEqual(tuple(proof.evidence_type for proof in proofs),
                         ('provider_receipt', 'artifact_verification'))
        for proof in proofs:
            self.assertEqual(proof.source, 'action-intent:same-original')
            self.assertEqual(proof.execution_id, 'same-original-execution')
            self.assertEqual(proof.metadata['receipt_id'], 'same-original-receipt')
            self.assertIsNotNone(proof.governance_record_id)
        self.assertEqual(len(self.governance.store.load().records), 2)

    def test_both_existing_are_exact_reuse_without_domain_write(self):
        first = self.service.create_evidence_batch(self.specs(), actor=self.producer)
        before = self.snapshot()
        second = self.service.create_evidence_batch(self.specs(), actor=self.producer)
        self.assertEqual(second, first)
        self.assertEqual(self.snapshot(), before)

    def test_either_partial_original_is_reused_and_only_missing_proof_appended(self):
        for existing_index in (0, 1):
            with self.subTest(existing_index=existing_index):
                specs = tuple((payload.model_copy(update={'source': f'original-{existing_index}'}), key)
                              for payload, key in self.specs())
                payload, key = specs[existing_index]
                original = self.service.create_evidence(payload, actor=self.producer, idempotency_key=key)
                before = len(self.governance.store.load().records)
                proofs = self.service.create_evidence_batch(specs, actor=self.producer)
                self.assertEqual(proofs[existing_index], original)
                self.assertEqual(len(self.governance.store.load().records), before + 1)

    def test_second_payload_conflict_rolls_back_first_append_and_governance(self):
        specs = self.specs()
        payload, key = specs[1]
        self.service.create_evidence(payload.model_copy(update={'summary': 'different content'}),
                                     actor=self.producer, idempotency_key=key)
        before = self.snapshot()
        with self.assertRaises(ArtifactEvidenceConflictError):
            self.service.create_evidence_batch(specs, actor=self.producer)
        self.assertEqual(self.snapshot(), before)

    def test_missing_artifact_in_second_proof_rolls_back_both_domains(self):
        specs = list(self.specs())
        payload, key = specs[1]
        specs[1] = (payload.model_copy(update={'artifact_ids': ('artifact-missing',)}), key)
        before = self.snapshot()
        with self.assertRaises(ArtifactNotFoundError):
            self.service.create_evidence_batch(specs, actor=self.producer)
        self.assertEqual(self.snapshot(), before)

    def test_governance_failure_after_first_domain_upsert_rolls_back_all(self):
        backend = self.artifact_store.store
        namespace = self.governance.store.namespace
        with backend._connect() as connection:
            connection.execute("CREATE TRIGGER test_refuse_governance BEFORE INSERT ON state_documents "
                f"WHEN NEW.namespace = '{namespace}' BEGIN SELECT RAISE(ABORT, 'test refusal'); END")
        before = self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.create_evidence_batch(self.specs(), actor=self.producer)
        self.assertEqual(self.snapshot(), before)

    def test_derived_artifact_governance_is_preserved(self):
        artifact = self.service.create_artifact(ArtifactCreate(project_id='home',
            artifact_type='report', name='Original governed source'), actor=self.producer)
        specs = tuple((payload.model_copy(update={'artifact_ids': (artifact.id,)}), key)
                      for payload, key in self.specs())
        proofs = self.service.create_evidence_batch(specs, actor=self.producer)
        records = {row.id: row for row in self.governance.store.load().records}
        for proof in proofs:
            self.assertEqual(proof.artifact_ids, (artifact.id,))
            self.assertIn(artifact.governance_record_id, records[proof.governance_record_id].source_record_ids)

    def test_canonical_work_prefix_runs_once_per_payload_before_writer_begin(self):
        specs = self.specs(work=True)
        backend = self.artifact_store.store
        repository = ModelMapRepository(backend, namespace='work_item_states',
            legacy_path=Path(self.temp.name) / 'work.json', model=WorkItemState)
        repository.put(self.work_item.ref, self.work_item)
        self.service.work_item_host = SimpleNamespace(_load_work_item_states=repository.load)
        original_connect = backend._connect
        active = False
        begins = []
        work_reads = []
        def trace(statement):
            nonlocal active
            upper = statement.upper().strip()
            if upper.startswith('BEGIN IMMEDIATE'):
                active = True
                begins.append(True)
            elif upper.startswith(('COMMIT', 'ROLLBACK')):
                active = False
            elif upper.startswith('SELECT') and 'work_item_states' in statement:
                work_reads.append(active)
        def connect():
            connection = original_connect()
            connection.set_trace_callback(trace)
            return connection
        with patch.object(backend, '_connect', connect):
            self.assertTrue(self.service.can_batch_evidence_creation(specs))
            proofs = self.service.create_evidence_batch(specs, actor=self.producer)
        self.assertTrue(work_reads)
        self.assertFalse(any(work_reads))
        self.assertEqual(len(begins), 1)
        self.assertFalse(active)
        self.assertEqual(tuple(proof.work_item_ref for proof in proofs), (self.work_item.ref,) * 2)

    def test_custom_work_host_keeps_original_prefix_write_interleaving(self):
        specs = self.specs(work=True)
        original = self.host._load_work_item_states
        backend = self.artifact_store.store
        counts = []
        before = backend._keyed_mutation_metrics.snapshot()['count']
        def load():
            counts.append(backend._keyed_mutation_metrics.snapshot()['count'] - before)
            return original()
        with patch.object(self.host, '_load_work_item_states', load):
            self.assertFalse(self.service.can_batch_evidence_creation(specs))
            proofs = self.service.create_evidence_batch(specs, actor=self.producer)
        self.assertEqual(counts, [0, 1])
        self.assertEqual(len(proofs), 2)

    def test_intercepting_work_host_keeps_original_interleaving_without_probe_callbacks(self):
        backend = self.artifact_store.store
        repository = ModelMapRepository(backend, namespace='work_item_states',
            legacy_path=Path(self.temp.name) / 'intercept-work.json', model=WorkItemState)
        repository.put(self.work_item.ref, self.work_item)
        before = backend._keyed_mutation_metrics.snapshot()['count']
        calls = []
        def load():
            calls.append(backend._keyed_mutation_metrics.snapshot()['count'] - before)
            return repository.load()
        class InterceptingHost:
            def __getattribute__(self, name):
                if name == '_load_work_item_states':
                    return load
                return object.__getattribute__(self, name)
        host = InterceptingHost()
        # Static inspection alone sees this genuine binding. Actual access
        # would substitute a custom callback, so the host must stay legacy.
        host._load_work_item_states = repository.load
        self.service.work_item_host = host
        specs = self.specs(work=True)
        self.assertFalse(self.service.can_batch_evidence_creation(specs))
        self.assertFalse(self.service.can_batch_evidence_creation(work_item_ref=self.work_item.ref))
        self.assertEqual(calls, [])
        self.service.create_evidence_batch(specs, actor=self.producer)
        self.assertEqual(calls, [0, 1])

    def test_custom_public_creation_retains_legacy_signatures_and_single_calls(self):
        original = self.service.create_evidence
        calls = []
        def create(payload, *, actor, idempotency_key=None):
            calls.append((payload, idempotency_key))
            return original(payload, actor=actor, idempotency_key=idempotency_key)
        specs = self.specs()
        with patch.object(self.service, 'create_evidence', create):
            self.assertFalse(self.service.can_batch_evidence_creation(specs))
            proofs = self.service.create_evidence_batch(specs, actor=self.producer)
        self.assertEqual(calls, list(specs))
        self.assertEqual(len(proofs), 2)

    def test_custom_backend_owner_uses_ordinary_calls_without_replay(self):
        backend = self.artifact_store.store
        original = backend.record_mutate_many
        calls = []
        def mutate(domains, callback):
            calls.append(domains)
            return original(domains, callback)
        with patch.object(backend, 'record_mutate_many', mutate):
            self.assertFalse(self.service.can_batch_evidence_creation(self.specs()))
            proofs = self.service.create_evidence_batch(self.specs(), actor=self.producer)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(proofs), 2)

    def test_invalid_pair_never_enters_transaction(self):
        before = self.snapshot()
        with self.assertRaises(ArtifactEvidenceConflictError):
            self.service.create_evidence_batch((), actor=self.producer)
        self.assertEqual(self.snapshot(), before)
