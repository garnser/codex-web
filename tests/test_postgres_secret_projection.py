"""Metadata-only Secret references share the hash-bound optional SQL index."""
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from codex_web.secrets import SecretAuditEvent, SecretReference, SecretState
from codex_web.storage.postgres_state import PostgresStateStore
from codex_web.storage.secret_state import SecretStateStore
from tests import test_autonomy_append_scaling as pg_fixture
from tests import test_secret_metadata_point_read as metadata_fixture
from tests import test_secrets_broker as broker_fixture


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'PostgreSQL test DSN required')
class PostgresSecretProjectionTests(unittest.TestCase):
    setUp = pg_fixture.PostgresAutonomyAppendTests.setUp

    def reference(self, **changes):
        return SecretReference(id='secret-selected', organization_id='local', workspace_id='default',
            name='Synthetic credential metadata', **changes)

    def state(self, references=None, audit=None):
        return SecretState(references=references or [], audit=audit or []).model_dump(mode='json')

    def enable(self):
        return self.backend.enable_document_array_projection('secret_metadata', array_names=('references',))

    def read(self, *, backend=None, identity='secret-selected'):
        return (backend or self.backend).document_array_items_envelope('secret_metadata','references',(identity,),
            organization_id='local',workspace_id='default',collection_names=('references','audit'),
            first_per_id=True,scope_after_first=True)

    def rows(self):
        with self.backend._read_connection() as connection:
            return connection.execute('SELECT ordinal,payload,ctid::text,xmin::text '
                'FROM codex_document_record_rows WHERE namespace=%s ORDER BY ordinal', ('secret_metadata',)).fetchall()

    def test_initialized_store_indexes_only_references_not_audit_or_material(self):
        store = SecretStateStore(self.backend)
        reference = self.reference()
        audit = [SecretAuditEvent(id='audit-'+str(i),secret_id=reference.id,organization_id='local',
            workspace_id='default',actor_identity_id='worker',actor_kind='service',action='metadata',
            outcome='allowed',reason='AUDIT_NOT_PROJECTED_'+'x'*256) for i in range(2000)]
        self.backend.put(store.namespace,self.state([reference],audit))
        rows=self.rows();self.assertEqual(len(rows),1)
        self.assertEqual(rows[0][0],4)
        self.assertNotIn('AUDIT_NOT_PROJECTED', rows[0][1])
        self.assertNotIn('"value"',rows[0][1])
        self.assertEqual(json.loads(rows[0][1]),reference.model_dump(mode='json'))
        decoded=[];decode=store._decode
        def counted(payload):
            decoded.append(len(payload.get('audit',[])))
            return decode(payload)
        with patch.object(store,'_decode',side_effect=counted),patch.object(self.backend,'get',side_effect=AssertionError('unexpected full fallback')):
            self.assertEqual(store.reference_state(reference.id,organization_id='local',workspace_id='default').references,[reference])
        self.assertEqual(decoded,[0])

    def test_audit_append_updates_hash_without_rewriting_reference_rows(self):
        store=SecretStateStore(self.backend);reference=self.reference()
        self.backend.put(store.namespace,self.state([reference]))
        before=self.rows()
        event=SecretAuditEvent(secret_id=reference.id,organization_id='local',workspace_id='default',
            actor_identity_id='worker',actor_kind='service',action='use',outcome='allowed')
        store.update(lambda state:state.model_copy(update={'audit':[event]}))
        self.assertEqual(self.rows(),before)
        with self.backend._read_connection() as connection:
            self.assertEqual(connection.execute('SELECT h.source_sha256=sha256(convert_to(d.payload,\'UTF8\')) '
                'FROM codex_document_record_headers h JOIN codex_state_documents d USING(namespace) '
                'WHERE h.namespace=%s',('secret_metadata',)).fetchone(),(True,))
        with patch.object(self.backend,'get',side_effect=AssertionError('audit append must keep current indexed lookup')):
            self.assertEqual(self.read()['references'],[reference.model_dump(mode='json')])

    def test_first_global_foreign_shadow_and_same_scope_duplicates_keep_order(self):
        self.enable();reference=self.reference();foreign=reference.model_copy(update={'organization_id':'foreign'})
        later=reference.model_copy(update={'rotation':9})
        self.backend.put('secret_metadata',self.state([foreign,reference,later]))
        self.assertEqual(self.read()['references'],[])
        # The canonical owner must load the original global first reference so
        # SecretBroker can retain its tenant denial, rather than select local.
        self.assertEqual(SecretStateStore(self.backend).reference_state(reference.id,
            organization_id='local',workspace_id='default').references[0],foreign)
        self.backend.put('secret_metadata',self.state([reference,later]))
        self.assertEqual(self.read()['references'],[reference.model_dump(mode='json')])
        self.assertEqual(self.read(identity='missing')['references'],[])

    def test_reference_rotation_revocation_removal_and_empty_state_are_fresh(self):
        store=SecretStateStore(self.backend);reference=self.reference()
        self.backend.put(store.namespace,self.state([reference]))
        for change in ({'rotation':1},{'revoked_at':1},{'allowed_identity_ids':['worker']}):
            reference=reference.model_copy(update=change)
            self.backend.put(store.namespace,self.state([reference]))
            self.assertEqual(self.read()['references'],[reference.model_dump(mode='json')])
        self.backend.put(store.namespace,self.state())
        self.assertEqual(self.rows(),[]);self.assertEqual(self.read()['references'],[])

    def test_direct_old_writer_erases_reference_copy_in_same_transaction(self):
        self.enable();reference=self.reference();self.backend.put('secret_metadata',self.state([reference]))
        self.assertEqual(len(self.rows()),1)
        changed=self.state([reference.model_copy(update={'revoked_at':1})])
        with self.backend._connection() as connection:
            connection.execute('UPDATE codex_state_documents SET payload=%s WHERE namespace=%s',
                (json.dumps(changed),'secret_metadata'))
            self.assertEqual(connection.execute('SELECT count(*) FROM codex_document_record_rows '
                'WHERE namespace=%s',('secret_metadata',)).fetchone()[0],0)
        self.assertEqual(self.read()['references'],changed['references'])

    def test_unknown_reference_marker_and_unknown_format_preserve_canonical_fallback(self):
        self.enable();reference=self.reference();self.backend.put('secret_metadata',self.state([reference]))
        for index,key in enumerate(('document-reference-projection-v2:secret_metadata','document-array-projection-format')):
            with self.subTest(key=key):
                with self.backend._connection() as connection:
                    connection.execute("UPDATE codex_state_metadata SET value='999' WHERE key=%s",(key,))
                self.assertFalse(self.enable())
                self.backend.put('secret_metadata',self.state([reference.model_copy(update={'rotation':8+index})]))
                self.assertEqual(self.rows(),[])
                self.assertEqual(self.read()['references'][0]['rotation'],8+index)
                with self.backend._connection() as connection:
                    self.assertEqual(connection.execute('SELECT value FROM codex_state_metadata WHERE key=%s',(key,)).fetchone()[0],'999')
                    connection.execute("UPDATE codex_state_metadata SET value='2' WHERE key=%s",(key,))

    def test_malformed_header_nonstring_and_oversized_encoded_ids_keep_full_reader(self):
        self.enable();valid=self.state([self.reference()])
        for changed in ({**valid,'references':{}},{**valid,'audit':{}},{**valid,'unexpected':True},
                        {**valid,'schema_version':1.0}):
            with self.subTest(changed=tuple(changed)):
                self.backend.put('secret_metadata',changed)
                self.assertEqual(self.read(),changed)
        for identity in (17.0,'"\\'*6000):
            reference={**valid['references'][0],'id':identity}
            self.backend.put('secret_metadata',{**valid,'references':[reference]})
            self.assertEqual(self.rows(),[])
            self.assertEqual(self.read(identity=str(identity))['references'],[reference])

    def test_readonly_callback_and_opposite_group_never_enable(self):
        readonly=PostgresStateStore(self.backend.dsn,connection_pool=self.backend._pool,initialize=False)
        self.addCleanup(readonly.close)
        with patch.object(readonly,'_connection',side_effect=AssertionError('readonly constructor write')):
            SecretStateStore(readonly)
        def callback(state):
            SecretStateStore(self.backend)
            return state
        self.backend.update('callback',callback,default={})
        self.assertTrue(self.enable())
        self.assertFalse(self.backend.enable_document_array_projection('secret_metadata',array_names=('artifacts','evidence','verifications')))
        with self.backend._read_connection() as connection:
            self.assertIsNone(connection.execute('SELECT value FROM codex_state_metadata WHERE key=%s',
                ('document-array-projection-v2:secret_metadata',)).fetchone())
            self.assertEqual(connection.execute("SELECT value FROM codex_state_metadata WHERE key='document-record-projection-version'").fetchone()[0],'1')

    def test_old92_initializer_cannot_overwrite_reference_helper_or_refresh_stale_rows(self):
        directory=os.environ.get('CODEX_WEB_TEST_LEGACY_SECRET_PROJECTION_SOURCE')
        if not directory:
            self.skipTest('Pinned actual92a helper/store supplied by private compatibility qualification')
        def load(name,filename):
            spec=importlib.util.spec_from_file_location(name,Path(directory)/filename)
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
        helper=load('secret_old92_helper','postgres_document_projection.py')
        old_module=load('secret_old92_store','postgres_state.py');old_module.postgres_document_projection=helper
        store=SecretStateStore(self.backend);reference=self.reference()
        self.backend.put(store.namespace,self.state([reference]))
        old=old_module.PostgresStateStore(self.backend.dsn,connection_pool=self.backend._pool)
        self.addCleanup(old.close)
        changed=reference.model_copy(update={'rotation':7,'revoked_at':1})
        self.backend.put(store.namespace,self.state([changed]))
        with patch.object(self.backend,'get',side_effect=AssertionError('new helper must survive old initialization')):
            self.assertEqual(self.read()['references'],[changed.model_dump(mode='json')])
        self.assertEqual(json.loads(self.rows()[0][1]),changed.model_dump(mode='json'))
        replacement=reference.model_copy(update={'rotation':9})
        old.put(store.namespace,self.state([replacement]))
        self.assertEqual(self.rows(),[])
        self.assertEqual(self.read()['references'],[replacement.model_dump(mode='json')])
        with self.backend._read_connection() as connection:
            self.assertIsNotNone(connection.execute("SELECT to_regprocedure('codex_prepare_document_references_v2(text,text)')").fetchone()[0])
            self.assertIsNone(connection.execute('SELECT value FROM codex_state_metadata WHERE key=%s',
                ('document-array-projection-v2:secret_metadata',)).fetchone())


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'PostgreSQL test DSN required')
class PostgresSecretMetadataOwnerTests(metadata_fixture.SecretMetadataPointReadTests):
    def setUp(self):
        pg_fixture.PostgresAutonomyAppendTests.setUp(self)
        backend=self.backend
        # Keep the existing complete canonical broker/identity/material fixture;
        # only its private persistence backend is PostgreSQL for these controls.
        with patch.object(broker_fixture,'SQLiteStateStore',lambda unused:backend):
            broker_fixture.SecretBrokerTests.setUp(self)
