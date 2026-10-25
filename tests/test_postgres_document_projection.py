from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from codex_web.storage.postgres_state import PostgresStateStore
from codex_web.storage.data_governance import DataGovernanceStore
from codex_web.storage.organizational_memory import OrganizationalMemoryStore
from tests import test_autonomy_append_scaling as fixtures


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'PostgreSQL test DSN required')
class PostgresDocumentProjectionTests(unittest.TestCase):
    setUp = fixtures.PostgresAutonomyAppendTests.setUp

    def document(self, records=None, **extra):
        return {'schema_version': '1.1', 'records': records or [], 'requests': [], 'events': [], **extra}

    def row(self, identity='selected', organization='local', **extra):
        return dict(id=identity, organization_id=organization, workspace_id='default', **extra)

    def read(self, *, first=False, global_first=False):
        return self.backend.document_array_items_envelope('projection-test', 'records', ('selected',),
            organization_id='local', workspace_id='default', collection_names=('records', 'requests', 'events'),
            first_per_id=first, scope_after_first=global_first)

    def projected(self):
        with self.backend._read_connection() as connection:
            return connection.execute('SELECT ordinal,payload,ctid::text,xmin::text FROM codex_document_record_rows '
                'WHERE namespace=%s ORDER BY ordinal', ('projection-test',)).fetchall()

    def test_initial_insert_and_original_duplicate_scope_order(self):
        self.backend.enable_document_record_projection('projection-test')
        rows = [self.row(organization='foreign', value='global'), self.row(value='local'),
                self.row(value='later', n=-0.0, big=9007199254740993)]
        self.backend.put('projection-test', self.document(rows))
        self.assertEqual(len(self.projected()), 3)
        self.assertEqual(self.read()['records'], rows[1:])
        self.assertEqual(self.read(first=True)['records'], rows[1:2])
        self.assertEqual(self.read(first=True, global_first=True)['records'], [])
        point = self.backend.document_array_item_envelope('projection-test', 'records', 'selected',
            organization_id='local', workspace_id='default', collection_names=('records', 'requests', 'events'))
        self.assertEqual(point['records'], rows[1:2])

    def test_unchanged_array_and_append_preserve_raw_row_identity(self):
        self.backend.enable_document_record_projection('projection-test')
        self.backend.put('projection-test', self.document([self.row(), self.row('peer')]))
        before = self.projected()
        self.backend.update('projection-test', lambda state: {**state, 'events': [{'audit': 'new'}]}, default={})
        self.assertEqual(self.projected(), before)
        self.backend.update('projection-test', lambda state: {**state,
            'records': [*state['records'], self.row('append')]}, default={})
        after = self.projected()
        self.assertEqual(after[:2], before)
        self.assertEqual(len(after), 3)

    def test_old_writer_redaction_erases_raw_rows_before_commit_and_falls_back(self):
        self.backend.enable_document_record_projection('projection-test')
        self.backend.put('projection-test', self.document([self.row(secret='must disappear')]))
        replacement = self.document([self.row(value='redacted')])
        with self.backend._connection() as connection:
            connection.execute('UPDATE codex_state_documents SET payload=%s WHERE namespace=%s',
                (json.dumps(replacement), 'projection-test'))
            self.assertEqual(connection.execute('SELECT count(*) FROM codex_document_record_rows '
                'WHERE namespace=%s', ('projection-test',)).fetchone()[0], 0)
        self.assertEqual(self.read()['records'], replacement['records'])
        self.backend.put('projection-test', replacement)
        self.assertEqual(len(self.projected()), 1)
        self.backend.delete('projection-test')
        self.assertEqual(self.projected(), [])

    def test_document_and_projection_rollback_together(self):
        self.backend.enable_document_record_projection('projection-test')
        original = self.document([self.row(value='before')])
        self.backend.put('projection-test', original)
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with self.backend._connection() as connection:
                with connection.cursor() as cursor:
                    self.backend._lock(cursor, 'projection-test')
                    self.backend._upsert(cursor, 'projection-test', self.document([self.row(value='after')]))
                    raise RuntimeError('rollback')
        self.assertEqual(self.backend.get('projection-test'), original)
        self.assertEqual(self.read()['records'], original['records'])
        self.assertEqual(json.loads(self.projected()[0][1]), original['records'][0])

    def test_read_only_and_callback_constructors_never_enable_or_rebuild(self):
        readonly = PostgresStateStore(self.backend.dsn, connection_pool=self.backend._pool, initialize=False)
        self.addCleanup(readonly.close)
        with patch.object(readonly, '_connection', side_effect=AssertionError('undeclared write')):
            DataGovernanceStore(readonly)
            OrganizationalMemoryStore(readonly)
        def update(state):
            self.assertFalse(self.backend.enable_document_record_projection('projection-test'))
            DataGovernanceStore(self.backend)
            return state
        self.backend.update('callback-test', update, default={})
        self.assertNotIn('projection-test', self.backend._document_projection_namespaces)

    def test_unsupported_identity_and_unknown_envelope_keep_original_fallback(self):
        self.backend.enable_document_record_projection('projection-test')
        self.backend.put('projection-test', self.document([self.row(1e3)]))
        self.assertEqual(self.projected(), [])
        value = self.backend.document_array_items_envelope('projection-test', 'records', ('1000.0',),
            organization_id='local', workspace_id='default', collection_names=('records', 'requests', 'events'))
        self.assertEqual(value['records'][0]['id'], 1000.0)
        original = self.document([self.row()], unexpected={'retained': True})
        self.backend.put('projection-test', original)
        self.assertEqual(self.read(), original)

    def test_missing_or_wrong_fingerprint_keeps_current_canonical_read(self):
        self.backend.enable_document_record_projection('projection-test')
        self.backend.put('projection-test', self.document([self.row(value='current')]))
        with self.backend._connection() as connection:
            connection.execute("UPDATE codex_document_record_headers SET source_sha256=sha256('wrong'::bytea) "
                'WHERE namespace=%s', ('projection-test',))
        self.assertEqual(self.read()['records'][0]['value'], 'current')
        with self.backend._connection() as connection:
            connection.execute('DELETE FROM codex_document_record_headers WHERE namespace=%s', ('projection-test',))
        self.assertEqual(self.read()['records'][0]['value'], 'current')

    def test_concurrent_uncommitted_writer_keeps_one_current_snapshot(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        self.backend.enable_document_record_projection('projection-test')
        self.backend.put('projection-test', self.document([self.row(deny=False)]))
        prepared, release = Event(), Event()
        def writer():
            with self.backend._connection() as connection:
                with connection.cursor() as cursor:
                    self.backend._lock(cursor, 'projection-test')
                    self.backend._upsert(cursor, 'projection-test', self.document([self.row(deny=True)]))
                    prepared.set()
                    if not release.wait(5):
                        raise AssertionError('test writer was not released')
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(writer)
            try:
                self.assertTrue(prepared.wait(5))
                self.assertFalse(self.read()['records'][0]['deny'])
            finally:
                release.set()
            future.result(timeout=5)
        self.assertTrue(self.read()['records'][0]['deny'])

    def test_new_writer_redaction_removes_old_complete_raw_record(self):
        self.backend.enable_document_record_projection('projection-test')
        self.backend.put('projection-test', self.document([self.row(secret='gone'), self.row('peer')]))
        self.backend.put('projection-test', self.document([self.row(value='redacted')]))
        projected = self.projected()
        self.assertEqual(len(projected), 1)
        self.assertNotIn('gone', projected[0][1])
        self.assertEqual(self.read()['records'], [self.row(value='redacted')])

    def test_empty_missing_schema_null_schema_and_nonobjects_preserve_envelope_semantics(self):
        self.backend.enable_document_record_projection('projection-test')
        for schema in ('missing', None, '0.0'):
            with self.subTest(schema=schema):
                state = self.document([None, 'scalar', self.row()])
                if schema == 'missing':
                    state.pop('schema_version')
                else:
                    state['schema_version'] = schema
                self.backend.put('projection-test', state)
                self.assertEqual(self.read(), {'schema_version': '0.0' if schema == 'missing' else schema,
                    'records': [self.row()]})

    def test_concurrent_initialized_owners_serialize_derived_ddl(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        start = Barrier(3)
        def initialize():
            start.wait(timeout=5)
            return PostgresStateStore(self.backend.dsn, connection_pool=self.backend._pool)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(initialize) for _ in range(2)]
            start.wait(timeout=5)
            owners = [future.result(timeout=10) for future in futures]
        self.assertEqual(len(owners), 2)
        with self.backend._read_connection() as connection:
            self.assertEqual(connection.execute("SELECT value FROM codex_state_metadata "
                "WHERE key='document-record-projection-version'").fetchone()[0], '1')
        for owner in owners:
            owner.close()

    def test_direct_writer_document_lock_precedes_new_derived_preparation(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        self.backend.enable_document_record_projection('projection-test')
        self.backend.put('projection-test', self.document([self.row(value='initial')]))
        started = Event()
        def canonical_writer():
            started.set()
            self.backend.put('projection-test', self.document([self.row(value='canonical')]))
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.backend._connection() as connection:
                # Model the old/direct writer: row lock first, then its trigger.
                connection.execute('SELECT payload FROM codex_state_documents WHERE namespace=%s FOR UPDATE',
                    ('projection-test',)).fetchone()
                future = pool.submit(canonical_writer)
                self.assertTrue(started.wait(5))
                connection.execute('UPDATE codex_state_documents SET payload=%s WHERE namespace=%s',
                    (json.dumps(self.document([self.row(value='direct')])), 'projection-test'))
            future.result(timeout=5)
        self.assertEqual(self.read()['records'][0]['value'], 'canonical')

    def test_seed_locks_source_before_deriving_old_raw_against_redaction(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from codex_web.storage import postgres_document_projection
        self.backend.put('projection-test', self.document([self.row(secret='must disappear')]))
        entered, release, writer_started = Event(), Event(), Event()
        original_prepare = postgres_document_projection.prepare
        def paused_prepare(cursor, namespace, serialized):
            entered.set()
            if not release.wait(5):
                raise AssertionError('seed was not released')
            return original_prepare(cursor, namespace, serialized)
        def old_writer():
            writer_started.set()
            with self.backend._connection() as connection:
                connection.execute('UPDATE codex_state_documents SET payload=%s WHERE namespace=%s',
                    (json.dumps(self.document([self.row(value='redacted')])), 'projection-test'))
        with ThreadPoolExecutor(max_workers=2) as pool:
            with patch.object(postgres_document_projection, 'prepare', paused_prepare):
                seeded = pool.submit(self.backend.enable_document_record_projection, 'projection-test')
                try:
                    self.assertTrue(entered.wait(5))
                    # The seed has captured its payload but not yet derived raw
                    # records. Its source lock must already exclude redaction.
                    with self.backend._connection() as connection:
                        with self.assertRaises(Exception) as refused:
                            with connection.transaction():
                                connection.execute('SELECT payload FROM codex_state_documents '
                                    'WHERE namespace=%s FOR UPDATE NOWAIT', ('projection-test',))
                        self.assertEqual(getattr(refused.exception, 'sqlstate', None), '55P03')
                    redacted = pool.submit(old_writer)
                    self.assertTrue(writer_started.wait(5))
                finally:
                    release.set()
                self.assertTrue(seeded.result(timeout=5))
                redacted.result(timeout=5)
        self.assertEqual(self.projected(), [])
        self.assertEqual(self.read()['records'], [self.row(value='redacted')])

    def test_oversized_index_identity_keeps_canonical_write_and_full_reader(self):
        import hashlib
        identities = ('x' * 12800,
            ''.join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(200)))
        self.backend.enable_document_record_projection('projection-test')
        for identity in identities:
            with self.subTest(compressible=identity[0] == 'x'):
                row = self.row(identity, value='large identity remains valid')
                self.backend.put('projection-test', self.document([row]))
                self.assertEqual(self.backend.get('projection-test')['records'], [row])
                selected = self.backend.document_array_items_envelope('projection-test', 'records', (identity,),
                    organization_id='local', workspace_id='default', collection_names=('records', 'requests', 'events'))
                self.assertEqual(selected['records'], [row])
                self.assertEqual(self.projected(), [])
        namespace = 'n' * 2500
        self.assertFalse(self.backend.enable_document_record_projection(namespace))
        self.backend.put(namespace, self.document([self.row()]))
        self.assertEqual(self.backend.document_array_items_envelope(namespace, 'records', ('selected',),
            organization_id='local', workspace_id='default', collection_names=('records', 'requests', 'events'))['records'], [self.row()])
