"""Private synthetic StateStore rows; never use an installed backend or payload."""
import hashlib
import json
import os
import struct
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.postgres_state import PostgresStateStore
from codex_web.storage.storage_preservation import (
    StoragePreservationRefused, StorageSnapshotLimits, StateStorageSnapshotV1,
)
from codex_web.storage.state_store import StorageOperationBudget, storage_operation_budget


class _StorageCases:
    def table(self, logical):
        return ('codex_' if self.kind == 'postgres' else '') + 'state_' + logical

    def insert(self, store, rows, *, metadata=False):
        table = self.table('metadata' if metadata else 'documents')
        columns = 'key,value,updated_at' if metadata else 'namespace,payload,updated_at'
        mark = '%s' if self.kind == 'postgres' else '?'
        with store._connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.executemany(f'INSERT INTO {table}({columns}) VALUES ({mark},{mark},{mark})', rows)
            finally:
                cursor.close()

    def raw_digest(self, store):
        digest = hashlib.sha256()
        # No JSON parsing or accumulating a catalog of payloads.
        for chunk in store.iter_storage_snapshot():
            digest.update(chunk)
        return digest.hexdigest()

    def test_malformed_unknown_lexemes_metadata_roundtrip_without_decode(self):
        source = self.source
        malformed = '{"n": 1 ' + 'x'*6898
        self.insert(source, [('legacy', ' { "n":1e+02, "unknown":[null,"é"] } ', 1.125),
                             ('__codex_records__/usage/__meta__', '{}', 2.25),
                             ('__codex_records__/usage/k/a', malformed, 3.5)])
        self.insert(source, [('extra_metadata', ' 007\né ', 4.125)], metadata=True)
        before = self.raw_digest(source)
        with self.assertRaises(ValueError):
            source.documents()  # Actual old semantic path fails before repair.
        self.target.restore_storage_snapshot(source.iter_storage_snapshot())
        self.assertEqual(before, self.raw_digest(self.target))
        self.assertEqual(before, self.raw_digest(source))
        with self.assertRaises(ValueError):
            self.target.documents()  # Integrity is not semantic recovery PASS.

    def test_truncated_and_tampered_footer_rollback_seed_and_source(self):
        self.insert(self.source, [('a', '{bad', 1.0)])
        before, target_before = self.raw_digest(self.source), self.raw_digest(self.target)
        archive = b''.join(self.source.iter_storage_snapshot())
        for bad in (archive[:-1], archive.replace(b'"rows":2', b'"rows":3'), archive+b'x'):
            with self.subTest(size=len(bad)):
                with self.assertRaises(StoragePreservationRefused):
                    self.target.restore_storage_snapshot([bad])
                self.assertEqual(target_before, self.raw_digest(self.target))
                self.assertEqual(before, self.raw_digest(self.source))

    def test_cancellation_rolls_back_actual_target_transaction(self):
        self.insert(self.source, [('a', 'true', 1.0), ('b', 'false', 2.0)])
        before = self.raw_digest(self.target)
        chunks = list(self.source.iter_storage_snapshot())
        budget = StorageOperationBudget(20)
        def cancelled():
            for index, chunk in enumerate(chunks):
                if index == len(chunks)-1:
                    budget.cancelled.set()
                yield chunk
        token = storage_operation_budget.set(budget)
        try:
            with self.assertRaises(TimeoutError):
                self.target.restore_storage_snapshot(cancelled())
        finally:
            storage_operation_budget.reset(token)
        self.assertEqual(before, self.raw_digest(self.target))

    def test_nonempty_target_and_inventory_limit_refuse_without_mutation(self):
        self.insert(self.source, [('a', '{}', 1.0), ('b', '{}', 2.0)])
        self.insert(self.target, [('retained', '{unknown', 3.0)])
        before = self.raw_digest(self.target)
        with self.assertRaisesRegex(StoragePreservationRefused, 'not_empty'):
            self.target.restore_storage_snapshot(self.source.iter_storage_snapshot())
        with self.assertRaisesRegex(StoragePreservationRefused, 'inventory_limit'):
            list(self.source.iter_storage_snapshot(limits=StorageSnapshotLimits(max_rows=1)))
        self.assertEqual(before, self.raw_digest(self.target))

    def test_separate_writer_between_fetches_cannot_mix_tables(self):
        self.insert(self.source, [('a', '{"original":1}', 1.0)])
        baseline = self.raw_digest(self.source)
        generator = self.source.iter_storage_snapshot()
        prefix = [next(generator), next(generator)]  # Header then first row length; snapshot established.
        self.insert(self.writer, [('later', '{"concurrent":1}', 5.0)])
        self.insert(self.writer, [('later_meta', 'concurrent', 6.0)], metadata=True)
        def remaining():
            yield from prefix
            yield from generator
        self.target.restore_storage_snapshot(remaining())
        self.assertEqual(baseline, self.raw_digest(self.target))
        self.assertNotEqual(baseline, self.raw_digest(self.source))

    def test_unknown_version_and_foreign_backend_refused_before_commit(self):
        before = self.raw_digest(self.target)
        archive = b''.join(self.source.iter_storage_snapshot())
        foreign = 'sqlite' if self.kind == 'postgres' else 'postgres'
        for value in (archive.replace(b'"format_version":"1.0"', b'"format_version":"9.0"'),):
            with self.assertRaises(StoragePreservationRefused):
                self.target.restore_storage_snapshot([value])
        from codex_web.storage.storage_preservation import export_rows
        chunks = export_rows(StateStorageSnapshotV1(backend=foreign, storage_schema_version=1), [], StorageSnapshotLimits())
        with self.assertRaisesRegex(StoragePreservationRefused, 'backend_or_schema'):
            self.target.restore_storage_snapshot(chunks)
        self.assertEqual(before, self.raw_digest(self.target))

    def crypto_fixture(self):
        from codex_web.services.crypto_keys import CryptoKeyService
        from codex_web.storage.crypto_keys import CryptoKeyStore
        from codex_web.key_backends import LocalFileKeyBackend
        from codex_web.crypto import ManagedKeyCreate, KeyPurpose, EncryptionContext
        from codex_web.identity import AuthenticationActor, AuthenticationAssurance, PrincipalKind, MembershipRole
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        crypto = CryptoKeyService(CryptoKeyStore(self.source),{'local':LocalFileKeyBackend(Path(temporary.name)/'keys')})
        actor = AuthenticationActor(identity_id='fixture-admin',principal_kind=PrincipalKind.HUMAN,
            organization_id='org',workspace_id='ws',roles=(MembershipRole.ADMIN,),assurance=AuthenticationAssurance.MFA)
        key = crypto.create_key(ManagedKeyCreate(purpose=KeyPurpose.BACKUP),actor=actor)
        context = EncryptionContext(organization_id='org',workspace_id='ws',
            object_type='recovery_storage_preservation',object_id='fixture-'+uuid.uuid4().hex)
        return crypto,actor,key,context

    def test_encrypted_roundtrip_closes_source_before_kms_batch_audit_pool1(self):
        from codex_web.services.recovery_storage_preservation import seal_storage_snapshot, open_storage_snapshot
        crypto,actor,key,context = self.crypto_fixture()
        self.insert(self.source,[('opaque','{broken',1.0)])
        before = self.raw_digest(self.source)
        with tempfile.TemporaryFile() as content:
            for chunk in seal_storage_snapshot(self.source.iter_storage_snapshot(),crypto=crypto,key_id=key.id,context=context,actor=actor):
                content.write(chunk)
            content.seek(0)
            self.target.restore_storage_snapshot(open_storage_snapshot(iter(lambda:content.read(65536),b''),crypto=crypto,context=context,actor=actor))
        self.assertEqual(before,self.raw_digest(self.target))
        # Only the mandatory canonical crypto audit changes source; never call
        # raw byte preservation a promise of zero authorized audit appends.
        self.assertNotEqual(before,self.raw_digest(self.source))
        self.assertEqual(crypto.store.load().events[-1].operation,'decrypt_batch')

    def test_encrypted_missing_manifest_refuses_atomic_target_success(self):
        from codex_web.services.recovery_storage_preservation import seal_storage_snapshot, open_storage_snapshot
        crypto,actor,key,context = self.crypto_fixture()
        self.insert(self.source,[('opaque','{broken',1.0)])
        before = self.raw_digest(self.target)
        chunks = list(seal_storage_snapshot(self.source.iter_storage_snapshot(),crypto=crypto,key_id=key.id,context=context,actor=actor))
        with self.assertRaises(StoragePreservationRefused):
            self.target.restore_storage_snapshot(open_storage_snapshot(chunks[:-1],crypto=crypto,context=context,actor=actor))
        self.assertEqual(before,self.raw_digest(self.target))

    @unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_PRESERVATION_SCALE') == '1', 'explicit serialized full-shape operator job')
    def test_full_current_shape_crosses_old_row_and_value_limits(self):
        count, total, largest = 118389, 294508748, 12373762
        malformed_size = 6906
        ordinary, remainder = divmod(total-largest-malformed_size, count-2)
        def rows():
            yield ('000000', '"'+'A'*(largest-2)+'"', 1.0)
            yield ('000001', '{'+'?'*(malformed_size-1), 2.0)
            for index in range(count-2):
                size = ordinary + (index < remainder)
                yield (f'{index+2:06}', '"'+'b'*(size-2)+'"', float(index+3))
        iterator = iter(rows())
        while True:
            import itertools
            batch = list(itertools.islice(iterator,256))
            if not batch:
                break
            self.insert(self.source, batch)
        with self.source._connection() as connection:
            cursor = connection.cursor()
            length = 'octet_length(payload)' if self.kind == 'postgres' else 'length(CAST(payload AS BLOB))'
            cursor.execute(f'SELECT COUNT(*),SUM({length}),MAX({length}) FROM {self.table("documents")}')
            self.assertEqual(tuple(cursor.fetchone()),(count,total,largest))
            cursor.close()
        before = self.raw_digest(self.source)
        with tempfile.TemporaryFile() as archive:
            for chunk in self.source.iter_storage_snapshot():
                archive.write(chunk)
            archive.seek(0)
            self.target.restore_storage_snapshot(iter(lambda:archive.read(65536),b''))
        self.assertEqual(before,self.raw_digest(self.target))
        self.assertEqual(before,self.raw_digest(self.source))


class SQLiteStoragePreservationTests(_StorageCases, unittest.TestCase):
    kind = 'sqlite'
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = SQLiteStateStore(root/'source.db')
        self.writer = SQLiteStateStore(root/'source.db')
        self.target = SQLiteStateStore(root/'target.db')


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'isolated PostgreSQL DSN required')
class PostgresStoragePreservationTests(_StorageCases, unittest.TestCase):
    kind = 'postgres'
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg_pool import ConnectionPool
        self.dsn = os.environ['CODEX_WEB_TEST_POSTGRES_DSN']
        def new_store(schema):
            with psycopg.connect(self.dsn) as conn:
                conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            def drop():
                with psycopg.connect(self.dsn) as conn:
                    conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
            self.addCleanup(drop)
            pool = ConnectionPool(self.dsn,min_size=1,max_size=1,timeout=.5,
                                  kwargs={'options':'-c search_path='+schema})
            self.addCleanup(pool.close)
            store = PostgresStateStore(self.dsn,connection_pool=pool)
            self.addCleanup(store.close)
            return store
        schema = 'preserve1530_'+uuid.uuid4().hex
        self.source = new_store(schema)
        self.target = new_store(schema+'_target')
        writer_pool = ConnectionPool(self.dsn,min_size=1,max_size=1,timeout=.5,
                                     kwargs={'options':'-c search_path='+schema})
        self.addCleanup(writer_pool.close)
        self.writer = PostgresStateStore(self.dsn,connection_pool=writer_pool)
        self.addCleanup(self.writer.close)

    def test_callback_refuses_without_nested_pool_checkout(self):
        with self.source._connection() as connection:
            def callback(_):
                with self.assertRaisesRegex(StoragePreservationRefused,'own_transaction'):
                    next(self.source.iter_storage_snapshot())
            self.source._run_updater(connection,callback,None)
