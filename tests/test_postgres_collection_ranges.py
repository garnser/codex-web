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


class SQLiteCollectionRangeTests(CollectionRangeContract, unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.backend = SQLiteStateStore(Path(directory.name) / 'state.db')

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
    setUp = fixtures.PostgresAutonomyAppendTests.setUp

    def test_real_collection_read_plan_uses_existing_namespace_index(self):
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
