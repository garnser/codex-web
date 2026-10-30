"""Point selection keeps first scoped record and original JSON decoding."""
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from codex_web.storage.postgres_state import PostgresStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests import test_autonomy_append_scaling as fixtures


class PointEnvelopeContract:
    def put_text(self, raw):
        self.backend.put('point-envelope', {})
        pg = isinstance(self.backend, PostgresStateStore)
        table, slot = ('codex_state_documents', '%s') if pg else ('state_documents', '?')
        with self.backend._connection() as connection:
            connection.execute(f'UPDATE {table} SET payload={slot} WHERE namespace={slot}', (raw, 'point-envelope'))
            connection.commit()

    def point(self):
        return self.backend.document_array_item_envelope('point-envelope', 'records', 'selected',
            organization_id='local', workspace_id='default', collection_names=('records','requests','events'))

    def test_first_scoped_duplicate_keeps_original_numeric_unicode_and_value(self):
        raw = r'''{"schema_version":"1.1","records":[
            {"id":"selected","organization_id":"foreign","workspace_id":"default","value":"foreign"},
            {"id":"selected","organization_id":"local","workspace_id":"default",
             "value":{"scientific":1e0,"signedzero":-0.0,"bigint":BIGINT,"unicode":"\u96ea","duplicate":1,"duplicate":2}},
            {"id":"selected","organization_id":"local","workspace_id":"default","value":"later"}],
            "requests":[],"events":[{"retained":"history"}]}'''.replace('BIGINT',str(2**100+1))
        self.put_text(raw)
        with patch.object(self.backend,'get',side_effect=AssertionError('unexpected whole-catalog fallback')):
            actual=self.point()
        self.assertEqual(actual,{'schema_version':'1.1','records':[json.loads(raw)['records'][1]]})
        value=actual['records'][0]['value']
        self.assertIs(type(value['scientific']),float)
        self.assertEqual(math.copysign(1,value['signedzero']),-1)
        self.assertEqual(value['bigint'],2**100+1)
        self.assertEqual(value['unicode'],'雪')
        self.assertEqual(value['duplicate'],2)

    def test_foreign_or_missing_id_does_not_select_unscoped_record(self):
        self.backend.put('point-envelope',{'schema_version':'1.1','records':[
            {'id':'selected','organization_id':'foreign','workspace_id':'default'},
            {'id':'other','organization_id':'local','workspace_id':'default'}], 'requests':[], 'events':[]})
        self.assertEqual(self.point(),{'schema_version':'1.1','records':[]})

    def test_legacy_numeric_schema_returns_full_original_envelope(self):
        raw='{"schema_version":1e0,"records":[],"requests":[],"events":[{"retained":true}]}'
        self.put_text(raw)
        actual=self.point()
        self.assertEqual(actual['records'],[])
        self.assertIs(type(actual['schema_version']),float)
        if isinstance(self.backend,PostgresStateStore):self.assertEqual(actual,json.loads(raw))

    def test_each_legacy_conversion_fallback_keeps_owning_transaction_usable(self):
        for case,value in [('nan',float('nan')),('nul','before\0after'),('surrogate','before\ud800after'),('overflow','overflow-number')]:
            with self.subTest(case=case):
                row={'id':'selected','organization_id':'local','workspace_id':'default','value':value}
                raw=json.dumps({'schema_version':'1.1','records':[row],'requests':[],'events':[]})
                if case=='overflow':raw=raw.replace('"overflow-number"','1e1000000')
                self.put_text(raw);self.backend.put('point-sentinel',{'ok':False})
                def update(current):
                    actual=self.point()['records'][0]['value']
                    if case=='nan':self.assertTrue(math.isnan(actual))
                    elif case=='overflow':self.assertTrue(math.isinf(actual))
                    else:self.assertEqual(actual,value)
                    return {'ok':True}
                self.backend.update('point-sentinel',update,default={})
                self.assertEqual(self.backend.get('point-sentinel'),{'ok':True})

    def test_malformed_or_unexpected_envelope_preserves_full_validation_input(self):
        for raw in [{'schema_version':'1.1','records':{},'requests':[],'events':[]},
                    {'schema_version':'1.1','records':[],'requests':[],'events':[],'unexpected':True}]:
            with self.subTest(raw=raw):
                self.backend.put('point-envelope',raw)
                self.assertEqual(self.point(),raw)


class SQLitePointEnvelopeTests(PointEnvelopeContract,unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.backend=SQLiteStateStore(Path(directory.name)/'state.db')


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'),'PostgreSQL test DSN required')
class PostgresPointEnvelopeTests(PointEnvelopeContract,unittest.TestCase):
    def setUp(self):
        fixtures.PostgresAutonomyAppendTests.setUp(self)

    def test_legacy_identity_text_matches_original_json_for_point_and_batch(self):
        # These generic envelope predicates retain JSON's exact text even when
        # a malformed identity will subsequently be refused by its typed owner.
        cases=(('1e0','1'),('{"z": 1, "a": 2}','{"a": 2, "z": 1}'),('[1,  2]','[1, 2]'))
        for field in ('id', 'organization_id', 'workspace_id'):
            for raw_identity,normalized in cases:
                with self.subTest(field=field,raw_identity=raw_identity):
                    row={'id':'selected','organization_id':'local','workspace_id':'default','value':'retained'}
                    row[field]='legacy-identity'
                    raw=json.dumps({'schema_version':'1.1','records':[row],'requests':[],'events':[]}).replace('"legacy-identity"',raw_identity)
                    self.put_text(raw)
                    for text,selected in ((raw_identity,True),(normalized,False)):
                        scope={'organization_id':'local','workspace_id':'default','collection_names':('records','requests','events')}
                        item='selected'
                        if field=='id':item=text
                        else:scope[field]=text
                        for method,selector in (('document_array_item_envelope',item),('document_array_items_envelope',(item,))):
                            with self.subTest(text=text,method=method):
                                actual=getattr(self.backend,method)('point-envelope','records',selector,**scope)
                                self.assertEqual(actual['records'],json.loads(raw)['records'] if selected else [])

    def test_first_per_id_groups_by_original_point_identity_text(self):
        for raw_identity in ('1', '1e0', '{"z": 1, "a": 2}', '[1,  2]'):
            with self.subTest(raw_identity=raw_identity):
                # Numeric and string IDs (or object/array and string IDs) can
                # match the SAME original JSON-text POINT predicate.
                rows = ('{"id":' + raw_identity +
                    ',"organization_id":"local","workspace_id":"default","value":"first"},' +
                    json.dumps({'id': raw_identity, 'organization_id': 'local',
                        'workspace_id': 'default', 'value': 'later'}))
                self.put_text('{"schema_version":"1.1","records":[' + rows + ']}')
                scope = {'organization_id': 'local', 'workspace_id': 'default',
                         'collection_names': ('records', 'requests', 'events')}
                point = self.backend.document_array_item_envelope('point-envelope', 'records', raw_identity, **scope)
                first = self.backend.document_array_items_envelope('point-envelope', 'records',
                    (raw_identity,), first_per_id=True, **scope)
                all_matches = self.backend.document_array_items_envelope('point-envelope', 'records',
                    (raw_identity,), **scope)
                self.assertEqual(first, point)
                self.assertEqual(len(first['records']), 1)
                self.assertEqual(len(all_matches['records']), 2)

    def test_real_server_casts_whole_envelope_only_twice(self):
        self.backend.put('point-envelope',{'schema_version':'1.1','records':[
            {'id':'selected','organization_id':'local','workspace_id':'default','value':'first'},
            {'id':'selected','organization_id':'local','workspace_id':'default','value':'second'}],
            'requests':[],'events':[{'history':'retained '*1000}for _ in range(150)]})
        with self.backend._connection() as connection:
            connection.execute('CREATE TEMP TABLE point_parse_calls(kind text)')
            for kind in ('json','jsonb'):
                connection.execute(f'''CREATE FUNCTION pg_temp.point_count_{kind}(text) RETURNS {kind}
                    LANGUAGE plpgsql AS $$ BEGIN INSERT INTO pg_temp.point_parse_calls VALUES ('{kind}');
                    RETURN $1::{kind}; END $$''')
            class Cursor:
                def __init__(self,cursor):self.cursor=cursor
                def __enter__(self):self.cursor.__enter__();return self
                def __exit__(self,*args):return self.cursor.__exit__(*args)
                def __getattr__(self,name):return getattr(self.cursor,name)
                def execute(self,query,*args,**kwargs):
                    query=query.replace('payload::jsonb','pg_temp.point_count_jsonb(payload)')
                    query=query.replace('payload::json','pg_temp.point_count_json(payload)')
                    return self.cursor.execute(query,*args,**kwargs)
            class Connection:
                def cursor(self,*args,**kwargs):return Cursor(connection.cursor(*args,**kwargs))
                def transaction(self):return connection.transaction()
            @contextmanager
            def tracing():yield Connection()
            with patch.object(self.backend,'_read_connection',tracing):actual=self.point()
            counts=dict(connection.execute('SELECT kind,count(*) FROM pg_temp.point_parse_calls GROUP BY kind').fetchall())
        self.assertEqual(actual['records'][0]['value'],'first')
        self.assertEqual(len(actual['records']),1)
        self.assertLessEqual(sum(counts.values()),2,'Point predicates must share the materialized parse')
        self.assertEqual(counts,{'jsonb':1,'json':1})

    def test_batch_policies_preserve_raw_values_and_original_document_order(self):
        raw = r'''{"schema_version":"1.1","records":[
            null, 17,
            {"id":"selected","organization_id":"foreign","workspace_id":"default","value":"foreign-first"},
            {"id":"other","organization_id":"local","workspace_id":"default","value":"other-first"},
            {"id":"selected","organization_id":"local","workspace_id":"default",
             "value":{"scientific":1e0,"signedzero":-0.0,"unicode":"\u96ea","duplicate":1,"duplicate":2}},
            {"id":"selected","organization_id":"local","workspace_id":"default","value":"later"}],
            "requests":[],"events":[]}'''
        self.put_text(raw)
        rows = json.loads(raw)['records']
        scope = {'organization_id':'local','workspace_id':'default',
                 'collection_names':('records','requests','events')}
        for first, after, expected in ((False, False, [rows[3],rows[4],rows[5]]),
                                       (True, False, [rows[3],rows[4]]),
                                       (True, True, [rows[3]])):
            with self.subTest(first_per_id=first,scope_after_first=after):
                actual = self.backend.document_array_items_envelope('point-envelope','records',
                    ('selected','missing','other','selected'),first_per_id=first,
                    scope_after_first=after,**scope)
                self.assertEqual(actual,{'schema_version':'1.1','records':expected})
        value = self.backend.document_array_item_envelope('point-envelope','records','selected',**scope)['records'][0]['value']
        self.assertIs(type(value['scientific']),float)
        self.assertEqual(math.copysign(1,value['signedzero']),-1)
        self.assertEqual(value['unicode'],'雪')
        self.assertEqual(value['duplicate'],2)
        self.assertEqual(self.backend.document_array_items_envelope('point-envelope','records',(),**scope),
                         {'schema_version':'1.1','records':[]})

    def test_batch_malformed_headers_keep_full_original_validation_input(self):
        for raw in ('{"schema_version":1e0,"records":[],"events":[{"retained":true}]}',
                    '{"schema_version":"1.1","records":{},"requests":[],"events":[]}',
                    '{"schema_version":"1.1","records":[],"events":[],"unexpected":true}',
                    '{"schema_version":"1.1","records":[],"requests":{},"events":[]}',
                    '[{"records":[]}]'):
            with self.subTest(raw=raw):
                self.put_text(raw)
                actual = self.backend.document_array_items_envelope('point-envelope','records',('selected',),
                    organization_id='local',workspace_id='default',collection_names=('records','requests','events'))
                self.assertEqual(actual,json.loads(raw))

    def test_unrelated_large_members_do_not_enter_selected_materialization(self):
        rows = [{'id':'unrelated-'+str(i),'organization_id':'local','workspace_id':'default',
                 'padding':'synthetic metadata '*80} for i in range(128)]
        selected = [{'id':'selected','organization_id':tenant,'workspace_id':'default','value':i}
                    for i,tenant in enumerate(('foreign','local','local'))]
        self.backend.put('point-envelope',{'schema_version':'1.1','records':rows+selected,
                                         'requests':[],'events':[]})
        for multi in (False,True):
            captured=[]
            with self.backend._connection() as connection:
                class Cursor:
                    def __init__(self,cursor):self.cursor=cursor
                    def __enter__(self):self.cursor.__enter__();return self
                    def __exit__(self,*args):return self.cursor.__exit__(*args)
                    def __getattr__(self,name):return getattr(self.cursor,name)
                    def execute(self,query,parameters):
                        captured.append((query,parameters))
                        return self.cursor.execute(query,parameters)
                class Connection:
                    def cursor(self,*args,**kwargs):return Cursor(connection.cursor(*args,**kwargs))
                    def transaction(self):return connection.transaction()
                @contextmanager
                def tracing():yield Connection()
                with patch.object(self.backend,'_read_connection',tracing):
                    actual=(self.backend.document_array_items_envelope('point-envelope','records',('selected',),
                        organization_id='local',workspace_id='default',collection_names=('records','requests','events'))
                        if multi else self.point())
                query,parameters=captured[-1]
                plan=connection.execute('EXPLAIN (ANALYZE,FORMAT JSON) '+query,parameters).fetchone()[0]
            self.assertEqual(actual['records'],selected[1:] if multi else selected[1:2])
            def nodes(node):
                yield node
                for child in node.get('Plans',[]):yield from nodes(child)
            producer=next(node for node in nodes(plan[0]['Plan']) if node.get('Subplan Name')=='CTE members')
            self.assertEqual(producer['Actual Rows'],3,
                'Only matching original members may enter the reusable materialized relation')
