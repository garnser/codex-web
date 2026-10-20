"""Atomic collection updates must leave unrelated persisted history untouched."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from codex_web.storage.state_store import state_record_prefix, state_record_storage_key
from tests import test_autonomy_append_scaling as fixtures


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'PostgreSQL test DSN required')
class PostgresUpdateManyDeltaTests(unittest.TestCase):
    setUp = fixtures.PostgresAutonomyAppendTests.setUp

    def rows(self, namespace):
        prefix = state_record_prefix(namespace) + 'k/'
        with self.backend._connection() as connection:
            return dict((key, (payload, timestamp, tuple_id)) for key, payload, timestamp, tuple_id in
                connection.execute('SELECT namespace,payload,updated_at,ctid::text FROM codex_state_documents '
                    'WHERE namespace COLLATE "C" >= %s AND namespace COLLATE "C" < %s',
                    (prefix, prefix + '\uffff')).fetchall())

    def test_one_nested_mutation_preserves_unrelated_payloads_timestamps_and_rows(self):
        original = {str(i): {'nested': {'fence': i}} for i in range(500)}
        self.backend.record_replace('workspaces', original)
        before = self.rows('workspaces')
        revision = self.backend.namespace_revision('workspaces')

        def release(documents):
            documents['workspaces']['0']['nested']['fence'] = 501
            return documents

        result = self.backend.update_many({'workspaces': {}}, release)
        self.assertEqual(result['workspaces']['0']['nested']['fence'], 501)
        self.assertEqual(self.backend.record_get('workspaces', '0')['nested']['fence'], 501)
        after = self.rows('workspaces')
        changed = state_record_storage_key('workspaces', '0')
        self.assertNotEqual(before[changed], after[changed])
        self.assertEqual({k: v for k, v in before.items() if k != changed},
                         {k: v for k, v in after.items() if k != changed})
        self.assertGreater(self.backend.namespace_revision('workspaces'), revision)

    def test_added_removed_and_empty_collections_preserve_neighbors(self):
        self.backend.record_replace('leases/雪', {'keep': {'fence': 1}, 'remove': 2})
        self.backend.record_replace('leases/雪.neighbor', {'foreign': 3})
        self.backend.record_replace('empty', {'remove': 4})
        self.backend.put('legacy', {'old': True})
        before = self.rows('leases/雪')
        self.backend.update_many({'leases/雪': {}, 'empty': {}, 'legacy': {}}, lambda current: {
            'leases/雪': {'keep': current['leases/雪']['keep'], 'added': 5},
            'empty': {}, 'legacy': {'new': True}})
        self.assertEqual(self.backend.record_items('leases/雪'), {'keep': {'fence': 1}, 'added': 5})
        key = state_record_storage_key('leases/雪', 'keep')
        self.assertEqual(self.rows('leases/雪')[key], before[key])
        self.assertEqual(self.backend.record_items('leases/雪.neighbor'), {'foreign': 3})
        self.assertTrue(self.backend.record_collection_exists('empty'))
        self.assertEqual(self.backend.record_items('empty'), {})
        self.assertEqual(self.backend.get('legacy'), {'new': True})

    def test_no_change_keeps_rows_but_advances_existing_collection_revision_contract(self):
        self.backend.record_replace('events', {'retained': {'event': 'accepted'}})
        before = self.rows('events')
        revision = self.backend.namespace_revision('events')
        self.backend.update_many({'events': {}}, lambda current: current)
        self.assertEqual(self.rows('events'), before)
        self.assertGreater(self.backend.namespace_revision('events'), revision)

    def test_later_namespace_failure_rolls_back_earlier_changes_and_revisions(self):
        for name in ('first', 'second'):
            self.backend.record_replace(name, {'item': {'fence': 1}, 'retained': 'evidence'})
        before = {name: self.rows(name) for name in ('first', 'second')}
        revisions = {name: self.backend.namespace_revision(name) for name in before}
        upsert = self.backend._upsert

        def fail_second(cursor, key, value):
            if key == state_record_storage_key('second', 'item'):
                raise ValueError('second namespace failed')
            return upsert(cursor, key, value)

        with patch.object(type(self.backend), '_upsert', side_effect=fail_second):
            with self.assertRaisesRegex(ValueError, 'second namespace failed'):
                self.backend.update_many({name: {} for name in before}, lambda current: {
                    name: {'item': {'fence': 2}, 'retained': 'evidence'} for name in before})
        self.assertEqual({name: self.rows(name) for name in before}, before)
        self.assertEqual({name: self.backend.namespace_revision(name) for name in before}, revisions)

    def test_deleting_future_dated_row_preserves_monotonic_revision(self):
        self.backend.record_replace('leases', {'removed': 1, 'retained': 2})
        revision = self.backend.namespace_revision('leases')
        future = revision + 100
        with self.backend._connection() as connection:
            connection.execute('UPDATE codex_state_documents SET updated_at=%s WHERE namespace=%s',
                (future, state_record_storage_key('leases', 'removed')))
        self.backend.update_many({'leases': {}}, lambda current: {'leases': {'retained': 2}})
        self.assertGreater(self.backend.namespace_revision('leases'), future)

    def test_single_namespace_update_preserves_retained_rows_and_nested_changes(self):
        self.backend.record_replace('records', {'changed': {'nested': [1]}, 'remove': 2, 'keep': 3})
        before = self.rows('records')
        def mutate(current):
            current['changed']['nested'].append(2)
            del current['remove']
            return current
        self.backend.update('records', mutate, default={})
        self.assertEqual(self.backend.record_items('records'), {'changed': {'nested': [1, 2]}, 'keep': 3})
        key = state_record_storage_key('records', 'keep')
        self.assertEqual(self.rows('records')[key], before[key])

    def test_json_boolean_integer_and_float_changes_are_persisted_distinctly(self):
        for many in (False, True):
            with self.subTest(many=many):
                self.backend.record_replace('types', {'value': True})
                for value, expected in ((1, int), (1.0, float), (True, bool)):
                    if many:
                        self.backend.update_many({'types': {}}, lambda current: {'types': {'value': value}})
                    else:
                        self.backend.update('types', lambda current: {'value': value}, default={})
                    self.assertIs(type(self.backend.record_get('types', 'value')), expected)

    def test_concurrent_updates_keep_atomic_counts_and_retained_evidence(self):
        self.backend.record_replace('counts', {'count': 0, 'retained': {'evidence': 'accepted'}})
        before = self.rows('counts')[state_record_storage_key('counts', 'retained')]
        def increment(_):
            for _ in range(10):
                def mutate(current):
                    current['counts']['count'] += 1
                    return current
                self.backend.update_many({'counts': {}}, mutate)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(increment, range(2)))
        self.assertEqual(self.backend.record_get('counts', 'count'), 20)
        self.assertEqual(self.rows('counts')[state_record_storage_key('counts', 'retained')], before)


if __name__ == '__main__':
    unittest.main()
