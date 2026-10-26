"""Real synthetic PG export correctness, transaction custody and allocation regression."""
import gc
import hashlib
import json
import os
import resource
import time
import tracemalloc
import unittest
import uuid
from contextlib import contextmanager
from unittest.mock import patch

from codex_web.storage.postgres_state import PostgresStateStore
from codex_web.storage.state_store import (
    StorageOperationBudget, storage_operation_budget, state_documents_checksum,
    state_record_marker, state_record_storage_key,
)


class _Cursor:
    def __init__(self, cursor, stats, hook):
        self.cursor, self.stats, self.hook = cursor, stats, hook
        self.export = False

    def __enter__(self):
        self.cursor.__enter__()
        return self

    def __exit__(self, *args):
        try:
            return self.cursor.__exit__(*args)
        finally:
            if self.export:
                self.stats['closed'] = self.cursor.closed

    def __getattr__(self, key):
        return getattr(self.cursor, key)

    def execute(self, query, *args, **kwargs):
        self.export = isinstance(query, str) and query.startswith('SELECT namespace, payload FROM codex_state_documents ORDER BY')
        if self.export:
            self.stats['named'] = bool(getattr(self.cursor, 'name', None))
        if isinstance(query, str) and "set_config('statement_timeout'" in query:
            self.stats.setdefault('timeouts_ms', []).append(int(args[0][0]))
        return self.cursor.execute(query, *args, **kwargs)

    def fetchmany(self, count):
        rows = self.cursor.fetchmany(count)
        self.stats['batches'].append(len(rows))
        if self.hook:
            self.hook(rows, len(self.stats['batches']))
        return rows

    def fetchall(self):
        self.stats['fetchall'] += 1
        return self.cursor.fetchall()


class _Connection:
    def __init__(self, connection, stats, hook):
        self.connection, self.stats, self.hook = connection, stats, hook

    def cursor(self, *args, **kwargs):
        return _Cursor(self.connection.cursor(*args, **kwargs), self.stats, self.hook)

    def __getattr__(self, key):
        return getattr(self.connection, key)


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'isolated PostgreSQL test DSN required')
class PostgresDocumentsStreamingTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg_pool import ConnectionPool
        self.dsn = os.environ['CODEX_WEB_TEST_POSTGRES_DSN']
        self.schema = 'documents_1520_' + uuid.uuid4().hex
        with psycopg.connect(self.dsn) as connection:
            connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        def drop():
            with psycopg.connect(self.dsn) as connection:
                connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        self.addCleanup(drop)
        self.pool = ConnectionPool(self.dsn, min_size=1, max_size=1, timeout=.5,
                                  kwargs={'options': '-c search_path=' + self.schema})
        # Caller owns this supplied pool. LIFO: backend, pool, then schema.
        self.addCleanup(self.pool.close)
        self.backend = PostgresStateStore(self.dsn, connection_pool=self.pool)
        self.addCleanup(self.backend.close)

    def test_existing_only_constructor_reads_private_schema_without_initialization(self):
        from unittest.mock import patch
        self.backend.put('existing-only-private-fixture', {'retained': True})
        with patch.object(PostgresStateStore, '_initialize', side_effect=AssertionError('schema bootstrap')):
            existing = PostgresStateStore(self.dsn, connection_pool=self.pool, initialize=False)
        self.addCleanup(existing.close)
        self.assertEqual(existing.get('existing-only-private-fixture'), {'retained': True})

    def test_existing_only_empty_private_schema_remains_empty(self):
        import psycopg
        from psycopg import sql
        from psycopg_pool import ConnectionPool
        empty = 'existing_only_' + uuid.uuid4().hex
        with psycopg.connect(self.dsn) as connection:
            connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(empty)))
        try:
            pool = ConnectionPool(self.dsn, min_size=1, max_size=1, timeout=.5,
                kwargs={'options': '-c search_path=' + empty})
            try:
                with self.assertRaises(psycopg.errors.UndefinedTable):
                    PostgresStateStore(self.dsn, connection_pool=pool, initialize=False)
            finally:
                pool.close()
            with psycopg.connect(self.dsn) as connection:
                rows = connection.execute('SELECT tablename FROM pg_tables WHERE schemaname=%s', (empty,)).fetchall()
            self.assertEqual(rows, [])
        finally:
            with psycopg.connect(self.dsn) as connection:
                connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(empty)))

    def seed(self, count, payload_size=16384):
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                fixed = [('anchor', {'value': 1}), ('legacy', {'winner': 'legacy'}),
                         (state_record_marker('legacy'), {'schemaVersion': 1}),
                         (state_record_storage_key('legacy', 'ignored'), {'winner': 'keyed-shadow'}),
                         (state_record_marker('records'), {'schemaVersion': 1})]
                for namespace, payload in fixed:
                    cursor.execute('INSERT INTO codex_state_documents(namespace,payload,updated_at) VALUES(%s,%s,0)',
                                   (namespace, json.dumps(payload, sort_keys=True, separators=(',', ':'))))
                for i in range(count):
                    payload = {'blob': hashlib.shake_256(str(i).encode()).hexdigest(payload_size // 2), 'index': i}
                    cursor.execute('INSERT INTO codex_state_documents(namespace,payload,updated_at) VALUES(%s,%s,0)',
                                   (state_record_storage_key('records', f'{i:06d}'),
                                    json.dumps(payload, sort_keys=True, separators=(',', ':'))))

    @contextmanager
    def observe(self, hook=None):
        stats = {'batches': [], 'fetchall': 0, 'closed': False}
        original = self.backend._read_connection
        @contextmanager
        def read():
            with original() as connection:
                yield _Connection(connection, stats, hook)
        with patch.object(self.backend, '_read_connection', read):
            yield stats

    def test_export_does_not_overlap_full_encoded_and_decoded_mapping(self):
        self.seed(1024)
        gc.collect()
        with self.observe() as stats:
            tracemalloc.start()
            try:
                documents = self.backend.documents()
                retained, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        # Baseline exact75ada: peak34,203,086 / retained17,270,618 (1.98x).
        # Allow ample interpreter variance while requiring removal of the full raw copy.
        checksum = state_documents_checksum(documents)
        self.assertEqual(checksum, '2cc9ad158c066917df512f49d300aa80a4f2cb5f7afec53267a80e817f589c13')
        print(json.dumps({'pg_export_measurement': {'rows': 1024, 'retained_traced': retained,
              'peak_traced': peak, 'ru_maxrss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              'cursor': stats, 'logical_checksum': checksum}}))
        self.assertLess(peak, retained * 1.30)
        self.assertTrue(stats['named'])
        self.assertEqual(stats['fetchall'], 0)
        self.assertTrue(stats['closed'])
        self.assertLessEqual(max(stats['batches']), 64)
        self.assertEqual(sum(stats['batches']), 1029)

    def test_unicode_fields_empty_markers_and_legacy_precedence(self):
        self.seed(4, 32)
        self.backend.record_replace('empty', {})
        self.backend.record_replace('tenant/雪', {'key/%': {'audit': [1, None], 'flag': False}})
        result = self.backend.documents()
        self.assertEqual(result['legacy'], {'winner': 'legacy'})
        self.assertEqual(result['empty'], {})
        self.assertEqual(result['tenant/雪'], {'key/%': {'audit': [1, None], 'flag': False}})
        self.assertEqual(len(result['records']), 4)
        self.assertFalse(any(key.startswith('__codex_records__/') for key in result))

    def test_batches_share_one_query_snapshot_during_another_connection_commit(self):
        import psycopg
        self.seed(192, 32)
        changed = []
        def writer(rows, batch):
            if batch != 1:
                return
            with psycopg.connect(self.dsn, options='-c search_path=' + self.schema) as connection:
                connection.execute('UPDATE codex_state_documents SET payload=%s WHERE namespace=%s',
                                   ('{"blob":"changed","index":191}', state_record_storage_key('records', '000191')))
                connection.execute('INSERT INTO codex_state_documents(namespace,payload,updated_at) VALUES(%s,%s,0)',
                                   ('new-after-select', '{"new":true}'))
            changed.append(True)
        with self.observe(writer) as stats:
            result = self.backend.documents()
        self.assertTrue(changed)
        self.assertNotIn('new-after-select', result)
        self.assertEqual(result['records']['000191']['blob'], hashlib.shake_256(b'191').hexdigest(16))
        self.assertEqual(self.backend.get('new-after-select'), {'new': True})
        self.assertGreater(len(stats['batches']), 2)

    def callback_export(self, fail):
        self.seed(2, 32)
        def updater(value):
            connection = self.backend._callback_reads.connection
            connection.execute('INSERT INTO codex_state_documents(namespace,payload,updated_at) VALUES(%s,%s,0)',
                               ('callback-write', '{"pending":true}'))
            self.assertEqual(self.backend.documents()['callback-write'], {'pending': True})
            if fail:
                raise ValueError('abort owning callback')
            return {'value': value['value'] + 1}
        with patch.object(self.pool, 'connection', wraps=self.pool.connection) as checkout:
            if fail:
                with self.assertRaisesRegex(ValueError, 'abort owning callback'):
                    self.backend.update('anchor', updater, default={})
            else:
                self.backend.update('anchor', updater, default={})
            self.assertEqual(checkout.call_count, 1)
        self.assertIsNone(getattr(self.backend._callback_reads, 'connection', None))
        self.assertEqual(self.backend.get('anchor'), {'value': 1 if fail else 2})
        self.assertEqual(self.backend.get('callback-write'), None if fail else {'pending': True})

    def test_callback_reads_own_uncommitted_state_without_second_pool_lease(self):
        self.callback_export(False)

    def test_callback_export_does_not_commit_or_hide_outer_rollback(self):
        self.callback_export(True)

    def test_per_row_cooperative_cancellation_returns_no_partial_export(self):
        self.seed(192, 32)
        budget = StorageOperationBudget(10)
        original = json.loads
        decoded = []
        def cancel(value, *args, **kwargs):
            result = original(value, *args, **kwargs)
            if isinstance(result, dict) and 'blob' in result:
                decoded.append(result['index'])
                budget.cancelled.set()
            return result
        token = storage_operation_budget.set(budget)
        try:
            with self.observe() as stats, patch('codex_web.storage.postgres_state.json.loads', cancel):
                with self.assertRaises(TimeoutError):
                    self.backend.documents()
        finally:
            storage_operation_budget.reset(token)
        self.assertEqual(len(decoded), 1)
        self.assertTrue(stats['closed'])
        self.assertEqual(self.backend.get('anchor'), {'value': 1})

    def test_fetch_failure_closes_cursor_rolls_back_and_does_not_return_partial_data(self):
        self.seed(192, 32)
        def fail(rows, batch):
            if batch == 2:
                raise OSError('synthetic fetch interruption')
        with self.observe(fail) as stats:
            with self.assertRaisesRegex(OSError, 'synthetic fetch interruption'):
                self.backend.documents()
        self.assertTrue(stats['closed'])
        self.assertEqual(self.backend.get('anchor'), {'value': 1})

    def test_expired_budget_refuses_before_pool_checkout(self):
        token = storage_operation_budget.set(StorageOperationBudget(0))
        try:
            with patch.object(self.pool, 'connection', wraps=self.pool.connection) as checkout:
                with self.assertRaises(TimeoutError):
                    self.backend.documents()
                checkout.assert_not_called()
        finally:
            storage_operation_budget.reset(token)

    def test_remaining_statement_timeout_is_reapplied_for_each_fetch(self):
        self.seed(192, 32)
        budget = StorageOperationBudget(5)
        def shorten(rows, batch):
            if batch == 1:
                budget.deadline = time.monotonic() + 2
        token = storage_operation_budget.set(budget)
        try:
            with self.observe(shorten) as stats:
                self.assertEqual(len(self.backend.documents()['records']), 192)
        finally:
            storage_operation_budget.reset(token)
        self.assertEqual(len(stats['timeouts_ms']), len(stats['batches']))
        self.assertGreater(stats['timeouts_ms'][0], stats['timeouts_ms'][1])
        self.assertTrue(all(0 < value <= 2000 for value in stats['timeouts_ms'][1:]))

    def test_real_backup_restore_preserves_checksum_key_manifest_and_audit(self):
        from tests.test_recovery import RecoveryTests
        case = RecoveryTests('test_backup_is_encrypted_and_restore_verification_publishes_evidence')
        # Reuse the canonical integration fixture, substituting only its private StateStore backend.
        with patch('tests.test_recovery.SQLiteStateStore', return_value=self.backend):
            case.setUp()
        try:
            case.test_backup_is_encrypted_and_restore_verification_publishes_evidence()
        finally:
            case.tearDown()

    def test_bounded_semantic_capture_batches_audit_and_restores_with_pool_one(self):
        from pathlib import Path
        from tests import test_recovery as fixture
        from codex_web.autonomy_audit import AutonomyAuditRecord
        from codex_web.recovery import RestoreVerificationStatus
        from codex_web.services.recovery_snapshot_capture import SnapshotCapture, BATCH_ROWS, MAX_ROW_BYTES
        from codex_web.storage.sqlite_state import SQLiteStateStore

        case = fixture.RecoveryTests('test_backup_is_encrypted_and_restore_verification_publishes_evidence')
        with patch('tests.test_recovery.SQLiteStateStore', return_value=self.backend):
            case.setUp()
        try:
            self.seed(1024, payload_size=2048)
            state = case.audit_store.load()
            records = list(state.records)
            for _ in range(1024):
                previous = records[-1]
                records.append(AutonomyAuditRecord.build(previous.payload,
                    sequence=previous.sequence + 1, previous_hash=previous.record_hash,
                    created_at=case.clock()))
            case.audit_store.update(lambda _: state.model_copy(update={'records': records}))
            del state, records
            expected_root = case.audit.verify(organization_id='org-a', workspace_id='ws-a').root_hash
            stats = {'payload_queries': 0, 'max_batch_rows': 0, 'max_batch_bytes': 0}
            original_cursor = SnapshotCapture.cursor

            class MeasuredCursor:
                def __init__(self, cursor):
                    self.cursor, self.payload = cursor, False

                def execute(self, query, *args):
                    self.payload = query.startswith('SELECT namespace,payload FROM ')
                    if self.payload:
                        stats['payload_queries'] += 1
                    return self.cursor.execute(query, *args)

                def fetchmany(self, count):
                    rows = self.cursor.fetchmany(count)
                    if self.payload:
                        size = sum(len(value.encode('utf-8')) for _, value in rows)
                        stats['max_batch_rows'] = max(stats['max_batch_rows'], len(rows))
                        stats['max_batch_bytes'] = max(stats['max_batch_bytes'], size)
                        self_outer.assertLessEqual(len(rows), BATCH_ROWS)
                        self_outer.assertLessEqual(size, MAX_ROW_BYTES)
                    return rows

                def __getattr__(self, name):
                    return getattr(self.cursor, name)

            self_outer = self

            @contextmanager
            def measured_cursor(capture, named=False):
                with original_cursor(capture, named=named) as cursor:
                    yield MeasuredCursor(cursor)

            started = time.monotonic()
            with patch.object(SnapshotCapture, 'cursor', new=measured_cursor):
                tracemalloc.start()
                try:
                    manifest = case.service.create_backup(actor=case.actor)
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
            elapsed = time.monotonic() - started
            self.assertEqual(manifest.audit_root_hash, expected_root)
            self.assertEqual(stats['max_batch_rows'], BATCH_ROWS)
            self.assertLess(stats['payload_queries'], 128, 'payload reads must batch, not perform rowwise SQL')
            verified = case.service.verify_restore(manifest.id, actor=case.actor)
            self.assertEqual(verified.status, RestoreVerificationStatus.PASS)
            self.assertTrue(verified.checksum_valid and verified.key_manifest_valid and verified.audit_continuity_valid)
            target = SQLiteStateStore(Path(case.temp.name) / 'pg-batched-isolated.sqlite3')
            restored = case.service.restore_into_isolated(manifest.id, target, actor=case.actor)
            self.assertEqual(restored.status, RestoreVerificationStatus.PASS)
            self.assertEqual(target.get('records'), self.backend.get('records'))
            self.assertEqual(target.get('legacy'), {'winner': 'legacy'})
            print(json.dumps({'semantic_capture_postgres_fixture': {
                'keyed_rows': 1024, 'payload_bytes_per_row': 2048, 'audit_records': 1025,
                'pool_max_size': 1, 'capture_peak_bytes': peak, 'capture_seconds': elapsed,
                **stats,
            }}), flush=True)
        finally:
            case.tearDown()
