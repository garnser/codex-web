from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from codex_web.storage.sqlite_state import SQLiteStateStore


class KeyedRevisionContract:
    def frozen_clock(self, value):
        return patch(self.clock_module + '.time.time', return_value=value)

    def seed(self, namespace='cache'):
        with self.frozen_clock(10):
            self.store.record_replace(namespace, {'older': {'value': 1}})
        with self.frozen_clock(20):
            self.store.record_apply(namespace, upserts={'newer': {'value': 2}})
        return self.store.namespace_revision(namespace)

    def test_deletion_of_older_and_last_record_invalidates_other_reader(self):
        for method in ('apply', 'update', 'mutate'):
            with self.subTest(method=method):
                before = self.seed()
                with self.frozen_clock(5):
                    if method == 'apply':
                        self.store.record_apply('cache', upserts={}, deletes=('older',))
                    elif method == 'update':
                        self.store.record_update('cache', 'older', lambda _: None, default=None)
                    else:
                        self.store.record_mutate('cache', ('older',), lambda _: {'older': None})
                after = self.reader.namespace_revision('cache')
                self.assertGreater(after, before)
                self.assertIsNone(self.reader.record_get('cache', 'older'))
                with self.frozen_clock(5):
                    self.store.record_apply('cache', upserts={}, deletes=('newer',))
                self.assertGreater(self.reader.namespace_revision('cache'), after)
                self.assertEqual(self.reader.record_items('cache'), {})

    def test_older_keyed_marker_is_initialized_on_first_mutation(self):
        from codex_web.storage.state_store import state_record_marker, state_record_storage_key
        # Reproduce the physical collection left by a pre-revision writer.
        with self.frozen_clock(10):
            self.store.put(state_record_marker('cache'), {'schemaVersion': 1})
            self.store.put(state_record_storage_key('cache', 'older'), 1)
        with self.frozen_clock(20):
            self.store.put(state_record_storage_key('cache', 'newer'), 2)
        before = self.reader.namespace_revision('cache')
        self.assertEqual(before, 20)
        with self.frozen_clock(5):
            self.store.record_apply('cache', upserts={}, deletes=('older',))
        self.assertGreater(self.reader.namespace_revision('cache'), before)
        self.assertEqual(self.reader.record_items('cache'), {'newer': 2})

    def test_work_item_pagination_revision_notices_an_older_removal(self):
        from codex_web.identity import TenantScope
        from codex_web.models import WorkItemState
        from codex_web.storage.work_item_list_index import WorkItemListIndex
        writer = WorkItemListIndex(self.store)
        reader = WorkItemListIndex(self.reader)
        scope = TenantScope()
        for timestamp, ref in ((10, 'example/repo#1'), (20, 'example/repo#2')):
            state = WorkItemState(ref=ref, project_id='project-a', updated_at=timestamp, created_at=timestamp, last_meaningful_update_at=timestamp)
            with self.frozen_clock(timestamp):
                writer.upsert(state)
        before = reader.revision(scope=scope, project_id='project-a')
        with self.frozen_clock(20):
            writer.remove('example/repo#1')
        self.assertGreater(reader.revision(scope=scope, project_id='project-a'), before)

    def test_empty_deltas_and_missing_deletes_preserve_revision(self):
        before = self.seed()
        self.store.record_apply('cache', upserts={}, deletes=('missing',))
        self.store.record_update('cache', 'missing', lambda _: None, default=None)
        self.store.record_mutate('cache', ('older',), lambda _: {})
        self.store.record_mutate('cache', ('missing',), lambda _: {'missing': None})
        self.assertEqual(self.reader.namespace_revision('cache'), before)

    def test_frozen_clock_upserts_and_empty_replacements_change_revision(self):
        before = self.seed()
        with self.frozen_clock(1):
            self.store.record_update('cache', 'older', lambda _: {'value': 3}, default=None)
            updated = self.reader.namespace_revision('cache')
            self.assertGreater(updated, before)
            self.store.record_replace('cache', {})
            replaced = self.reader.namespace_revision('cache')
            self.assertGreater(replaced, updated)
            self.store.record_replace('cache', {})
            self.assertGreater(self.reader.namespace_revision('cache'), replaced)

    def test_failed_mutation_rolls_back_records_and_revision(self):
        before = self.seed()
        def fail(_):
            raise ValueError('rollback')
        with self.assertRaisesRegex(ValueError, 'rollback'):
            self.store.record_update('cache', 'older', fail, default=None)
        with self.assertRaises(TypeError):
            self.store.record_apply('cache', upserts={'ok': 3, 'bad': object()}, deletes=('older',))
        self.assertEqual(self.reader.namespace_revision('cache'), before)
        self.assertEqual(self.reader.record_items('cache'), {'older': {'value': 1}, 'newer': {'value': 2}})

    def test_encoded_namespace_neighbors_do_not_invalidate_or_get_deleted(self):
        before = self.seed('cache/%?')
        with self.frozen_clock(100):
            self.store.record_replace('cache/%?extra', {'untouched': 3})
            self.store.record_replace('cache', {'untouched': 4})
        self.assertEqual(self.reader.namespace_revision('cache/%?'), before)
        self.store.record_replace('cache/%?', {})
        self.assertEqual(self.reader.record_items('cache/%?extra'), {'untouched': 3})
        self.assertEqual(self.reader.record_items('cache'), {'untouched': 4})

    def test_concurrent_point_writers_and_replacements_complete(self):
        self.store.record_replace('cache', {'counter': 0})
        def write(_):
            self.store.record_update('cache', 'counter', lambda n: n + 1, default=0)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(write, i) for i in range(12)]
            for future in futures:
                future.result(timeout=15)
        self.assertEqual(self.reader.record_get('cache', 'counter'), 12)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(self.store.record_replace, 'cache', {'counter': 0})]
            futures += [pool.submit(write, i) for i in range(12)]
            for future in futures:
                future.result(timeout=15)
        self.assertIsInstance(self.reader.record_get('cache', 'counter'), int)

    def test_legacy_document_migration_and_aggregate_writes_invalidate(self):
        self.store.put('cache', {'older': 1})
        before = self.reader.namespace_revision('cache')
        with self.frozen_clock(1):
            self.store.record_apply('cache', upserts={'newer': 2})
        self.assertNotEqual(self.reader.namespace_revision('cache'), before)
        before = self.reader.namespace_revision('cache')
        with self.frozen_clock(1):
            self.store.put('cache', {'replacement': 3})
        self.assertGreater(self.reader.namespace_revision('cache'), before)
        self.assertEqual(self.reader.record_items('cache'), {'replacement': 3})


class SQLiteKeyedRevisionTests(KeyedRevisionContract, unittest.TestCase):
    clock_module = 'codex_web.storage.sqlite_state'

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / 'state.db'
        self.store = SQLiteStateStore(path)
        self.reader = SQLiteStateStore(path)


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'PostgreSQL test DSN required')
class PostgresKeyedRevisionTests(KeyedRevisionContract, unittest.TestCase):
    clock_module = 'codex_web.storage.postgres_state'

    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg_pool import ConnectionPool
        from codex_web.storage.postgres_state import PostgresStateStore
        dsn = os.environ['CODEX_WEB_TEST_POSTGRES_DSN']
        schema = 'keyed_revision_test_' + uuid.uuid4().hex
        with psycopg.connect(dsn) as connection:
            connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        def cleanup():
            with psycopg.connect(dsn) as connection:
                connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        self.addCleanup(cleanup)
        pool = ConnectionPool(dsn, min_size=1, max_size=4, kwargs={'options': f'-c search_path={schema}'})
        self.addCleanup(pool.close)
        self.store = PostgresStateStore(dsn, connection_pool=pool)
        self.reader = PostgresStateStore(dsn, connection_pool=pool)
