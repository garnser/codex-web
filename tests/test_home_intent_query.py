"""Original-row Home recovery predicate on isolated SQLite/PostgreSQL stores."""
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from codex_web.storage.home_intent_query import HomeIntentRecoveryQuery, HomeCommandIdentity
from codex_web.storage.sqlite_state import SQLiteStateStore


class HomeIntentQueryContract:
    namespace='action_intents.records.v1'
    def intent(self, key, **changes):
        value=dict(id=key,organization_id='org',workspace_id='ws',project_id='home',
            requested_by='admin',provider_type='home-operator',provider_instance='trusted-host',
            action_id='operator.host.command',resource_ids=['host'],execution_id='same-native',
            status='succeeded',attempt=1,idempotency_key='key-'+key,correlation_id='correlation-'+key,execution_started_at=10,last_receipt_id='receipt-'+key,
            request={'parameters':{'argv':['/usr/bin/read',"a' OR 1=1 --",'å']}})
        value.update(changes)
        return value
    def query(self, execution=False):
        if execution:
            return HomeIntentRecoveryQuery(organization_id='org',workspace_id='ws',execution_id='same-native')
        return HomeIntentRecoveryQuery(organization_id='org',workspace_id='ws',command=HomeCommandIdentity(
            requested_by='admin',provider_type='home-operator',provider_instance='trusted-host',
            resource_ids=('host',),argv=('/usr/bin/read',"a' OR 1=1 --",'å')))
    def write(self, items):
        rows={}
        for item in items:
            rows['intents:'+item['id']]=item
            if item.get('last_receipt_id'):
                rows['receipts:'+item['id']+':000000000001.000000:'+item['last_receipt_id']]={
                    'id':item['last_receipt_id'],'intent_id':item['id'],'outcome':item.pop('_receipt_outcome','completed'),
                    **{field:item[field] for field in ('attempt','provider_type','provider_instance','action_id','idempotency_key','correlation_id')}}
        self.backend.record_apply(self.namespace,upserts=rows)

    def test_settled_history_over_256_does_not_consume_unresolved_budget(self):
        items=[self.intent('settled-'+str(i)) for i in range(300)]
        self.write(items)
        for mode in (False,True):
            self.assertEqual(self.backend.home_intent_recovery_rows(self.namespace,self.query(mode)),[])
        held=self.intent('old-unknown',status='failed',_receipt_outcome='unknown')
        self.write([held])
        # Missing arbitrary derived aliases cannot prove original absence.
        self.backend.record_apply(self.namespace,upserts={'home-command:old':{'id':held['id']}})
        self.backend.record_apply(self.namespace,upserts={},deletes=('home-command:old',))
        for mode in (False,True):
            found=self.backend.home_intent_recovery_rows(self.namespace,self.query(mode))
            self.assertEqual([i['id'] for i in found],['old-unknown'])

    def test_every_unresolved_status_and_missing_or_unknown_receipt_remains_visible(self):
        items=[self.intent('state-'+s,status=s,last_receipt_id=None,execution_started_at=None)
            for s in ('pending','claimed','executing','uncertain','requires_reconciliation')]
        items.extend([self.intent('terminal-missing',status='failed'),
            self.intent('terminal-no-receipt',status='failed',last_receipt_id=None)])
        self.write(items)
        self.backend.record_apply(self.namespace,upserts={},
            deletes=('receipts:terminal-missing:000000000001.000000:receipt-terminal-missing',))
        found=self.backend.home_intent_recovery_rows(self.namespace,self.query())
        self.assertEqual({i['id'] for i in found},{i['id'] for i in items})

    def test_exact_parameters_scope_and_no_derived_alias_authority(self):
        same=self.intent('same',status='uncertain')
        foreign=self.intent('foreign',status='uncertain',requested_by='someone-else')
        other=self.intent('other',status='uncertain',execution_id='other-native',resource_ids=['other-host'])
        self.write([same,foreign,other])
        found=self.backend.home_intent_recovery_rows(self.namespace,self.query())
        self.assertEqual([i['id'] for i in found],['same'])
        injection=HomeIntentRecoveryQuery(organization_id="org' OR 1=1 --",workspace_id='ws',execution_id='same-native')
        self.assertEqual(self.backend.home_intent_recovery_rows(self.namespace,injection),[])

    def test_malformed_receipt_identity_and_outcome_never_count_as_positive(self):
        item=self.intent('malformed')
        self.write([item])
        key='receipts:malformed:000000000001.000000:receipt-malformed'
        raw=self.backend.record_get(self.namespace,key)
        for change in ({'outcome':'unknown'},{'outcome':None},{'intent_id':'other'},{'action_id':'other'},{'attempt':2}):
            self.backend.record_apply(self.namespace,upserts={key:{**raw,**change}})
            self.assertEqual([i['id'] for i in self.backend.home_intent_recovery_rows(self.namespace,self.query())],['malformed'])

    def test_only_genuine_unresolved_overflow_refuses(self):
        self.write([self.intent('held-'+str(i),status='uncertain') for i in range(257)])
        with self.assertRaisesRegex(ValueError,'unresolved inventory'):
            self.backend.home_intent_recovery_rows(self.namespace,self.query())


class SQLiteHomeIntentQueryTests(HomeIntentQueryContract,unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.backend=SQLiteStateStore(Path(directory.name)/'state.db')


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'),'isolated PostgreSQL test DSN required')
class PostgresHomeIntentQueryTests(HomeIntentQueryContract,unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg_pool import ConnectionPool
        from codex_web.storage.postgres_state import PostgresStateStore
        dsn=os.environ['CODEX_WEB_TEST_POSTGRES_DSN'];schema='home_query_'+uuid.uuid4().hex
        with psycopg.connect(dsn) as connection:
            connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        def cleanup():
            with psycopg.connect(dsn) as connection:
                connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        self.addCleanup(cleanup)
        pool=ConnectionPool(dsn,min_size=1,max_size=2,kwargs={'options':f'-c search_path={schema}'})
        self.addCleanup(pool.close)
        self.backend=PostgresStateStore(dsn,connection_pool=pool)
