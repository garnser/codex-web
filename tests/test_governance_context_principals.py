"""Fresh canonical Governance observations; no application or external I/O."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_web.data_governance import ContextFilterRequest, GovernedDataCreate, DataClassification, GovernedDataLifecycle
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, MembershipRole, PrincipalKind
from codex_web.services.data_governance import DataGovernanceService, GovernanceNotFoundError
from codex_web.storage.data_governance import DataGovernanceStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class GovernanceContextPrincipalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = DataGovernanceService(DataGovernanceStore(
            SQLiteStateStore(Path(self.temp.name) / 'private.sqlite3')))
        self.admin = AuthenticationActor(identity_id='private-owner', principal_kind=PrincipalKind.HUMAN,
            organization_id='local', workspace_id='default', roles=(MembershipRole.ADMIN,), assurance=AuthenticationAssurance.MFA)
        self.actors = (self.admin, self.admin.model_copy(update={'identity_id':'private-requester'}),
            self.admin.model_copy(update={'identity_id':'private-worker', 'principal_kind':PrincipalKind.SERVICE}))
        self.record = self.service.register(GovernedDataCreate(object_type='artifact', object_id='fixture',
            category='artifact', classification='internal', reason='Private generic context'), actor=self.admin)
        self.payload = ContextFilterRequest(record_ids=(self.record.id,))

    def test_each_principal_same_results_with_one_selected_observation(self):
        expected = tuple(self.service.filter_context(self.payload, actor=a) for a in self.actors)
        with patch.object(self.service.store, 'context_state', wraps=self.service.store.context_state) as reads, \
                patch.object(self.service, '_filter_context_state', wraps=self.service._filter_context_state) as filters:
            actual = self.service.filter_context_for_actors(self.payload, actors=self.actors)
        self.assertEqual(actual, expected)
        self.assertEqual(reads.call_count, 1)
        self.assertEqual([call.kwargs['actor'] for call in filters.call_args_list], list(self.actors))

    def test_next_call_observes_current_classification_and_lifecycle(self):
        self.assertEqual(self.service.filter_context_for_actors(self.payload, actors=self.actors)[0].denied_record_ids, ())
        def restrict(state):
            state.records[0].classification = DataClassification.SECRET
            return state
        self.service.store.update(restrict)
        results = self.service.filter_context_for_actors(self.payload, actors=self.actors)
        self.assertTrue(all(r.denied_record_ids == (self.record.id,) for r in results))
        def deactivate(state):
            state.records[0].classification = DataClassification.INTERNAL
            state.records[0].lifecycle = GovernedDataLifecycle.DELETED
            return state
        self.service.store.update(deactivate)
        self.assertTrue(all(r.denied_record_ids == (self.record.id,) for r in
            self.service.filter_context_for_actors(self.payload, actors=self.actors)))

    def test_custom_filter_is_called_for_every_principal(self):
        original = self.service.filter_context
        with patch.object(self.service, 'filter_context', wraps=original) as custom, \
                patch.object(self.service.store, 'context_state', wraps=self.service.store.context_state) as reads:
            results = self.service.filter_context_for_actors(self.payload, actors=self.actors)
        self.assertEqual(custom.call_count, 3)
        self.assertEqual(reads.call_count, 3)
        self.assertTrue(all(r.allowed_record_ids == (self.record.id,) for r in results))

    def test_other_tenant_cannot_use_first_principals_observation(self):
        other = self.admin.model_copy(update={'identity_id':'foreign', 'workspace_id':'foreign'})
        with self.assertRaises(GovernanceNotFoundError):
            self.service.filter_context_for_actors(self.payload, actors=(self.admin, other))

    def test_backend_full_decoder_fallback_still_refuses_bad_document(self):
        self.service.store.store.update(self.service.store.namespace,
            lambda state:{**state, 'unexpected_current_header': True}, default={})
        with self.assertRaises(ValueError):
            self.service.filter_context_for_actors(self.payload, actors=self.actors)

    def test_no_principals_performs_no_read(self):
        with patch.object(self.service.store, 'context_state', side_effect=AssertionError('no read')):
            self.assertEqual(self.service.filter_context_for_actors(self.payload, actors=()), ())
