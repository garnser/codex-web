"""Real governed input owners: fresh observations, never cached authority."""
from collections import Counter
from contextlib import contextmanager
import json
import unittest
from unittest.mock import patch

from codex_web.artifact_content import ArtifactContentState
from codex_web.artifact_evidence import EvidenceLifecycle
from codex_web.services.procedure_execution import ProcedureExecutionError
from codex_web.services import procedure_execution as procedure_owner
from tests import test_mr_observation_policy as policy_fixture
from tests import test_mr_guarded_readonly_budget as guarded_fixture


class GovernedInputReadBatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = policy_fixture.MRObservationPolicyTests(
            'test_v2_real_publish_start_readonly_receipt_evidence_after_47_seconds')
        await self.f.asyncSetUp()
        self.f.broker_response_timeout = 150  # Private observation, no canonical extension.
        self.f.configure(180)
        await guarded_fixture.publish_guarded_skill(self.f)
        self.published = await self.f.publish_v2()
        self.artifact_id = self.published['procedure_input']['id']
        self.started = await self.f.start_snapshot(self.artifact_id)
        self.assignment = self.f.workers.store.assignment(self.started.id)
        self.procedure = self.f.runner._procedure(self.assignment, self.f.admin)

    async def asyncTearDown(self):
        await self.f.asyncTearDown()
        self.f.doCleanups()

    def inputs(self):
        return self.f.runner._inputs(self.assignment, self.f.admin, self.procedure)

    @contextmanager
    def physical_catalog_reads(self):
        """Count actual SQLite statements; do not override canonical getters."""
        reads = Counter()
        connect = self.f.sqlite._connect
        namespaces = ('artifact_evidence', self.f.evidence.governance.store.namespace)

        def observe(statement):
            if statement.lstrip().upper().startswith(('SELECT', 'WITH')) and 'state_documents' in statement:
                for namespace in namespaces:
                    if "'" + namespace + "'" in statement:
                        reads[namespace] += 1

        def traced_connection():
            connection = connect()
            connection.set_trace_callback(observe)
            return connection

        with patch.object(self.f.sqlite, '_connect', traced_connection):
            yield reads

    def mutate_at_content_end(self, mutation, *, stream_number=1):
        """An actual managed stream changes metadata before final input admission."""
        original = self.f.evidence.content.open
        calls = []
        opened_streams = []

        def opened(*args, **kwargs):
            stream = original(*args, **kwargs)
            opened_streams.append(True)
            number = len(opened_streams)

            def changed_stream():
                yield from stream
                if number == stream_number:
                    mutation()
                    calls.append(True)

            return changed_stream()

        return patch.object(self.f.evidence.content, 'open', opened), calls

    async def test_actual_input_uses_three_physical_reads_without_qa_or_provider_effect(self):
        artifact = self.f.evidence.get_artifact(self.artifact_id, actor=self.f.admin)
        verification_id = artifact.metadata['verification_id']

        def nonlexical_proof_order(state):
            verification = next(v for v in state.verifications if v.id == verification_id)
            verification.evidence_ids = tuple(sorted(verification.evidence_ids, reverse=True))
            return state

        self.f.evidence.store.update(nonlexical_proof_order)
        verification = self.f.evidence.get_verification(verification_id, actor=self.f.admin,
            work_item_ref=self.assignment.work_item_ref)
        self.assertEqual(len(set(verification.evidence_ids)), 3)
        self.assertNotEqual(verification.evidence_ids, tuple(sorted(verification.evidence_ids)))
        self.assertEqual(artifact.metadata['source_evidence_ids'],
                         json.dumps(sorted(verification.evidence_ids), separators=(',', ':')))
        provider_calls = len(self.f.observed_calls)
        before = self.f.workers.store.assignment(self.assignment.id).model_dump(mode='json')
        with self.physical_catalog_reads() as reads:
            values = self.inputs()
        self.assertEqual(reads['artifact_evidence'], 2)
        self.assertEqual(reads[self.f.evidence.governance.store.namespace], 1)
        self.assertEqual(values['expected_head_sha'], self.f.request['expected_head_sha'])
        self.assertEqual(next(e for e in self.published['evidence'].values()
                              if e['metadata']['observation_kind'] == 'independent_review')['result'], 'fail')
        self.assertEqual(len(self.f.observed_calls), provider_calls)
        self.assertEqual(self.f.workers.store.assignment(self.assignment.id).model_dump(mode='json'), before)
        self.f.runner.models.invoke.assert_not_called()

    async def test_distinct_current_witness_uses_its_fresh_bundle_with_four_reads(self):
        fresh = await self.f.publish_v2('e')
        witness_id = fresh['procedure_input']['id']
        self.assertNotEqual(witness_id, self.artifact_id)
        original_artifact = self.f.evidence.get_artifact(self.artifact_id, actor=self.f.admin)
        witness = self.f.evidence.get_artifact(witness_id, actor=self.f.admin)
        self.assertNotEqual(witness.source, original_artifact.source)
        self.assertNotEqual(witness.metadata['verification_id'], original_artifact.metadata['verification_id'])
        self.assertEqual(self.f.content(witness_id), self.f.content(self.artifact_id))
        before = self.f.workers.store.assignment(self.assignment.id).procedure_checkpoint.model_dump(mode='json')
        calls = len(self.f.observed_calls)
        token = procedure_owner._current_input_witness.set((self.assignment.id,
            self.f.runner.run_id(self.assignment.procedure_checkpoint), witness_id))
        try:
            with self.physical_catalog_reads() as reads:
                values = self.inputs()
            self.assertEqual(reads['artifact_evidence'], 3)
            self.assertEqual(reads[self.f.evidence.governance.store.namespace], 1)
            self.assertEqual(values['expected_head_sha'], self.f.request['expected_head_sha'])

            def change_witness(state):
                next(a for a in state.artifacts if a.id == witness_id).metadata['requester_identity_id'] = 'foreign'
                return state

            changed, mutations = self.mutate_at_content_end(
                lambda: self.f.evidence.store.update(change_witness), stream_number=2)
            with changed, self.assertRaises((ProcedureExecutionError, ValueError)):
                self.inputs()
            self.assertEqual(mutations, [True])
        finally:
            procedure_owner._current_input_witness.reset(token)
        self.assertEqual(len(self.f.observed_calls), calls)
        self.assertEqual(self.f.workers.store.assignment(self.assignment.id).procedure_checkpoint.model_dump(mode='json'),
                         before)
        self.assertEqual(before['input_snapshot_id'], self.artifact_id)

    async def test_post_content_artifact_proof_and_pointer_changes_refuse(self):
        original = self.f.evidence.store.load().model_copy(deep=True)

        def alter_artifact(state):
            next(a for a in state.artifacts if a.id == self.artifact_id).metadata['requester_identity_id'] = 'foreign'
            return state

        def alter_proof(state):
            ids = {e['id'] for e in self.published['evidence'].values()}
            next(e for e in state.evidence if e.id in ids).lifecycle = EvidenceLifecycle.INVALIDATED
            return state

        def alter_pointer(state):
            artifact = next(a for a in state.artifacts if a.id == self.artifact_id)
            artifact.content = artifact.content.model_copy(update={'state': ArtifactContentState.TOMBSTONED})
            return state

        for mutation in (alter_artifact, alter_proof, alter_pointer):
            with self.subTest(mutation=mutation.__name__):
                self.f.evidence.store.update(lambda state: original.model_copy(deep=True))
                guard, calls = self.mutate_at_content_end(lambda: self.f.evidence.store.update(mutation))
                with guard, self.assertRaises((ProcedureExecutionError, ValueError)):
                    self.inputs()
                self.assertEqual(calls, [True])

    async def test_next_callback_rechecks_governance_policy_and_proofs(self):
        self.inputs()
        artifact = self.f.evidence.get_artifact(self.artifact_id, actor=self.f.admin)
        original = self.f.evidence.governance.store.load().model_copy(deep=True)

        def restrict(state):
            for record in state.records:
                if record.id == artifact.governance_record_id:
                    record.deny_model_context = True
            return state

        self.f.evidence.governance.store.update(restrict)
        with self.assertRaises((ProcedureExecutionError, ValueError)):
            self.inputs()
        self.f.evidence.governance.store.update(lambda state: original.model_copy(deep=True))
        self.inputs()
        from codex_web.configuration import ConfigurationResetRequest
        self.f.configuration.reset_override(ConfigurationResetRequest(
            key='code_host.mr_observation_window_seconds', scope_type='deployment',
            actor=self.f.admin.identity_id))
        with self.assertRaises((ProcedureExecutionError, ValueError)):
            self.inputs()

    async def test_first_scoped_artifact_evidence_and_verification_ambiguity(self):
        source = self.f.evidence.store.load().model_copy(deep=True)
        artifact = next(a for a in source.artifacts if a.id == self.artifact_id)
        verification_id = artifact.metadata['verification_id']
        verification = next(v for v in source.verifications if v.id == verification_id)
        ids = {e['id'] for e in self.published['evidence'].values()}

        def interleave(state):
            foreign_artifact = artifact.model_copy(deep=True, update={'organization_id': 'foreign'})
            state.artifacts.insert(0, foreign_artifact)
            poisoned = artifact.model_copy(deep=True)
            poisoned.metadata['requester_identity_id'] = 'foreign'
            state.artifacts.append(poisoned)
            for proof in [e for e in source.evidence if e.id in ids]:
                state.evidence.insert(0, proof.model_copy(deep=True, update={'organization_id': 'foreign'}))
                state.evidence.append(proof.model_copy(deep=True, update={'lifecycle': EvidenceLifecycle.INVALIDATED}))
            state.verifications.insert(0, verification.model_copy(deep=True, update={'organization_id': 'foreign'}))
            return state

        self.f.evidence.store.update(interleave)
        self.inputs()  # Original point readers choose the first matching tenant row.

        self.assertIsNotNone(self.assignment.work_item_ref)
        self.f.evidence.store.update(lambda state: state.model_copy(update={
            'verifications': [*state.verifications, verification.model_copy(deep=True,
                update={'work_item_ref': 'group/other#99'})]}))
        self.inputs()  # Another Work's duplicate is not a second scoped match.

        def duplicate(state):
            state.verifications.append(verification.model_copy(deep=True))
            return state

        self.f.evidence.store.update(duplicate)
        with self.assertRaises((ProcedureExecutionError, ValueError)):
            self.inputs()  # Verification deliberately preserves ALL scoped matches.

    async def test_custom_getters_keep_their_real_owner_and_no_internal_state(self):
        for name in ('get_artifact', 'get_evidence', 'get_verification'):
            original = getattr(self.f.evidence, name)
            calls = []

            def custom(*args, **kwargs):
                calls.append(kwargs.get('state'))
                return original(*args, **kwargs)

            with self.subTest(getter=name), patch.object(self.f.evidence, name, custom):
                self.inputs()
            self.assertTrue(calls)
            self.assertTrue(all(state is None for state in calls))

    async def test_custom_content_owner_refusal_is_not_bypassed(self):
        def denied(*args, **kwargs):
            raise RuntimeError('private custom content refusal')

        with patch.object(self.f.evidence, 'open_artifact_content', denied):
            with self.assertRaisesRegex(RuntimeError, 'private custom content refusal'):
                self.inputs()

    async def test_custom_governance_filter_is_not_bypassed(self):
        governance = self.f.evidence.governance
        original = governance.filter_context
        actors = []

        def current(payload, *, actor):
            actors.append(actor.identity_id)
            return original(payload, actor=actor)

        with patch.object(governance, 'filter_context', current):
            self.inputs()
        self.assertEqual(len(actors), 3)
        self.assertIn(self.f.admin.identity_id, actors)
        self.assertIn(self.f.worker_actor.identity_id, actors)

    async def test_custom_existing_governance_batch_owner_is_not_bypassed(self):
        governance = self.f.evidence.governance
        calls = []

        def denied(payload, *, actors):
            calls.append(tuple(actor.identity_id for actor in actors))
            raise RuntimeError('private custom existing governance batch refusal')

        with patch.object(governance, 'filter_context_for_actors', denied):
            with self.assertRaisesRegex(RuntimeError, 'private custom existing governance batch refusal'):
                self.inputs()
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]), 3)

    async def test_custom_store_decoder_and_selector_refusals_are_not_bypassed(self):
        for name in ('_decode', 'artifact_state', 'evidence_states', 'verification_state'):
            calls = []

            def denied(*args, **kwargs):
                calls.append(True)
                raise RuntimeError('private custom canonical store refusal')

            with self.subTest(owner=name), patch.object(self.f.evidence.store, name, denied):
                with self.assertRaisesRegex(RuntimeError, 'private custom canonical store refusal'):
                    self.inputs()
            self.assertTrue(calls)

    async def test_legacy_backend_and_header_keep_full_decoder_refusals(self):
        backend = self.f.evidence.store.store
        namespace = self.f.evidence.store.namespace
        with patch.object(backend, 'supports_artifact_input_envelope', False, create=True):
            self.inputs()
            raw = backend.get(namespace)
            raw['schema_version'] = '1.4'
            backend.put(namespace, raw)
            self.inputs()  # The existing ordinary migration remains the owner.
            raw['schema_version'] = '99.0'
            backend.put(namespace, raw)
            with self.assertRaises(ValueError):
                self.inputs()

    async def test_expiry_between_current_callbacks_does_not_refresh_input(self):
        values = self.inputs()
        expiry = next(iter(self.published['evidence'].values()))['metadata']['observation_expires_at']
        with patch.object(self.f.runner, 'clock', return_value=expiry):
            with self.assertRaises((ProcedureExecutionError, ValueError)):
                self.inputs()
        self.assertEqual(self.inputs(), values)
        self.assertEqual(self.f.workers.store.assignment(self.assignment.id).procedure_checkpoint.input_snapshot_id,
                         self.artifact_id)

