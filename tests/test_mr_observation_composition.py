"""Real governed MR publication/start with bounded transaction elapsed clocks."""
import time
import unittest
from urllib.parse import quote
from unittest.mock import patch

from codex_web.artifact_evidence import ArtifactEvidenceState, EvidenceCreate
from codex_web.data_governance import DataGovernanceState
from codex_web.mr_continuation_input import METHOD
from codex_web.services.artifact_evidence import ArtifactEvidenceConflictError
from tests import test_governed_mr_procedure_runs as fixtures


class MRObservationCompositionTests(fixtures.GovernedMRProcedureRunTests):
    async def test_one_complete_domain_export_preserves_all_provenance_and_start(self):
        counts = {'artifact': 0, 'governance': 0}
        original_artifact = ArtifactEvidenceState.model_dump
        original_governance = DataGovernanceState.model_dump
        def artifact(state, *args, **kwargs):
            counts['artifact'] += 1
            return original_artifact(state, *args, **kwargs)
        def governance(state, *args, **kwargs):
            counts['governance'] += 1
            return original_governance(state, *args, **kwargs)
        with patch.object(ArtifactEvidenceState, 'model_dump', new=artifact), \
             patch.object(DataGovernanceState, 'model_dump', new=governance):
            result = await self.publish()
        self.assertEqual(counts, {'artifact': 1, 'governance': 1})
        state = self.evidence.store.load()
        records = self.evidence.governance.store.load().records
        ids = {p['id'] for p in result['evidence'].values()}
        self.assertEqual(set(result['item']['evidence_ids']), ids)
        item = next(a for a in state.artifacts if a.id == result['procedure_input']['id'])
        self.assertIsNotNone(item.content)
        source_records = {e.governance_record_id for e in state.evidence if e.id in ids}
        derived = next(r for r in records if r.id == item.governance_record_id)
        self.assertTrue(source_records.issubset(set(derived.source_record_ids)))
        await self.start_snapshot(item.id)
        self.assertIsNotNone(self.workers.store.assignment(self.assignment.id).procedure_checkpoint)

    async def test_expiry_during_final_domain_dump_rolls_back_all_domains(self):
        before = self.evidence.store.load().model_dump(mode='json')
        governed = self.evidence.governance.store.load().model_dump(mode='json')
        offset = [0]
        original = DataGovernanceState.model_dump
        def dump(state, *args, **kwargs):
            value = original(state, *args, **kwargs)
            offset[0] = 31
            return value
        with patch.object(self.service, '_clock', side_effect=lambda: time.time()+offset[0]), \
             patch.object(DataGovernanceState, 'model_dump', new=dump):
            status, _, body = await self.post({**self.request, 'method': METHOD, 'request_id': 'b'*32})
        self.assertEqual(status, 409, body)
        self.assertEqual(body['error']['code'], 'mr_observation_head_changed_or_expired')
        self.assertEqual(self.evidence.store.load().model_dump(mode='json'), before)
        self.assertEqual(self.evidence.governance.store.load().model_dump(mode='json'), governed)
        self.assertIsNone(self.workers.store.assignment(self.assignment.id).procedure_checkpoint)

    async def test_postcommit_expiry_refuses_and_reconciles_original_without_read_replay(self):
        store = self.evidence.store.store
        original = store.record_mutate_many
        offset = [0]
        committed = []
        def mutate(domains, updater):
            def update(rows):
                changes = updater(rows)
                if self.evidence.store.namespace in changes and self.evidence.governance.store.namespace in changes:
                    committed.append(True)
                    offset[0] = 31
                return changes
            return original(domains, update)
        with patch.object(store, 'record_mutate_many', side_effect=mutate), \
             patch.object(self.service, '_clock', side_effect=lambda: time.time()+offset[0]):
            status, _, body = await self.post({**self.request, 'method': METHOD, 'request_id': 'b'*32})
            self.assertEqual(status, 409, body)
            error = body['error']
            self.assertEqual(error['code'], 'mr_observation_head_changed_or_expired')
            proof = error['mr_observation']
            self.assertFalse(proof['retryable'])
            self.assertTrue(proof['reconcile_without_replay'])
            self.assertEqual(len(committed), 1)
            state = self.evidence.store.load()
            self.assertEqual(len(state.verifications), 1)
            self.assertIsNone(self.workers.store.assignment(self.assignment.id).procedure_checkpoint)
            calls = len(self.observed_calls)
            status, _, found = await self._request('GET', '/api/verifications?method='+METHOD+
                '&source='+quote(proof['source'], safe=''))
            self.assertEqual(status, 200, found)
            self.assertEqual(found['outcome'], 'unique')
            self.assertFalse(found['original']['observation_fresh'])
            self.assertEqual(found['original']['item']['id'], state.verifications[0].id)
            self.assertEqual(len(self.observed_calls), calls)

    async def test_composed_evidence_state_requires_canonical_commit_owner(self):
        state = self.evidence.store.load()
        before = state.model_dump(mode='json')
        payload = EvidenceCreate(project_id='project-a', evidence_type='review', result='pass')
        for supplied, commit in [(state, None), ({}, lambda creation: None)]:
            with self.assertRaises(ArtifactEvidenceConflictError):
                self.evidence.create_evidence(payload, actor=self.admin, _state=supplied, _commit=commit)
        self.assertEqual(self.evidence.store.load().model_dump(mode='json'), before)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(MRObservationCompositionTests(name)
        for name in MRObservationCompositionTests.__dict__ if name.startswith('test_'))
