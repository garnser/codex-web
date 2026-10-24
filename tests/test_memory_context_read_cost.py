"""Complete current source records without repeated unrelated Memory/audit reads."""
import json
import os
import unittest
from unittest.mock import patch

from codex_web.organizational_memory import KnowledgeLifecycle, KnowledgeQuery
from codex_web.storage.organizational_memory import KnowledgeNotFoundError
from tests import test_ux_briefs as ux_fixtures
from tests import test_autonomy_append_scaling as pg_fixtures


class MemoryContextReadContract:
    create_memory = ux_fixtures.UxBriefMemoryServiceTests.create_memory
    create_brief = ux_fixtures.UxBriefMemoryServiceTests.create_brief

    def initialize(self):
        ux_fixtures.UxBriefMemoryServiceTests.setUp(self)
        self.addCleanup(self.temp.cleanup)
        if hasattr(self, 'backend'):
            self.store.store = self.backend
            self.governance.store.store = self.backend
        else:
            self.backend = self.store.store

    def test_public_search_keeps_output_and_audit_without_source_catalog_reloads(self):
        source = self.create_memory('source')
        brief = self.create_brief(source)
        state = self.store.load()
        state.records += [source.model_copy(update={'id': f'unrelated-{i}',
            'logical_key': f'unrelated-{i}', 'content': 'unrelated content ' * 100}) for i in range(40)]
        self.backend.put(self.store.namespace, state.model_dump(mode='json'))
        query = KnowledgeQuery(knowledge_ids=(brief.id,), project_ids=('project-a',))
        with patch.object(self.backend, 'supports_document_array_scope_after_first', False):
            expected = self.service.search(query, actor=self.actor)
        decoded_sizes = []
        decode = self.store._decode
        def counted(raw):
            decoded_sizes.append(len(raw.get('records', [])))
            return decode(raw)
        with patch.object(self.store, 'load', wraps=self.store.load) as loads, \
                patch.object(self.store, '_decode', side_effect=counted):
            actual = self.service.search(query, actor=self.actor)
        self.assertEqual(actual.model_dump(exclude={'retrieval_id'}),
                         expected.model_dump(exclude={'retrieval_id'}))
        self.assertEqual(loads.call_count, 2)  # Initial snapshot + fresh index check.
        self.assertEqual(decoded_sizes.count(42), 3)  # Plus atomic current audit.
        self.assertEqual(decoded_sizes.count(1), 2)  # Two complete source points.
        retained = self.store.load()
        self.assertEqual([r.id for r in retained.retrieval_runs],
                         [expected.retrieval_id, actual.retrieval_id])

    def test_first_global_record_precedes_scope_and_duplicate_selection(self):
        first = self.create_memory('first')
        foreign = first.model_copy(update={'organization_id': 'foreign'})
        later = first.model_copy(update={'title': 'Later duplicate'})
        state = self.store.load()
        for rows in ([foreign, first], [first, later]):
            self.backend.put(self.store.namespace, state.model_copy(update={'records': rows}).model_dump(mode='json'))
            if rows[0] is foreign:
                for context_only in (False, True):
                    with self.assertRaises(KnowledgeNotFoundError):
                        self.service.get(first.id, actor=self.actor, context_only=context_only)
            else:
                self.assertEqual(self.service.get(first.id, actor=self.actor, context_only=True), first)

    def test_selected_source_keeps_current_role_project_and_inactive_guards(self):
        source = self.create_memory('guards')
        self.assertEqual(self.service.get(source.id, actor=self.actor, context_only=True), source)
        state = self.store.load()
        for changes in ({'required_role_ids': ('unheld-role',)}, {'lifecycle': KnowledgeLifecycle.DELETED}):
            self.backend.put(self.store.namespace, state.model_copy(update={
                'records': [source.model_copy(update=changes)]}).model_dump(mode='json'))
            with self.assertRaises((ValueError, RuntimeError, KeyError)):
                self.service.get(source.id, actor=self.actor, context_only=True)
        self.backend.put(self.store.namespace, state.model_dump(mode='json'))
        with self.assertRaises(RuntimeError):
            self.briefs._source('knowledge', source.id, 'another-project', self.actor, context_only=True)

    def test_legacy_and_malformed_envelopes_keep_full_domain_decoder(self):
        source = self.create_memory('legacy')
        raw = self.backend.get(self.store.namespace)
        missing = dict(raw); missing.pop('schema_version')
        self.backend.put(self.store.namespace, missing)
        for context_only in (False, True):
            with self.assertRaises(ValueError):
                self.service.get(source.id, actor=self.actor, context_only=context_only)
        self.backend.put(self.store.namespace, {**raw, 'schema_version': '1.0'})
        self.assertEqual(self.service.get(source.id, actor=self.actor, context_only=True), source)
        for change in ({'schema_version': '99.0'}, {'relationships': 'malformed'},
                       {'unexpected': True}, {'records': [{'id': source.id}]}):
            with self.subTest(change=change):
                self.backend.put(self.store.namespace, {**raw, **change})
                with self.assertRaises((ValueError, RuntimeError)):
                    self.service.get(source.id, actor=self.actor, context_only=True)

    def test_full_fallback_and_initial_search_do_not_hide_unrelated_invalid_rows(self):
        source = self.create_memory('invalid')
        raw = self.backend.get(self.store.namespace)
        raw['records'].append({'id': 'unrelated-invalid'})
        self.backend.put(self.store.namespace, raw)
        with self.assertRaises(ValueError):
            self.service.search(KnowledgeQuery(project_ids=('project-a',)), actor=self.actor)
        with patch.object(self.backend, 'document_array_items_envelope', return_value=raw):
            with self.assertRaises(ValueError):
                self.service.get(source.id, actor=self.actor, context_only=True)

    def test_legacy_backend_keeps_ordinary_point_and_curation_keeps_default_get(self):
        source = self.create_memory('custom')
        with patch.object(self.backend, 'supports_document_array_scope_after_first', False), \
                patch.object(self.backend, 'document_array_items_envelope', side_effect=AssertionError('old signature')):
            self.assertEqual(self.service.get(source.id, actor=self.actor, context_only=True), source)
        with patch.object(self.store, 'get_for_context', side_effect=AssertionError('curation is not retrieval')):
            self.assertEqual(self.briefs._source('knowledge', source.id, 'project-a', self.actor), source)

    def test_atomic_audit_append_preserves_current_mutations_and_retention(self):
        self.create_memory('audit')
        self.service.search(KnowledgeQuery(), actor=self.actor)
        state = self.store.load(); original = state.retrieval_runs[0]
        state.records[0] = state.records[0].model_copy(update={'title': 'Concurrent current change'})
        state.retrieval_runs = [original.model_copy(update={'id': f'run-{i}'}) for i in range(5000)]
        self.backend.put(self.store.namespace, state.model_dump(mode='json'))
        new = original.model_copy(update={'id': 'new-run'})
        with patch.object(self.store, '_decode', wraps=self.store._decode) as decoder:
            self.assertIsNone(self.store.append_retrieval_run(new))
        self.assertEqual(decoder.call_count, 1)
        actual = self.store.load()
        self.assertEqual(actual.records[0].title, 'Concurrent current change')
        self.assertEqual(len(actual.retrieval_runs), 5000)
        self.assertEqual((actual.retrieval_runs[0].id, actual.retrieval_runs[-1].id), ('run-1', 'new-run'))


class SQLiteMemoryContextReadTests(MemoryContextReadContract, unittest.TestCase):
    def setUp(self): self.initialize()


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'isolated PostgreSQL DSN required')
class PostgresMemoryContextReadTests(MemoryContextReadContract, unittest.TestCase):
    def setUp(self):
        pg_fixtures.PostgresAutonomyAppendTests.setUp(self)
        self.initialize()

    def test_actual_conversion_fallback_preserves_full_validation_and_transaction(self):
        source = self.create_memory('conversion')
        for value in ('before\0after', 'before\ud800after', float('nan'), 'overflow-number'):
            with self.subTest(value_type=type(value).__name__):
                raw = self.backend.get(self.store.namespace)
                raw['records'].append({'id': 'unrelated-invalid', 'content': value})
                text = json.dumps(raw)
                if value == 'overflow-number': text = text.replace('"overflow-number"', '1e1000000')
                with self.backend._connection() as c:
                    c.execute('UPDATE codex_state_documents SET payload=%s WHERE namespace=%s',
                              (text, self.store.namespace))
                self.backend.put('memory-sentinel', {'checked': False})
                def update(current):
                    with self.assertRaises(ValueError):
                        self.service.get(source.id, actor=self.actor, context_only=True)
                    return {'checked': True}
                self.backend.update('memory-sentinel', update, default={})
                self.assertEqual(self.backend.get('memory-sentinel'), {'checked': True})
                self.backend.put(self.store.namespace, {**raw, 'records': [raw['records'][0]]})
