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

    def sql_rows(self,store):
        # Independent oracle: direct physical SQL, not export/import framing,
        # canonical(), parser normalization, documents() or raw_digest().
        pg=self.kind=='postgres'
        with store._connection(**({'read_only_snapshot':True} if pg else {})) as connection:
            if not pg:connection.execute('BEGIN')
            for logical,key,value in [('documents','namespace','payload'),('metadata','key','value')]:
                cursor=connection.cursor(name='oracle_'+uuid.uuid4().hex) if pg else connection.cursor()
                try:
                    collate=' COLLATE "C"' if pg else ' COLLATE BINARY'
                    cursor.execute(f'SELECT {key},{value},updated_at FROM {self.table(logical)} ORDER BY {key}{collate}')
                    while True:
                        batch=cursor.fetchmany(64)
                        if not batch:break
                        for k,v,t in batch:yield (logical,k,v,t)
                finally:cursor.close()

    def sql_digest(self,store):
        digest=hashlib.sha256()
        for table,key,value,timestamp in self.sql_rows(store):
            for text in (table,key,value):
                raw=text.encode('utf8');digest.update(struct.pack('>Q',len(raw)));digest.update(raw)
            digest.update(struct.pack('>d',timestamp))
        return digest.hexdigest()

    def test_malformed_unknown_lexemes_metadata_roundtrip_without_decode(self):
        source = self.source
        malformed = '{"n": 1 ' + 'x'*6898
        self.insert(source, [('legacy', ' { "n":1e+02, "unknown":[null,"é"] } ', 1.125),
                             ('__codex_records__/usage/__meta__', '{}', 2.25),
                             ('__codex_records__/usage/k/a', malformed, 3.5)])
        self.insert(source, [('extra_metadata', ' 007\né ', 4.125)], metadata=True)
        expected_sql=list(self.sql_rows(source))
        before = self.raw_digest(source)
        with self.assertRaises(ValueError):
            source.documents()  # Actual old semantic path fails before repair.
        self.target.restore_storage_snapshot(source.iter_storage_snapshot())
        self.assertEqual(before, self.raw_digest(self.target))
        self.assertEqual(list(self.sql_rows(self.target)),expected_sql)
        self.assertIn(('documents','legacy',' { "n":1e+02, "unknown":[null,"é"] } ',1.125),expected_sql)
        self.assertIn(('metadata','extra_metadata',' 007\né ',4.125),expected_sql)
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
        expected_sql=self.sql_digest(self.source)
        before = self.raw_digest(self.source)
        with tempfile.TemporaryFile() as content:
            for chunk in seal_storage_snapshot(self.source.iter_storage_snapshot(),crypto=crypto,key_id=key.id,context=context,actor=actor):
                content.write(chunk)
            content.seek(0)
            self.target.restore_storage_snapshot(open_storage_snapshot(iter(lambda:content.read(65536),b''),crypto=crypto,context=context,actor=actor))
        self.assertEqual(before,self.raw_digest(self.target))
        self.assertEqual(expected_sql,self.sql_digest(self.target))
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

    def test_malformed_envelope_errors_are_payload_free_and_target_rolls_back(self):
        import traceback
        from codex_web.services.recovery_storage_preservation import seal_storage_snapshot,open_storage_snapshot,MAGIC,_frame
        crypto,actor,key,context=self.crypto_fixture()
        self.insert(self.source,[('opaque','{broken',1.125)])
        valid=b''.join(seal_storage_snapshot(self.source.iter_storage_snapshot(),crypto=crypto,key_id=key.id,context=context,actor=actor))
        frames=[];position=len(MAGIC)
        while position<len(valid):
            size=struct.unpack('>Q',valid[position:position+8])[0]
            frames.append(json.loads(valid[position+8:position+8+size]));position+=8+size
        before=self.sql_digest(self.target)
        sentinel='SYNTHETIC_PRIVATE_ENVELOPE_SENTINEL_1530'
        for kind in ('chunk','manifest'):
            for bad in ({'extra':sentinel},sentinel,None):
                with self.subTest(kind=kind,bad_type=type(bad).__name__):
                    records=[dict(item) for item in frames]
                    selected=next(item for item in records if item.get('kind')==kind)
                    selected['envelope']=bad
                    wire=MAGIC+b''.join(_frame(item) for item in records)
                    with self.assertRaises(StoragePreservationRefused) as caught:
                        self.target.restore_storage_snapshot(open_storage_snapshot([wire],crypto=crypto,context=context,actor=actor))
                    error=caught.exception
                    self.assertEqual(str(error),'invalid_encrypted_envelope')
                    self.assertIsNone(error.__context__)
                    self.assertNotIn(sentinel,''.join(traceback.format_exception(error)))
                    self.assertNotIn(sentinel,json.dumps({'detail':str(error)}))
                    self.assertEqual(self.sql_digest(self.target),before)

    def test_bounded_restore_batches_and_cancellation_roll_back_prior_inserts(self):
        from unittest.mock import patch
        self.insert(self.source,[(f'batch-{i:04}','{"n":1e+02}',i+.125) for i in range(600)])
        source_before=self.sql_digest(self.source);target_before=self.sql_digest(self.target)
        original=self.target._connection
        budget=StorageOperationBudget(20);calls=[]
        class Cursor:
            def __init__(self,real):self.real=real
            def __getattr__(self,name):return getattr(self.real,name)
            def executemany(self,query,rows):
                calls.append((len(rows),sum(len(k.encode('utf8'))+len(v.encode('utf8'))+8 for k,v,t in rows)))
                if len(calls)==2:budget.cancelled.set()
                return self.real.executemany(query,rows)
        class Connection:
            def __init__(self,real):self.real=real
            def __getattr__(self,name):return getattr(self.real,name)
            def cursor(self,*args,**kwargs):return Cursor(self.real.cursor(*args,**kwargs))
        @contextmanager
        def owning(*args,**kwargs):
            with original(*args,**kwargs) as connection:yield Connection(connection)
        token=storage_operation_budget.set(budget)
        try:
            with patch.object(self.target,'_connection',owning):
                with self.assertRaises(TimeoutError):self.target.restore_storage_snapshot(self.source.iter_storage_snapshot())
        finally:storage_operation_budget.reset(token)
        self.assertEqual(len(calls),2)
        self.assertTrue(all(count<=256 and size<=4*1024**2 for count,size in calls))
        self.assertEqual(self.sql_digest(self.target),target_before)
        self.assertEqual(self.sql_digest(self.source),source_before)
        # Successful real batch execution retains exact physical values; a
        # footer failure after earlier batch inserts still rolls everything back.
        wire=b''.join(self.source.iter_storage_snapshot())
        with self.assertRaises(StoragePreservationRefused):self.target.restore_storage_snapshot([wire[:-1]])
        self.assertEqual(self.sql_digest(self.target),target_before)
        self.target.restore_storage_snapshot([wire])
        self.assertEqual(self.sql_digest(self.target),source_before)

    def test_isolated_target_growth_limit_rolls_back_real_bounded_insert(self):
        self.insert(self.source, [('growth-target','{"unchanged":true}',9.5)])
        source_before=self.sql_digest(self.source);target_before=self.sql_digest(self.target)
        with self.assertRaisesRegex(StoragePreservationRefused,'^isolated_target_growth_limit$'):
            self.target.restore_storage_snapshot(self.source.iter_storage_snapshot(),max_target_bytes=1)
        self.assertEqual(self.sql_digest(self.target),target_before)
        self.assertEqual(self.sql_digest(self.source),source_before)

    @unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_PRESERVATION_SCALE') == '1', 'explicit serialized full-shape operator job')
    def test_full_current_shape_crosses_old_row_and_value_limits(self):
        import time
        start=time.perf_counter();phases={}
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
        phases['seed']=time.perf_counter()-start
        print(json.dumps({'phase':'seed','seconds':phases['seed']}),flush=True)
        phase=time.perf_counter()
        before = self.sql_digest(self.source)
        phases['source_sql_oracle']=time.perf_counter()-phase
        print(json.dumps({'phase':'source_sql_oracle','seconds':phases['source_sql_oracle']}),flush=True)
        with tempfile.TemporaryFile() as archive:
            phase=time.perf_counter()
            for chunk in self.source.iter_storage_snapshot():
                archive.write(chunk)
            phases['export']=time.perf_counter()-phase
            print(json.dumps({'phase':'export','seconds':phases['export']}),flush=True)
            archive.seek(0)
            phase=time.perf_counter()
            self.target.restore_storage_snapshot(iter(lambda:archive.read(65536),b''))
            phases['restore']=time.perf_counter()-phase
            print(json.dumps({'phase':'restore','seconds':phases['restore']}),flush=True)
        self.assertEqual(before,self.sql_digest(self.target))
        self.assertEqual(before,self.sql_digest(self.source))

        print(json.dumps({'kind':'raw_full_shape','backend':self.kind,'rows':count,'payload_bytes':total,'largest':largest,'phase_seconds':phases,'total_seconds':time.perf_counter()-start}))


class SQLiteStoragePreservationTests(_StorageCases, unittest.TestCase):
    kind = 'sqlite'
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = SQLiteStateStore(root/'source.db')
        self.writer = SQLiteStateStore(root/'source.db')
        self.target = SQLiteStateStore(root/'target.db')

    def test_unrelated_sqlite_failure_is_not_relabelled_as_budget_cancel(self):
        import sqlite3
        before=self.sql_digest(self.target)
        def invalid_sql():
            with self.source._connection() as connection:
                connection.execute('SELECT impossible FROM fixture_table_does_not_exist')
            yield b'unreachable'
        with self.assertRaisesRegex(sqlite3.OperationalError,'no such table'):
            self.target.restore_storage_snapshot(invalid_sql())
        self.assertEqual(self.sql_digest(self.target),before)


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
