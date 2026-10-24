from __future__ import annotations

import os
import math
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codex_web.storage.postgres_state import PostgresStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.state_store import state_record_storage_key
from tests import test_autonomy_append_scaling as fixtures


class CollectionRangeContract:
    def test_context_record_opt_in_preserves_complete_first_scoped_record_and_default(self):
        service, selected, actor = self._governance_context()
        rows = [selected.model_copy(update={"organization_id": "foreign", "deny_model_context": True}),
            selected, selected.model_copy(update={"deny_model_context": True})]
        for version in ('0.0', '1.0', '1.1'):
            with self.subTest(version=version):
                self.backend.put('data_governance', {'schema_version': version,
                    'records': [row.model_dump(mode='json') for row in rows], 'requests': [], 'events': []})
                expected = service.get_record(selected.id, actor)
                with patch.object(service.store, 'load', side_effect=AssertionError('full context read')):
                    self.assertEqual(service.get_record(selected.id, actor, context_only=True), expected)
                with patch.object(service.store, 'context_state', side_effect=AssertionError('default changed')):
                    self.assertEqual(service.get_record(selected.id, actor), expected)

    def test_context_record_missing_foreign_malformed_and_fallback_contract(self):
        from codex_web.services.data_governance import GovernanceNotFoundError
        service, selected, actor = self._governance_context()
        raw = {'schema_version': '1.1', 'records': [selected.model_dump(mode='json')],
            'requests': [], 'events': []}
        for records in ([], [{**raw['records'][0], 'organization_id': 'foreign'}]):
            self.backend.put('data_governance', {**raw, 'records': records})
            for context in (False, True):
                with self.assertRaises(GovernanceNotFoundError):
                    service.get_record(selected.id, actor, context_only=context)
        for changed in ({**raw, 'schema_version': 'unknown'}, {**raw, 'events': 'invalid'},
                {**raw, 'unexpected': True},
                {**raw, 'records': [raw['records'][0], {**raw['records'][0], 'classification': 'invalid'}]}):
            self.backend.put('data_governance', changed)
            for context in (False, True):
                with self.assertRaises(ValueError):
                    service.get_record(selected.id, actor, context_only=context)
        # An optional backend without the selected-envelope API retains the
        # original full decoder, including unrelated malformed-record refusal.
        for records, valid in ((raw['records'], True),
                ([*raw['records'], {**raw['records'][0], 'id': 'foreign',
                    'organization_id': 'foreign', 'classification': 'invalid'}], False)):
            self.backend.put('data_governance', {**raw, 'records': records})
            with patch.object(self.backend, 'document_array_items_envelope', None):
                if valid:
                    self.assertEqual(service.get_record(selected.id, actor, context_only=True), selected)
                else:
                    with self.assertRaises(ValueError):
                        service.get_record(selected.id, actor, context_only=True)
        # Optimized context reads retain the existing scoped-reader contract;
        # foreign malformed records never become selected local records.
        with patch.object(service.store, 'load', side_effect=AssertionError('full context read')):
            self.assertEqual(service.get_record(selected.id, actor, context_only=True), selected)

    def _put_document_text(self, namespace, raw):
        self.backend.put(namespace, {})
        postgres = isinstance(self.backend, PostgresStateStore)
        table, slot = ('codex_state_documents', '%s') if postgres else ('state_documents', '?')
        with self.backend._connection() as connection:
            connection.execute(f'UPDATE {table} SET payload = {slot} WHERE namespace = {slot}', (raw, namespace))
            connection.commit()

    def test_batch_envelope_preserves_original_numeric_unicode_and_stored_order(self):
        raw = r'''{"schema_version":"1.1","records":[
            {"id":"selected","organization_id":"foreign","workspace_id":"default"},
            {"id":"selected","organization_id":"local","workspace_id":"default",
             "value":{"scientific":1e0,"signedzero":-0.0,"bigint":BIGINT,"unicode":"\u96ea",
                      "duplicate":1,"duplicate":2}},
            {"id":"selected","organization_id":"local","workspace_id":"default","value":"later"}],
            "requests":[],"events":[{"unrelated":"retained"}]}'''.replace('BIGINT', str(2**100 + 1))
        self._put_document_text('batch-envelope', raw)
        with patch.object(self.backend, 'get', side_effect=AssertionError('whole document transfer')):
            actual = self.backend.document_array_items_envelope('batch-envelope', 'records',
                ('selected','selected'), organization_id='local', workspace_id='default',
                collection_names=('records','requests','events'))
        self.assertEqual(actual['records'], json.loads(raw)['records'][1:])
        value = actual['records'][0]['value']
        self.assertIs(type(value['scientific']), float)
        self.assertEqual(math.copysign(1, value['signedzero']), -1)
        self.assertEqual(value['bigint'], 2**100 + 1)
        self.assertEqual(value['unicode'], '雪')
        self.assertEqual(value['duplicate'], 2)
        self.assertNotIn('events', actual)

    def test_batch_first_per_id_keeps_first_scoped_member_and_stored_order(self):
        rows = [
            {'id': 'one', 'organization_id': 'foreign', 'workspace_id': 'default'},
            {'id': 'two', 'organization_id': 'local', 'workspace_id': 'default', 'value': 'first-two'},
            {'id': 'one', 'organization_id': 'local', 'workspace_id': 'default', 'value': 'first-one'},
            {'id': 'two', 'organization_id': 'local', 'workspace_id': 'default', 'value': 'later-two'},
            {'id': 'one', 'organization_id': 'local', 'workspace_id': 'default', 'value': 'later-one'}]
        self.backend.put('batch-first', {'schema_version': '1.1', 'records': rows})
        scope = {'organization_id': 'local', 'workspace_id': 'default',
                 'collection_names': ('records', 'requests', 'events')}
        actual = self.backend.document_array_items_envelope('batch-first', 'records',
            ('one', 'two', 'one', 'missing'), first_per_id=True, **scope)
        self.assertEqual(actual, {'schema_version': '1.1', 'records': rows[1:3]})
        self.assertEqual(self.backend.document_array_items_envelope('batch-first', 'records',
            ('one', 'two'), **scope)['records'], rows[1:])

    def test_batch_envelope_legacy_numeric_schema_and_json_fallback_preserve_transaction(self):
        service, selected, actor = self._governance_context()
        raw = json.dumps({'schema_version': 1.0, 'records': [selected.model_dump(mode='json')],
            'requests': [], 'events': []}).replace('"schema_version": 1.0', '"schema_version": 1e0')
        self._put_document_text('data_governance', raw)
        self.assertEqual(service.store.context_state(('selected',), organization_id='local',
            workspace_id='default'), service.store.load())
        for case, value in (('nonfinite', float('nan')), ('nul', 'before\0after'),
                            ('surrogate', 'before\ud800after'), ('overflow', 'overflow-number')):
            with self.subTest(case=case):
                row = {'id': 'selected', 'organization_id': 'local', 'workspace_id': 'default',
                    'value': value}
                raw = json.dumps({'schema_version': '1.1', 'records': [row], 'requests': [], 'events': []})
                if case == 'overflow':
                    raw = raw.replace('"overflow-number"', '1e1000000')
                self._put_document_text('batch-envelope', raw)
                self.backend.put('batch-sentinel', {'ok': False})
                def update(current):
                    actual = self.backend.document_array_items_envelope('batch-envelope', 'records',
                        ('selected',), organization_id='local', workspace_id='default',
                        collection_names=('records','requests','events'))['records'][0]['value']
                    if case == 'nonfinite':
                        self.assertTrue(math.isnan(actual))
                    elif case == 'overflow':
                        self.assertEqual(actual, float('inf'))
                    else:
                        self.assertEqual(actual, value)
                    return {'ok': True}
                self.backend.update('batch-sentinel', update, default={})
                self.assertEqual(self.backend.get('batch-sentinel'), {'ok': True})

    def _governance_context(self):
        from codex_web.data_governance import GovernedDataRecord, DataCategory, DataClassification
        from codex_web.storage.data_governance import DataGovernanceStore
        from codex_web.services.data_governance import DataGovernanceService
        from codex_web.identity import AuthenticationActor, PrincipalKind, AuthenticationAssurance
        record = GovernedDataRecord(id='selected', organization_id='local', workspace_id='default',
            object_type='memory', object_id='selected', category=DataCategory.MEMORY,
            requested_classification=DataClassification.INTERNAL,
            classification=DataClassification.INTERNAL, created_by='fixture')
        actor = AuthenticationActor(identity_id='fixture', principal_kind=PrincipalKind.HUMAN,
            organization_id='local', workspace_id='default', assurance=AuthenticationAssurance.LOCAL_TRUSTED)
        return DataGovernanceService(DataGovernanceStore(self.backend)), record, actor

    def test_governance_context_selected_snapshot_preserves_decisions_without_history_transfer(self):
        from codex_web.data_governance import ContextFilterRequest, GovernedDataLifecycle, DataClassification
        service, selected, actor = self._governance_context()
        denied = selected.model_copy(update={'id': 'denied', 'deny_model_context': True})
        deleted = selected.model_copy(update={'id': 'deleted', 'lifecycle': GovernedDataLifecycle.DELETED})
        secret = selected.model_copy(update={'id': 'secret', 'classification': DataClassification.SECRET})
        rows = [selected, denied, deleted, secret]
        raw = {'schema_version': '1.1', 'records': [r.model_dump(mode='json') for r in rows],
               'events': [], 'requests': []}
        self.backend.put('data_governance', raw)
        request = ContextFilterRequest(record_ids=('deleted','selected','denied','secret','selected'))
        with patch.object(service.store, 'context_state', return_value=service.store.load()):
            expected = service.filter_context(request, actor=actor)
        # Valid retained event history is deliberately large, and irrelevant to
        # filtering. The selected complete records remain the canonical truth.
        raw['events'] = [{'organization_id': 'local', 'workspace_id': 'default',
            'actor_id': 'fixture', 'operation': 'register', 'outcome': 'allowed',
            'reason_code': 'retained ' * 1000} for _ in range(150)]
        self.backend.put('data_governance', raw)
        with patch.object(service.store, 'load', side_effect=AssertionError('whole governance decode')), \
             patch.object(self.backend, 'get', side_effect=AssertionError('whole document transfer')):
            actual = service.filter_context(request, actor=actor)
        self.assertEqual(actual, expected)
        self.assertEqual(actual.allowed_record_ids, ('selected', 'selected'))

    def test_governance_context_refuses_missing_foreign_malformed_and_unknown_envelopes(self):
        from codex_web.data_governance import ContextFilterRequest
        from codex_web.services.data_governance import GovernanceNotFoundError
        service, selected, actor = self._governance_context()
        raw = {'schema_version': '1.1', 'records': [selected.model_dump(mode='json')], 'events': [], 'requests': []}
        for record_id, records in (('missing', raw['records']), ('selected',
                [{**raw['records'][0], 'organization_id': 'foreign'}])):
            self.backend.put('data_governance', {**raw, 'records': records})
            with self.assertRaises(GovernanceNotFoundError):
                service.filter_context(ContextFilterRequest(record_ids=(record_id,)), actor=actor)
        for changed in ({**raw, 'schema_version': 'unknown'}, {**raw, 'events': 'wrong'},
                        {**raw, 'unexpected': True}, {**raw, 'records': [{**raw['records'][0], 'classification': 'unknown'}]}):
            self.backend.put('data_governance', changed)
            with self.assertRaises(ValueError):
                service.filter_context(ContextFilterRequest(record_ids=('selected',)), actor=actor)

    def test_governance_context_preserves_first_scoped_stored_duplicate_and_legacy_empty(self):
        from codex_web.data_governance import ContextFilterRequest
        service, selected, actor = self._governance_context()
        rows = [{**selected.model_dump(mode='json'), 'organization_id': 'foreign', 'deny_model_context': True},
                selected.model_dump(mode='json'),
                {**selected.model_dump(mode='json'), 'deny_model_context': True}]
        for version in ('0.0', '1.0', '1.1'):
            raw = {'schema_version': version, 'records': rows, 'events': [], 'requests': []}
            self.backend.put('data_governance', raw)
            request = ContextFilterRequest(record_ids=('selected','selected'))
            with patch.object(service.store, 'context_state', return_value=service.store.load()):
                expected = service.filter_context(request, actor=actor)
            self.assertEqual(service.filter_context(request, actor=actor), expected)
            self.assertEqual(expected.allowed_record_ids, ('selected','selected'))
            self.assertEqual(service.filter_context(ContextFilterRequest(record_ids=()), actor=actor).decisions, ())
        self.backend.put('data_governance', {**raw, 'schema_version': 'unknown'})
        with self.assertRaises(ValueError):
            service.filter_context(ContextFilterRequest(record_ids=()), actor=actor)

    def _evidence_point(self, raw, *, item_id="selected", organization_id="local", workspace_id="default"):
        from codex_web.artifact_evidence import Evidence, EvidenceType, EvidenceResult
        selected = Evidence(id=item_id, organization_id=organization_id, workspace_id=workspace_id,
            evidence_type=EvidenceType.POLICY_EVALUATION, result=EvidenceResult.PASS,
            producer_identity_id="fixture")
        self.backend.put("artifact_evidence", raw(selected))
        from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
        return ArtifactEvidenceStore(self.backend), selected

    def test_evidence_point_read_is_scoped_and_does_not_decode_unrelated_catalog(self):
        from codex_web.artifact_evidence import EvidenceLifecycle
        from codex_web.services.artifact_evidence import ArtifactEvidenceService
        from types import SimpleNamespace
        def raw(selected):
            selected.lifecycle = EvidenceLifecycle.INVALIDATED
            selected.invalidated_at = 7
            selected.retention_expires_at = 8
            selected.metadata = {"exact": True, "bigint": 2**100 + 1, "signedzero": -0.0}
            foreign = selected.model_copy(update={"organization_id": "foreign"})
            unrelated = [selected.model_copy(update={"id": f"unrelated-{i}",
                "summary": "ordinary retained history " * 200}).model_dump(mode="json") for i in range(100)]
            return {"schema_version": "1.3", "evidence": [foreign.model_dump(mode="json"),
                selected.model_dump(mode="json"), *unrelated], "artifacts": [], "verifications": []}
        store, selected = self._evidence_point(raw)
        with patch.object(store, "load", side_effect=AssertionError("whole Evidence catalog read")),                 patch.object(self.backend, "get", side_effect=AssertionError("whole document transfer")):
            actual = ArtifactEvidenceService(store).get_evidence("selected",
                actor=SimpleNamespace(organization_id="local", workspace_id="default"))
        self.assertEqual(actual, selected)
        self.assertIs(actual.metadata["exact"], True)
        self.assertEqual(actual.metadata["bigint"], 2**100 + 1)
        self.assertEqual(math.copysign(1, actual.metadata["signedzero"]), -1)
        self.assertEqual(actual.lifecycle, EvidenceLifecycle.INVALIDATED)
        self.assertEqual(actual.retention_expires_at, 8)

    def test_evidence_point_preserves_legacy_schema_and_refuses_selected_malformed_body(self):
        from pydantic import ValidationError
        def raw(selected):
            item = selected.model_dump(mode="json")
            item.pop("resource_ids")
            return {"schema_version": "1.0", "evidence": [item]}
        store, selected = self._evidence_point(raw)
        self.assertEqual(store.evidence_state("selected", organization_id="local", workspace_id="default").evidence,
                         [selected])
        for changed in ({"schema_version": "unsupported", "evidence": []},
                        {"schema_version": "1.3", "evidence": "not-an-array"},
                        {"schema_version": "1.3", "unexpected": True},
                        {"schema_version": "1.3", "evidence": [{**selected.model_dump(mode="json"),
                            "producer_identity_id": None}]}):
            with self.subTest(shape=list(changed)):
                self.backend.put("artifact_evidence", changed)
                with self.assertRaises((ValueError, ValidationError)):
                    store.evidence_state("selected", organization_id="local", workspace_id="default")

    def test_evidence_point_legacy_json_fallback_keeps_caller_transaction_valid(self):
        def raw(selected):
            selected.metadata = {"nonfinite": float("nan"), "nul": "before\0after",
                                 "surrogate": "before\ud800after"}
            return {"schema_version": "1.3", "evidence": [selected.model_dump(mode="json")]}
        store, selected = self._evidence_point(raw)
        self.backend.put("transaction-sentinel", {"ok": False})
        def update(current):
            actual = store.evidence_state("selected", organization_id="local", workspace_id="default").evidence[0]
            self.assertTrue(math.isnan(actual.metadata["nonfinite"]))
            self.assertEqual(actual.metadata["nul"], "before\0after")
            self.assertEqual(actual.metadata["surrogate"], "before\ud800after")
            return {"ok": True}
        self.backend.update("transaction-sentinel", update, default={})
        self.assertEqual(self.backend.get("transaction-sentinel"), {"ok": True})

    def test_evidence_point_retains_existing_keyed_document_backend_fallback(self):
        store, selected = self._evidence_point(lambda selected: {"schema_version": "1.3",
            "evidence": [selected.model_dump(mode="json")]})
        raw = self.backend.get("artifact_evidence")
        self.backend.delete("artifact_evidence")
        self.backend.record_replace("artifact_evidence", raw)
        self.assertEqual(store.evidence_state("selected", organization_id="local",
            workspace_id="default").evidence, [selected])

    def test_evidence_point_does_not_retry_unrelated_storage_failure(self):
        class TransportFailure(RuntimeError):
            sqlstate = "08006"
        failure = (sqlite3.OperationalError("database is locked")
                   if isinstance(self.backend, SQLiteStateStore) else TransportFailure())
        connection = "_connection" if isinstance(self.backend, SQLiteStateStore) else "_read_connection"
        with patch.object(self.backend, connection, side_effect=failure),                 patch.object(self.backend, "get") as fallback:
            with self.assertRaises(type(failure)):
                self.backend.document_array_item_envelope("artifact_evidence", "evidence", "selected",
                    organization_id="local", workspace_id="default",
                    collection_names=("artifacts", "evidence", "verifications"))
            fallback.assert_not_called()

    def test_evidence_point_wrong_scope_missing_and_legacy_backend(self):
        from codex_web.services.artifact_evidence import ArtifactEvidenceService, EvidenceNotFoundError
        from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
        from types import SimpleNamespace
        store, selected = self._evidence_point(lambda selected: {"schema_version": "1.3",
            "evidence": [selected.model_dump(mode="json")]})
        actor = SimpleNamespace(organization_id="local", workspace_id="other")
        with self.assertRaises(EvidenceNotFoundError):
            ArtifactEvidenceService(store).get_evidence("selected", actor=actor)
        with self.assertRaises(EvidenceNotFoundError):
            ArtifactEvidenceService(store).get_evidence("missing",
                actor=SimpleNamespace(organization_id="local", workspace_id="default"))
        older_backend = SimpleNamespace(get=self.backend.get)
        self.assertEqual(ArtifactEvidenceStore(older_backend).evidence_state("selected",
            organization_id="local", workspace_id="default").evidence, [selected])

    def test_projection_does_not_retry_unrelated_storage_failure(self):
        class TransportFailure(RuntimeError):
            sqlstate = '08006'
        failure = (sqlite3.OperationalError('database is locked')
                   if isinstance(self.backend, SQLiteStateStore) else TransportFailure())
        if isinstance(self.backend, SQLiteStateStore) and sqlite3.sqlite_version_info < (3, 38, 0):
            with patch.object(self.backend, 'record_page', side_effect=failure) as reader:
                with self.assertRaises(type(failure)):
                    self.backend.record_page_projected('scope', fields=('selected',))
                reader.assert_called_once()
            return
        with patch.object(self.backend, '_record_page', side_effect=failure) as selected:
            with patch.object(self.backend, 'record_page') as fallback:
                with self.assertRaises(type(failure)):
                    self.backend.record_page_projected('scope', fields=('selected',))
                selected.assert_called_once()
                fallback.assert_not_called()

    def test_projection_preserves_legacy_nonfinite_values_and_escaped_nul(self):
        self.backend.record_replace('legacy-json', {
            'a': {'selected': float('nan'), 'ignored': float('inf')},
            'b': {'selected': float('inf')},
            'c': {'selected': float('-inf')},
            'd': {'selected': 'before\0after'},
            'e': float('nan'),
            'f': {'selected': 'before\ud800after'},
            'g': {'selected': 2**100 + 1},
            'h': {'selected': -0.0},
        })
        result = {}; after = None
        while True:
            page, after = self.backend.record_page_projected('legacy-json',
                fields=('selected',), after=after, limit=2)
            result.update(page)
            if after is None:
                break
        self.assertTrue(math.isnan(result['a']['selected']))
        self.assertNotIn('ignored', result['a'])
        self.assertEqual(result['b'], {'selected': float('inf')})
        self.assertEqual(result['c'], {'selected': float('-inf')})
        self.assertEqual(result['d'], {'selected': 'before\0after'})
        self.assertTrue(math.isnan(result['e']))
        self.assertEqual(result['f'], {'selected': 'before\ud800after'})
        self.assertIs(type(result['g']['selected']), int)
        self.assertEqual(result['g']['selected'], 2**100 + 1)
        self.assertEqual(math.copysign(1.0, result['h']['selected']), -1.0)

    def test_projection_fallback_does_not_abort_owning_transaction(self):
        self.backend.record_replace('legacy-json', {'key': {'selected': float('inf')}})
        self.backend.record_replace('mutation', {'counter': 0})
        def update(rows):
            page, cursor = self.backend.record_page_projected('legacy-json', fields=('selected',))
            self.assertEqual(page, {'key': {'selected': float('inf')}})
            self.assertIsNone(cursor)
            return {'mutation': {'counter': rows['mutation']['counter'] + 1}}
        self.backend.record_mutate_many({'mutation': ('counter',), 'legacy-json': ('key',)}, update)
        self.assertEqual(self.backend.record_get('mutation', 'counter'), 1)

    def test_projected_page_preserves_json_types_missing_null_and_exact_cursor_scope(self):
        namespace = 'selected/雪:%'
        records = {
            'a': {'owner': 'dana', 'handoff': {'status': 'accepted'}, 'enabled': True,
                  'disabled': False, 'ignored': 'large'},
            'b': {'owner': None, 'handoff': None},
            'c': {},
            'd': [1, 2],
            'e': 3,
            'f': 'hello',
            'g': 'null',
            'h': '3',
            'i': True,
            'j': False,
            'k': None,
        }
        self.backend.record_replace(namespace, records)
        self.backend.record_replace(namespace + 'neighbor', {'foreign': {'owner': 'quinn'}})
        result = {}; after = None
        while True:
            page, after = self.backend.record_page_projected(namespace,
                fields=('owner', 'handoff', 'enabled', 'disabled'), after=after, limit=2)
            result.update(page)
            if after is None:
                break
        self.assertEqual(result, {'a': {'owner': 'dana', 'handoff': {'status': 'accepted'},
            'enabled': True, 'disabled': False},
            'b': {'owner': None, 'handoff': None}, 'c': {}, 'd': [1, 2], 'e': 3,
            'f': 'hello', 'g': 'null', 'h': '3', 'i': True, 'j': False, 'k': None})
        self.assertIs(result['a']['enabled'], True)
        self.assertIs(result['a']['disabled'], False)
        self.assertIs(result['i'], True)
        self.assertIs(result['j'], False)
        self.assertIsNone(result['k'])
        self.assertEqual(self.backend.record_items(namespace), records)
        for fields in ((), ('owner', 'owner'), ('x.y',), ('x; DROP TABLE state_documents',),
                ('x' * 65,), tuple('x'+str(i) for i in range(33)), ({},)):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.backend.record_page_projected('not-created', fields=fields)
        self.assertFalse(self.backend.record_collection_exists('not-created'))

    def test_cross_collection_bounded_transaction_is_atomic_and_preserves_unselected_rows(self):
        self.backend.record_replace('scope', {'owner': {'fence': 1}, 'unrelated': {'keep': True}})
        self.backend.record_replace('outbox', {'intent': {'attempt': 0}, 'foreign': {'keep': True}})
        def admit(current):
            self.assertEqual(current['scope'], {'owner': {'fence': 1}})
            self.assertEqual(current['outbox'], {'intent': {'attempt': 0}})
            return {'outbox': {'intent': {'attempt': 1}, 'receipt': {'intent': 'intent'}}}
        self.backend.record_mutate_many({'scope': ('owner',), 'outbox': ('intent',)}, admit)
        self.assertEqual(self.backend.record_get('scope', 'unrelated'), {'keep': True})
        self.assertEqual(self.backend.record_get('outbox', 'foreign'), {'keep': True})
        self.assertEqual(self.backend.record_get('outbox', 'intent'), {'attempt': 1})
        before = self.backend.record_items('outbox')
        def abort(current):
            current['outbox']['intent']['attempt'] = 99
            raise ValueError('stale scope')
        with self.assertRaisesRegex(ValueError, 'stale scope'):
            self.backend.record_mutate_many({'scope': ('owner',), 'outbox': ('intent',)}, abort)
        self.assertEqual(self.backend.record_items('outbox'), before)
        with self.assertRaises(TypeError):
            self.backend.record_mutate_many({'scope': ('owner',)}, lambda current: {'outside': {'grant': True}})
        self.assertIsNone(self.backend.record_get('outside', 'grant'))

    def test_batch_collection_probe_is_fresh_and_handles_encoded_names(self):
        names = ("worker/雪:%", "missing", "legacy", "worker/雪:%neighbor")
        self.backend.record_replace(names[0], {})
        self.backend.put("legacy", {"old": "document"})
        expected = {name: name == names[0] for name in names}
        self.assertEqual(self.backend.record_collections_exist(names + names[:1]), expected)
        self.backend.record_replace("missing", {"new": 1})
        self.backend.delete(names[0])
        expected.update({names[0]: False, "missing": True})
        self.assertEqual(self.backend.record_collections_exist(names), expected)
        self.assertEqual(self.backend.record_collections_exist(()), {})
        with self.assertRaises(ValueError):
            self.backend.record_collections_exist(("missing",) * 1001)

    def test_encoded_namespace_and_unicode_key_neighbors_are_isolated(self):
        target = 'tenant/usage:%?雪'
        self.backend.record_replace(target, {'a/%?': 1, '雪': 2, '\U0001f680': 3})
        self.backend.record_replace(target + 'neighbor', {'x': 4})
        self.backend.record_replace('tenant', {'y': 5})
        self.assertEqual(self.backend.record_items(target), {'a/%?': 1, '雪': 2, '\U0001f680': 3})
        self.assertEqual(self.backend.record_count(target), 3)
        page, after = self.backend.record_page(target, limit=2)
        rest, _ = self.backend.record_page(target, after=after, limit=2)
        self.assertEqual({**page, **rest}, self.backend.record_items(target))
        self.assertTrue(self.backend.delete(target))
        self.assertEqual(self.backend.record_items(target + 'neighbor'), {'x': 4})
        self.assertEqual(self.backend.record_items('tenant'), {'y': 5})

    def test_replacing_keyed_mapping_with_scalar_removes_only_its_collection(self):
        self.backend.record_replace('usage', {'a': 1})
        self.backend.record_replace('usage:neighbor', {'b': 2})
        self.backend.put('usage', ['legacy'])
        self.assertFalse(self.backend.record_collection_exists('usage'))
        self.assertEqual(self.backend.get('usage'), ['legacy'])
        self.assertEqual(self.backend.record_items('usage:neighbor'), {'b': 2})

    def test_replacement_and_cross_collection_mutation_preserve_neighbors(self):
        target = 'execution_workers.workers'
        self.backend.record_replace(target, {'worker-original': {'fence': 1}, 'worker-stale': 0})
        self.backend.record_replace(target + '.neighbor', {'foreign': 7})
        self.backend.record_replace(target, {'worker-current': {'fence': 2}})
        self.assertIsNone(self.backend.record_get(target, 'worker-stale'))
        self.assertEqual(self.backend.record_get_many(target, ('worker-current', 'worker-stale')),
                         {'worker-current': {'fence': 2}})
        self.backend.update_many({target: {}}, lambda rows: {
            target: {**rows[target], 'worker-appended': {'fence': 3}}})
        self.assertEqual(self.backend.record_count(target), 2)
        self.assertEqual(self.backend.record_items(target + '.neighbor'), {'foreign': 7})
        self.assertEqual(self.backend.documents()[target], self.backend.record_items(target))
        self.backend.delete(target)
        self.assertNotIn(target, self.backend.documents())
        self.assertEqual(self.backend.record_items(target + '.neighbor'), {'foreign': 7})


class SQLiteCollectionRangeTests(CollectionRangeContract, unittest.TestCase):
    def test_evidence_point_python_allocation_does_not_scale_with_unrelated_bodies(self):
        import tracemalloc
        from types import SimpleNamespace
        from codex_web.services.artifact_evidence import ArtifactEvidenceService
        def raw(selected):
            return {"schema_version": "1.3", "evidence": [selected.model_dump(mode="json"), *[
                selected.model_copy(update={"id": f"retained-{i}", "summary": "ordinary metadata " * 300}).model_dump(mode="json")
                for i in range(200)]]}
        store, selected = self._evidence_point(raw)
        def measured(call):
            tracemalloc.start()
            try:
                result = call()
                return result, tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
        entire, full_peak = measured(store.load)
        self.assertEqual(len(entire.evidence), 201)
        del entire
        item, point_peak = measured(lambda: ArtifactEvidenceService(store).get_evidence("selected",
            actor=SimpleNamespace(organization_id="local", workspace_id="default")))
        self.assertEqual(item, selected)
        self.allocation_observation = {"full_catalog_peak_bytes": full_peak, "point_read_peak_bytes": point_peak,
                                       "synthetic_records": 201}
        self.assertLess(point_peak * 8, full_peak,
                        "The point lookup must not materialize unrelated Evidence bodies")

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.backend = SQLiteStateStore(Path(directory.name) / 'state.db')

    def test_older_sqlite_uses_raw_page_without_json_token_operator(self):
        self.backend.record_replace('legacy', {'a': {'selected': 2**100 + 1, 'ignored': True}})
        with patch('codex_web.storage.sqlite_state.sqlite3.sqlite_version_info', (3, 37, 0)):
            with patch.object(self.backend, '_record_page', wraps=self.backend._record_page) as reader:
                result, cursor = self.backend.record_page_projected('legacy', fields=('selected',))
                self.assertEqual(result, {'a': {'selected': 2**100 + 1}})
                self.assertIsNone(cursor)
                self.assertNotIn('fields', reader.call_args.kwargs)

    def test_batch_probe_uses_one_query_and_connection(self):
        from contextlib import contextmanager
        self.backend.record_replace("workers", {})
        queries = []
        original = self.backend._connection
        @contextmanager
        def tracing():
            with original() as connection:
                connection.set_trace_callback(queries.append)
                yield connection
        with unittest.mock.patch.object(self.backend, "_connection", tracing):
            self.backend.record_collections_exist(("workers", "assignments", "events", "enrollments"))
        self.assertEqual(len(queries), 1, queries)

    def test_tracked_revision_reads_do_not_scan_history(self):
        from contextlib import contextmanager
        self.backend.record_replace('usage', {str(i): i for i in range(5000)})
        queries = []
        original = self.backend._connection
        @contextmanager
        def tracing():
            with original() as connection:
                connection.set_trace_callback(queries.append)
                yield connection
        with unittest.mock.patch.object(self.backend, '_connection', tracing):
            self.backend.namespace_revision('usage')
        self.assertFalse(any('MAX(updated_at)' in query for query in queries), queries)


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'PostgreSQL test DSN required')
class PostgresCollectionRangeTests(CollectionRangeContract, unittest.TestCase):
    collation = 'C'

    def setUp(self):
        from psycopg import sql

        fixtures.PostgresAutonomyAppendTests.setUp(self)
        # Each fixture owns a fresh schema. Exercise the same store with both
        # byte-ordered and locale-ordered primary keys, including existing tables.
        with self.backend._connection() as connection:
            connection.execute(sql.SQL('ALTER TABLE codex_state_documents ALTER COLUMN namespace '
                'TYPE TEXT COLLATE {}').format(sql.Identifier(self.collation)))

    def test_batch_envelope_does_not_reparse_whole_history_for_each_predicate(self):
        from contextlib import contextmanager
        row = {'id': 'selected', 'organization_id': 'local', 'workspace_id': 'default'}
        self.backend.put('batch-parse-cost', {'schema_version': '1.1', 'records': [row],
            'requests': [], 'events': [{'retained': 'history ' * 1000} for _ in range(150)]})
        with self.backend._connection() as connection:
            connection.execute('CREATE TEMP TABLE batch_parse_calls(kind text)')
            for kind in ('json','jsonb'):
                connection.execute(f'''CREATE FUNCTION pg_temp.count_{kind}(text) RETURNS {kind}
                    LANGUAGE plpgsql AS $$ BEGIN
                    INSERT INTO pg_temp.batch_parse_calls VALUES ('{kind}');
                    RETURN $1::{kind}; END $$''')
            class TracedCursor:
                def __init__(self, cursor): self.cursor = cursor
                def __enter__(self): self.cursor.__enter__(); return self
                def __exit__(self, *args): return self.cursor.__exit__(*args)
                def __getattr__(self, name): return getattr(self.cursor, name)
                def execute(self, query, *args, **kwargs):
                    # Instrument the real server-side envelope conversion, not
                    # rows/fields returned or the canonical selection predicates.
                    query = query.replace('payload::jsonb', 'pg_temp.count_jsonb(payload)')
                    query = query.replace('payload::json', 'pg_temp.count_json(payload)')
                    return self.cursor.execute(query, *args, **kwargs)
            class TracedConnection:
                def cursor(self, *args, **kwargs): return TracedCursor(connection.cursor(*args, **kwargs))
                def transaction(self): return connection.transaction()
            @contextmanager
            def tracing(): yield TracedConnection()
            with patch.object(self.backend, '_read_connection', tracing):
                actual = self.backend.document_array_items_envelope('batch-parse-cost', 'records',
                    ('selected',), organization_id='local', workspace_id='default',
                    collection_names=('records','requests','events'))
            counts = dict(connection.execute('SELECT kind,count(*) FROM pg_temp.batch_parse_calls GROUP BY kind').fetchall())
        self.assertEqual(actual, {'schema_version': '1.1', 'records': [row]})
        self.assertLessEqual(sum(counts.values()), 2,
            'Envelope predicates must share one binary parse and one original-record pass')
        self.assertEqual(counts, {'jsonb': 1, 'json': 1})

    def test_real_collection_read_plan_uses_namespace_range_index(self):
        self.backend.record_replace('unrelated', {str(i): i for i in range(8000)})
        self.backend.record_replace('usage', {'x': 1, 'y': 2})
        capture = Mock()
        capture.fetchall.return_value = [(state_record_storage_key('usage', 'x'), '1')]
        self.assertEqual(PostgresStateStore._record_items_in_cursor(capture, 'usage'), {'x': 1})
        query, params = capture.execute.call_args.args
        self.assertNotIn('LEFT(', query)
        with self.backend._connection() as connection:
            connection.execute('ANALYZE codex_state_documents')
            plan = connection.execute('EXPLAIN (FORMAT JSON) ' + query, params).fetchone()[0]
        import json
        self.assertIn('Index Cond', json.dumps(plan))

    def test_legacy_revision_fallback_and_upgrade_preserve_encoded_rows(self):
        self.backend.record_replace('worker/雪:%', {'worker-1': 1, 'worker-2': 2})
        from codex_web.storage.state_store import state_record_marker
        with self.backend._connection() as connection:
            connection.execute('UPDATE codex_state_documents SET payload = %s WHERE namespace = %s',
                ('{"schemaVersion":1}', state_record_marker('worker/雪:%')))
        before = self.backend.namespace_revision('worker/雪:%')
        self.assertIsNotNone(before)
        self.backend.record_apply('worker/雪:%', upserts={'worker-3': 3})
        self.assertGreater(self.backend.namespace_revision('worker/雪:%'), before)
        self.assertEqual(self.backend.record_items('worker/雪:%'),
                         {'worker-1': 1, 'worker-2': 2, 'worker-3': 3})


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'PostgreSQL test DSN required')
class PostgresLocaleCollectionRangeTests(PostgresCollectionRangeTests):
    # The distributed CI job uses Debian PostgreSQL's default libc locale.
    # These regressions fail before the byte-collated range/index correction.
    collation = 'en_US.utf8'


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'PostgreSQL test DSN required')
class PostgresCallbackReadTests(unittest.TestCase):
    postgres_pool_max_size = 1
    postgres_pool_timeout = .25
    setUp = fixtures.PostgresAutonomyAppendTests.setUp

    def prepare(self):
        self.backend.put('admission', {'allowed': True})
        self.backend.record_replace('records', {'a': 1, 'b': 2})

    def checked(self, value):
        state = self.backend
        self.assertEqual(state.get('admission'), {'allowed': True})
        self.assertEqual(state.document_get('admission'), {'allowed': True})
        self.assertEqual(state.get('records'), {'a': 1, 'b': 2})
        self.assertEqual(state.record_get('records', 'a'), 1)
        self.assertEqual(state.record_get_many('records', ('a', 'missing')), {'a': 1})
        self.assertEqual(state.record_items('records'), {'a': 1, 'b': 2})
        self.assertEqual(state.record_count('records'), 2)
        self.assertEqual(state.record_page('records', limit=1), ({'a': 1}, 'a'))
        self.assertTrue(state.record_collection_exists('records'))
        self.assertEqual(state.record_collections_exist(('records', 'missing')),
                         {'records': True, 'missing': False})
        self.assertIsNotNone(state.namespace_revision('records'))
        self.assertTrue(state.contains('admission'))
        self.assertEqual(state.documents()['records'], {'a': 1, 'b': 2})
        self.assertEqual(state.schema_version(), state.SCHEMA_VERSION)
        self.assertTrue(state.status()['ok'])
        return value

    def test_all_mutation_callbacks_read_without_another_pool_checkout(self):
        from unittest.mock import patch
        self.prepare()
        state = self.backend
        operations = (
            lambda: state.record_update('records', 'a', self.checked, default=None),
            lambda: state.record_mutate('records', ('a',), self.checked),
            lambda: state.record_mutate_many({'records': ('a',)}, self.checked),
            lambda: state.update('admission', self.checked, default=None),
            lambda: state.update('records', self.checked, default=None),
            lambda: state.update_many({'admission': {}}, self.checked),
        )
        for index, operation in enumerate(operations):
            with self.subTest(index=index), patch.object(state._pool, 'connection', wraps=state._pool.connection) as checkout:
                operation()
                self.assertEqual(checkout.call_count, 1)
        state.put('legacy', {'a': 1})
        def migrated(rows):
            # Initialization belongs to the outer mutation. Reads must see its
            # uncommitted marker/rows, not a different transaction's old view.
            self.assertTrue(state.record_collection_exists('legacy'))
            self.assertEqual(state.record_count('legacy'), 1)
            self.assertEqual(state.record_page('legacy'), ({'a': 1}, None))
            return rows
        state.record_mutate_many({'legacy': ('a',)}, migrated)

    def test_failed_callback_rolls_back_and_releases_reader_custody(self):
        from unittest.mock import patch
        self.prepare()
        def fail(rows):
            self.checked(rows)
            rows['records']['a'] = 99
            raise ValueError('denied admission')
        with self.assertRaisesRegex(ValueError, 'denied admission'):
            self.backend.record_mutate_many({'records': ('a',)}, fail)
        with patch.object(self.backend._pool, 'connection', wraps=self.backend._pool.connection) as checkout:
            self.assertEqual(self.backend.record_get('records', 'a'), 1)
            self.assertEqual(checkout.call_count, 1)
        self.backend.record_update('records', 'a', lambda value: value + 1, default=0)
        self.assertEqual(self.backend.record_get('records', 'a'), 2)

    def test_nested_mutation_is_refused_without_independent_side_effects(self):
        self.prepare()
        def mutate(rows):
            self.backend.put('outside', {'unexpected': True})
            return rows
        with self.assertRaisesRegex(RuntimeError, 'nested mutation'):
            self.backend.record_mutate_many({'records': ('a',)}, mutate)
        self.assertFalse(self.backend.contains('outside'))
        self.assertEqual(self.backend.record_get('records', 'a'), 1)

    def test_another_thread_cannot_borrow_the_callback_connection(self):
        from concurrent.futures import ThreadPoolExecutor
        from psycopg_pool import PoolTimeout
        self.prepare()
        def inspect(rows):
            with ThreadPoolExecutor(max_workers=1) as executor:
                other = executor.submit(self.backend.get, 'admission')
                # The only lease is owned here. A separate thread must use the
                # bounded pool and time out, never share the live transaction.
                with self.assertRaises(PoolTimeout):
                    other.result(timeout=2)
            self.assertEqual(self.backend.get('admission'), {'allowed': True})
            return rows
        self.backend.record_mutate_many({'records': ('a',)}, inspect)
        self.assertEqual(self.backend.get('admission'), {'allowed': True})
