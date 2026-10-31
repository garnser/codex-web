"""Canonical guarded-readiness result projection, with physical owner counters.

Only provider HTTP is synthetic. Counters are confined to the original result
projection invocation; catalog getters/decoders and their eligibility identities
are untouched. This small generic-assignment fixture does not reproduce native
custody/Role fan-out, live catalog sizes, or a live timing result.
"""
from collections import Counter
from contextlib import ExitStack
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex_web.services import procedure_execution as procedure_module
from codex_web.services.procedure_execution import ProcedureExecutionService
from tests import test_mr_guarded_readonly_budget as guarded
from tests import test_post_input_admission_metadata_reads as native


class ProcedureResultProjectionCompositionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = guarded.GuardedReadinessCompositionTests.asyncSetUp
    asyncTearDown = guarded.GuardedReadinessCompositionTests.asyncTearDown

    async def test_readiness_projection_uses_one_governed_creation_for_both_proofs(self):
        """Existing entry point: old two-write implementation fails by count only."""
        await self.exercise_projection(custom_work_host=False)

    async def test_custom_work_callback_keeps_legacy_prefix_write_order(self):
        await self.exercise_projection(custom_work_host=True)

    async def exercise_projection(self, *, custom_work_host):
        f = self.f
        # Application's Work host exposes this canonical bound ModelMap reader.
        # Keep the real Work state and its exact store/model/namespace ownership.
        f.evidence.work_item_host = SimpleNamespace(_load_work_item_states=f.items.load)
        f.configure(180)
        await guarded.publish_guarded_skill(f)
        published = await f.publish_v2()
        started = await f.start_snapshot(published['procedure_input']['id'])
        backend = f.sqlite
        self.assertIs(f.evidence.store.store, backend)
        self.assertIs(f.evidence.governance.store.store, backend)
        self.assertIs(f.runner._provider_result.__func__, ProcedureExecutionService._provider_result)

        namespaces = ('identity_state', 'resource_catalog', 'definition_registry',
                      'artifact_evidence', 'data_governance', 'canonical_configuration_registry')
        gets = Counter()
        before_first_write = Counter()
        prefix_gets = Counter()
        full_document_write_inputs = Counter()
        creation_writes = []
        captured = {}
        original_get = backend.get
        original_connect = backend._connect
        original_project = f.runner._provider_result_owned
        original_payload = procedure_module.EvidenceCreate
        payload_count = 0
        work_callback_write_counts = []
        measurement_active = False

        if custom_work_host:
            def load_custom_work():
                if measurement_active:
                    work_callback_write_counts.append(len(creation_writes))
                return f.items.load()
            f.evidence.work_item_host = SimpleNamespace(_load_work_item_states=load_custom_work)

        def eligibility():
            return {
                'identity': f.identity.can_observe_reads(),
                'identity_store': f.identity.store.can_observe_reads(),
                'resources': f.resources.can_observe_reads(),
                'resource_store': f.resources.store.can_observe_reads(),
                'definitions': f.registry.can_observe_reads(),
                'definition_store': f.registry.store.can_observe_reads(),
                'skill_definitions': f.skills.can_observe_definition_reads(),
            }

        batch_check = getattr(f.evidence, 'can_batch_evidence_creation', None)
        def batch_eligibility():
            return batch_check(work_item_ref=started.work_item_ref) if callable(batch_check) else None
        batch_eligible = batch_eligibility()
        if batch_eligible is not None and not custom_work_host:
            self.assertTrue(batch_eligible)
        before_eligibility = eligibility()
        self.assertTrue(all(before_eligibility.values()), before_eligibility)

        def construct_original_payload(*args, **kwargs):
            # A pure constructor boundary separates metadata prefix from Evidence
            # validation/writes without replacing any canonical reader method.
            nonlocal payload_count
            if payload_count == 0:
                prefix_gets.update(gets)
            payload_count += 1
            return original_payload(*args, **kwargs)

        def count_get(namespace, *args, **kwargs):
            if namespace in namespaces:
                gets[namespace] += 1
                if not creation_writes:
                    before_first_write[namespace] += 1
            return original_get(namespace, *args, **kwargs)

        def connect_with_closed_transaction_counts():
            connection = original_connect()
            domains = set()
            reads = Counter()
            in_write = False

            def trace(statement):
                # SQLite's private trace is reduced immediately to fixed counters.
                # Never retain/print SQL, expanded arguments, IDs or payload text.
                nonlocal in_write
                prefix = ' '.join(statement[:200].split())
                if prefix.startswith('BEGIN IMMEDIATE'):
                    in_write = True
                elif in_write and prefix.startswith('SELECT payload FROM state_documents'):
                    for namespace in namespaces:
                        if "namespace = '" + namespace + "'" in prefix:
                            reads[namespace] += 1
                elif in_write and prefix.startswith('INSERT INTO state_documents('):
                    for namespace in ('artifact_evidence', 'data_governance'):
                        if "VALUES ('" + namespace + "'," in prefix:
                            domains.add(namespace)
                elif prefix == 'COMMIT':
                    if domains == {'artifact_evidence', 'data_governance'}:
                        creation_writes.append(tuple(sorted(domains)))
                        full_document_write_inputs.update(reads)
                    in_write = False
                elif prefix == 'ROLLBACK':
                    in_write = False
            connection.set_trace_callback(trace)
            return connection

        async def observe_original_projection(assignment, step, intent, receipt, history, actor):
            nonlocal measurement_active
            self.assertFalse(captured, 'Projection must be observed once, never replayed')
            self.assertEqual(intent.status, 'succeeded')
            self.assertEqual(receipt.outcome, 'completed')
            self.assertIsNotNone(receipt.result)
            self.assertTrue(history.verifications[-1].verified)
            captured.update(intent_id=intent.id, receipt=receipt.model_dump(mode='json'),
                            verification=history.verifications[-1].model_dump(mode='json'),
                            http_before=len(f.observed_calls))
            with ExitStack() as stack:
                stack.enter_context(patch.object(backend, 'get', side_effect=count_get))
                stack.enter_context(patch.object(backend, '_connect', side_effect=connect_with_closed_transaction_counts))
                stack.enter_context(patch.object(procedure_module, 'EvidenceCreate',
                                                side_effect=construct_original_payload))
                self.assertEqual(eligibility(), before_eligibility)
                if batch_eligible is not None:
                    self.assertEqual(batch_eligibility(), batch_eligible)
                measurement_active = True
                try:
                    result = await original_project(assignment, step, intent, receipt, history, actor)
                finally:
                    measurement_active = False
                self.assertEqual(eligibility(), before_eligibility)
                if batch_eligible is not None:
                    self.assertEqual(batch_eligibility(), batch_eligible)
            captured['http_after'] = len(f.observed_calls)
            return result

        # This orchestration wrapper does not replace a service/store getter,
        # decoder, provider hook, or any stock metadata capability predicate.
        with patch.object(f.runner, '_provider_result_owned', side_effect=observe_original_projection):
            advanced = await f.advance_run(started.procedure_checkpoint.id)

        self.assertTrue(captured, advanced.procedure_checkpoint.failure_code)
        state = advanced.procedure_checkpoint.steps['observe']
        self.assertEqual(state.status, 'succeeded', advanced.procedure_checkpoint.failure_code)
        self.assertEqual(set(state.evidence), {'receipt', 'verification'})
        history = f.intents.store.load(intent_id=captured['intent_id'])
        self.assertEqual(len(history.receipts), 1)
        self.assertEqual(history.receipts[0].model_dump(mode='json'), captured['receipt'])
        self.assertEqual(history.verifications[-1].model_dump(mode='json'), captured['verification'])
        self.assertEqual(captured['http_before'], captured['http_after'])
        proofs = {name: f.evidence.get_evidence(eid, actor=f.admin)
                  for name, eid in state.evidence.items()}
        self.assertNotEqual(proofs['receipt'].id, proofs['verification'].id)
        self.assertEqual(proofs['receipt'].evidence_type, 'provider_receipt')
        self.assertEqual(proofs['verification'].evidence_type, 'artifact_verification')
        for name, proof in proofs.items():
            self.assertEqual(proof.source, 'action-intent:' + captured['intent_id'])
            self.assertEqual(proof.metadata['receipt_id'], captured['receipt']['id'])
            self.assertEqual(proof.metadata['verification_method'], 'code-host-readiness-observation.v2')
            expected = f.evidence._idempotent_evidence_id(
                f.admin, proof.source, 'procedure-result:' + captured['receipt']['id'] + ':' + name)
            self.assertEqual(proof.id, expected)
        self.assertEqual(proofs['verification'].metadata['verification_id'], captured['verification']['id'])
        output = json.loads(f.content(state.output_snapshot_id))
        self.assertEqual((output['readiness'], output['independent_review']), ('blocked', 'fail'))
        calls = len(f.observed_calls)
        completed = await f.advance_run(started.procedure_checkpoint.id)
        self.assertEqual(completed.procedure_checkpoint.status, 'completed')
        self.assertEqual(len(f.observed_calls), calls)
        f.runner.models.invoke.assert_not_called()

        # The message is a closed diagnostic: fixed namespace labels and counts,
        # no payloads, provider outputs, credentials, IDs, SQL or timing target.
        diagnostic = {
            'scope': 'one_original_guarded_readiness_result_projection',
            'full_catalog_gets': {key: gets[key] for key in namespaces},
            'full_catalog_gets_before_first_evidence_payload':
                {key: prefix_gets[key] for key in namespaces},
            'full_catalog_gets_during_evidence_projection':
                {key: gets[key] - prefix_gets[key] for key in namespaces},
            'full_catalog_gets_before_first_creation_write':
                {key: before_first_write[key] for key in namespaces},
            'governed_creation_transactions': len(creation_writes),
            'full_document_inputs_to_creation_transactions':
                {key: full_document_write_inputs[key] for key in namespaces},
            'provider_HTTP_during_projection': captured['http_after'] - captured['http_before'],
            'native_custody_fanout_reproduced': False,
            'batch_owner_eligibility_unchanged': batch_eligible,
            'stock_metadata_read_eligibility_unchanged': before_eligibility,
        }
        if custom_work_host:
            self.assertEqual(work_callback_write_counts, [0, 1])
        self.assertEqual(len(creation_writes), 2 if custom_work_host else 1,
                         json.dumps(diagnostic, sort_keys=True))


class NativeResultProcedureMetadataTests(unittest.IsolatedAsyncioTestCase):
    """Real two-Skill/three-action custody; only the metadata slice is invoked."""

    async def asyncSetUp(self):
        self.fixture = native.PostInputAdmissionMetadataReadTests(
            'test_existing_current_admission_shares_post_input_catalog_reads')
        self.addAsyncCleanup(self.fixture.close_fixture)
        await self.fixture.asyncSetUp()
        self.f = self.fixture.f
        self.assignment = self.f.workers.store.assignment(self.f.assignment.id)
        self.procedure = self.f.runner._procedure(self.assignment, self.f.admin)
        self.step = self.procedure.steps[0]

    async def test_stock_native_validation_uses_three_fresh_reads_and_closes_views(self):
        from codex_web.services import definitions, identity, resources
        from codex_web.services.definitions import DefinitionError
        from codex_web.services.identity import IdentityError
        from codex_web.services.resources import ResourceCatalogError

        f = self.f
        counts = Counter()
        original_get = f.sqlite.get
        captured = []

        def count_get(namespace, *args, **kwargs):
            if namespace in ('definition_registry', 'identity_state', 'resource_catalog'):
                counts[namespace] += 1
            return original_get(namespace, *args, **kwargs)

        def capture_constructor(original):
            def construct(view, owner, *args, **kwargs):
                original(view, owner, *args, **kwargs)
                captured.append((owner, view))
            return construct

        calls = len(f.observed_calls)
        with ExitStack() as stack:
            stack.enter_context(patch.object(f.sqlite, 'get', side_effect=count_get))
            # Track actual view instances at construction, preserving every
            # captured reader/getter/decoder identity and ordinary class type.
            for cls in (definitions._DefinitionReadObservation,
                        identity._IdentityReadObservation, resources._ResourceReadObservation):
                stack.enter_context(patch.object(cls, '__init__',
                    capture_constructor(cls.__init__)))
            observed = f.runner._result_procedure(self.assignment, self.step, f.admin)
        self.assertEqual(observed, self.procedure)
        self.assertEqual(dict(counts), {
            'definition_registry': 1, 'identity_state': 1, 'resource_catalog': 1})
        self.assertEqual(len(captured), 3)
        for owner, view in captured:
            with self.assertRaises((DefinitionError, IdentityError, ResourceCatalogError)):
                owner.require_read_observation(view)
        self.assertEqual(len(f.observed_calls), calls)
        f.runner.models.invoke.assert_not_called()

    async def test_custom_old_signature_and_scope_callback_keep_legacy_once(self):
        f = self.f
        called = []

        def old_procedure(assignment, actor):
            called.append((assignment.id, actor.identity_id))
            return self.procedure

        with patch.object(f.runner, '_procedure', side_effect=old_procedure):
            self.assertIs(f.runner._result_procedure(self.assignment, self.step, f.admin), self.procedure)
        self.assertEqual(called, [(self.assignment.id, f.admin.identity_id)])
        called.clear()
        # This helper was never reached by legacy _procedure. A custom
        # old-signature helper must not become an accidental new callback.
        with patch.object(f.runner, '_metadata_read_options',
                          side_effect=AssertionError('Legacy procedure must not call this helper')):
            self.assertEqual(f.runner._result_procedure(self.assignment, self.step, f.admin), self.procedure)

        def custom_scope(record):
            called.append(record.record_id)
            raise RuntimeError('Synthetic legacy scope callback refusal')

        with patch.object(f.skills, 'procedure_scope_actor', custom_scope):
            with self.assertRaisesRegex(RuntimeError, 'Synthetic legacy scope callback refusal'):
                f.runner._result_procedure(self.assignment, self.step, f.admin)
        self.assertEqual(called, [f.pin.record_id])

    async def test_next_result_validation_observes_revocation_then_restoration(self):
        from codex_web.definitions import DefinitionLifecycle
        from codex_web.services.skills import SkillResolutionFailure

        f = self.f
        f.runner._result_procedure(self.assignment, self.step, f.admin)
        original = f.registry.get_record(f.pin.record_id)
        f.registry.store.update(lambda rows: [row.model_copy(update={
            'lifecycle': DefinitionLifecycle.QUARANTINED})
            if row.record_id == original.record_id else row for row in rows])
        with self.assertRaises(SkillResolutionFailure) as caught:
            f.runner._result_procedure(self.assignment, self.step, f.admin)
        self.assertEqual(caught.exception.code, 'procedure_quarantined')
        f.registry.store.update(lambda rows: [original if row.record_id == original.record_id
                                             else row for row in rows])
        self.assertEqual(f.runner._result_procedure(self.assignment, self.step, f.admin), self.procedure)
