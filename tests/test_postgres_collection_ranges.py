from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from codex_web.storage.postgres_state import PostgresStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.state_store import state_record_storage_key
from tests import test_autonomy_append_scaling as fixtures


class CollectionRangeContract:
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
