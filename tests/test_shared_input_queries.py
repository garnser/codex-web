"""The same exact scoped query contract on canonical SQLite and real CI PostgreSQL."""
import tempfile
from pathlib import Path
import unittest

from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class SharedInputQueryContract:
    def query_store(self):
        return ArtifactEvidenceStore(self.backend)

    @staticmethod
    def query_row(identifier,**change):
        return dict(id=identifier,organization_id='local',workspace_id='default',project_id='project-a',
            work_item_ref='group/target!33',provider='governed-artifact-sharing.v1',
            source='worker-governed-artifact-sharing',**change)

    @staticmethod
    def query_scope():
        return dict(organization_id='local',workspace_id='default',project_id='project-a',
            work_item_ref='group/target!33',provider='governed-artifact-sharing.v1',source='worker-governed-artifact-sharing')

    def test_shared_input_query_filters_before_limit_and_pages_newest_first(self):
        store=self.query_store()
        matching=[self.query_row('selected-'+str(i)) for i in range(35)]
        unrelated=[]
        for field,value in (('organization_id','foreign'),('workspace_id','foreign'),('project_id','foreign'),
                ('work_item_ref','group/other!1'),('provider','other.v1'),('source','other')):
            unrelated.extend({**self.query_row(field+str(i)),field:value} for i in range(40))
        self.backend.put(store.namespace,{'schema_version':'1.5','artifacts':matching+unrelated,
            'evidence':[self.query_row('evidence-only')],'verifications':[]})
        page=store.shared_artifact_candidates(**self.query_scope())
        self.assertEqual([r['id'] for r in page['candidates']],['selected-'+str(i) for i in range(34,2,-1)])
        self.assertTrue(page['more'])
        rest=store.shared_artifact_candidates(**self.query_scope(),before=page['candidates'][-1]['ordinal'])
        self.assertEqual([r['id'] for r in rest['candidates']],['selected-2','selected-1','selected-0'])
        self.assertFalse(rest['more'])
        self.assertEqual(store.shared_artifact_candidates(**{**self.query_scope(),'work_item_ref':'missing!1'})['candidates'],[])

    def test_shared_input_query_rejects_schema_and_duplicate_scoped_ids(self):
        store=self.query_store()
        document={'schema_version':'1.5','artifacts':[self.query_row('same'),self.query_row('same')],
            'evidence':[],'verifications':[]}
        self.backend.put(store.namespace,document)
        with self.assertRaisesRegex(ValueError,'candidate'):
            store.shared_artifact_candidates(**self.query_scope())
        self.backend.put(store.namespace,{**document,'schema_version':'999.0','artifacts':[]})
        with self.assertRaises(ValueError):store.shared_artifact_candidates(**self.query_scope())
        self.backend.put(store.namespace,{**document,'artifacts':{},'schema_version':'1.5'})
        with self.assertRaises(ValueError):store.shared_artifact_candidates(**self.query_scope())

    def test_shared_input_query_refuses_malformed_selected_identity_and_cursor_bound(self):
        store=self.query_store()
        self.backend.put(store.namespace,{'schema_version':'1.5','artifacts':[self.query_row('x'*201)],
            'evidence':[],'verifications':[]})
        with self.assertRaisesRegex(ValueError,'candidate'):store.shared_artifact_candidates(**self.query_scope())
        with self.assertRaisesRegex(ValueError,'bound'):
            store.shared_artifact_candidates(**self.query_scope(),before=2**63)


class SQLiteSharedInputQueryTests(SharedInputQueryContract,unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.backend=SQLiteStateStore(Path(temp.name)/'private.sqlite')
