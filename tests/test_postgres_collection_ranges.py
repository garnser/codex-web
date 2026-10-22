from __future__ import annotations

import os
import math
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
