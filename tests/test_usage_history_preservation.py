"""Actual scoped FastAPI history with canonical usage inspection, synthetic data."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from codex_web.api.work_items import build_work_items_router
from codex_web.agent_runtime_usage import AgentRuntimeUsage
from codex_web.storage.agent_runtime_usage import AgentRuntimeUsageStore
from codex_web.storage.agent_teams import AgentTeamStore
from codex_web.storage.agent_profiles import AgentProfileStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.state_store import state_record_storage_key
from codex_web.services.agent_teams import AgentTeamService
from codex_web.services.agent_profiles import AgentProfileService
from codex_web.services.definitions import DefinitionRegistryService
from tests.test_task_source_project_resolution import ProjectSourceResolutionTests


class UsageHistoryPreservationTests(unittest.TestCase):
    def setUp(self):
        # Reuse actual configured-source/service fixture, not mocked usage or
        # fabricated successful WorkItem history. HTTP provider is synthetic.
        ProjectSourceResolutionTests.setUp(self)
        response = self.client.post('/api/work-items/sync/project-a', json={'reason':'fixture'})
        self.assertEqual(response.status_code,200,response.text)
        self.sqlite = SQLiteStateStore(Path(self.temp.name)/'usage-fixture.sqlite')
        self.usage = AgentRuntimeUsageStore(self.sqlite)
        definitions = DefinitionRegistryService(DefinitionRegistryStore(self.sqlite))
        profiles = AgentProfileService(AgentProfileStore(self.sqlite),definitions=definitions)
        self.profiles=profiles
        teams = AgentTeamService(AgentTeamStore(self.sqlite),profiles=profiles,
                                 definitions=definitions,runtime_usage_store=self.usage)
        self.teams=teams
        app = FastAPI()
        @app.middleware('http')
        async def identity(request, call_next):
            request.state.identity_actor = self.actor
            request.state.tenant_scope = self.actor.tenant
            return await call_next(request)
        app.include_router(build_work_items_router(self.service,agent_teams=teams))
        self.history_client = TestClient(app)
        self.addCleanup(self.history_client.close)

    def inject(self, key, raw):
        with self.sqlite._connection() as connection:
            connection.execute('INSERT INTO state_documents(namespace,payload,updated_at) VALUES (?,?,?)',
                (state_record_storage_key('agent_runtime_usage',key),raw,123.0))

    def snapshot(self):
        import hashlib,json,struct
        digest=hashlib.sha256()
        with self.sqlite._connection() as connection:
            for table,key,value in [('state_documents','namespace','payload'),('state_metadata','key','value')]:
                for k,v,t in connection.execute(f'SELECT {key},{value},updated_at FROM {table} ORDER BY {key} COLLATE BINARY'):
                    digest.update(json.dumps([table,k,v],ensure_ascii=True).encode())
                    digest.update(struct.pack('>d',t))
        return digest.hexdigest()

    def joined(self):
        from codex_web.agent_profiles import AgentProfileCreate
        from codex_web.agent_teams import AgentTeamCreate,AgentTeamMember,AgentTeamDelegationRecord
        for name in ('leader','worker'):
            self.profiles.create(AgentProfileCreate(profile_id=name,name=name),actor=self.actor)
        team=self.teams.create(AgentTeamCreate(team_id='visible-team',name='Visible team',
            leader_profile_id='leader',members=(AgentTeamMember(profile_id='worker',role='developer'),)),actor=self.actor)
        self.teams.store.append_delegation(AgentTeamDelegationRecord(
            organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id,
            team_id=team.team_id,team_revision=team.revision,work_item_id='acme/app#42',
            event_type='execution_linked',mode='linked',reason='Synthetic real canonical execution join',
            dedupe_key='visible-link',coordinator_execution_id='leader-exec',
            member_execution_ids={'worker':'worker-exec'}))

    def measured(self,name,**values):
        self.usage.upsert(AgentRuntimeUsage(id=name,organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,provider_id='synthetic',runtime_id=name,runtime_type='test',
            work_item_ref='acme/app#42',execution_id='leader-exec',**values))

    def assert_http_metrics(self,tokens,cost,records,*,complete):
        before=self.snapshot()
        for suffix in ('history','operator'):
            response=self.history_client.get('/api/work-items/acme/app%2342/'+suffix)
            self.assertEqual(response.status_code,200,response.text)
            body=response.json();history=body if suffix=='history' else body['history']
            team=history['teamDelegations']
            self.assertEqual(team['count'],1)
            self.assertEqual(team['usage']['coordinator'],{'tokens':tokens,'costUsd':cost,'records':records})
            self.assertEqual(team['usage']['workers'],{'tokens':0,'costUsd':0.0,'records':0})
            diagnostic=team['usage_diagnostics']
            self.assertEqual(diagnostic['complete'],complete)
            self.assertTrue(diagnostic['storage_complete'])
            self.assertEqual(diagnostic['metric_completeness']['coordinator'],
                {'tokens':tokens is not None,'costUsd':cost is not None,'records':True})
            self.assertNotIn('foreign-private',response.text)
        self.assertEqual(self.snapshot(),before)

    def test_joined_valid_missing_both_metrics_are_unknown_not_zero(self):
        self.joined();self.measured('unknown')
        self.assert_http_metrics(None,None,1,complete=False)

    def test_joined_known_tokens_missing_cost_preserves_metric_completeness(self):
        self.joined();self.measured('tokens-only',total_tokens=17)
        self.assert_http_metrics(17,None,1,complete=False)

    def test_joined_known_cost_missing_tokens_preserves_metric_completeness(self):
        self.joined();self.measured('cost-only',cost_usd=1.25)
        self.assert_http_metrics(None,1.25,1,complete=False)

    def test_joined_explicit_observed_zero_is_complete_zero(self):
        self.joined();self.measured('zero',total_tokens=0,cost_usd=0.0)
        self.assert_http_metrics(0,0.0,1,complete=True)

    def test_joined_known_and_unknown_records_do_not_claim_partial_sum_complete(self):
        self.joined();self.measured('known',total_tokens=17,cost_usd=1.25);self.measured('unknown',cost_usd=0.0)
        self.assert_http_metrics(None,1.25,2,complete=False)

    def test_valid_foreign_and_unjoined_missing_metrics_do_not_degrade_visible_totals(self):
        self.joined();self.measured('known',total_tokens=17,cost_usd=1.25)
        self.usage.upsert(AgentRuntimeUsage(id='foreign-private',organization_id='foreign-private-org',
            workspace_id='foreign-private-ws',provider_id='synthetic',runtime_id='foreign-private',runtime_type='test',
            work_item_ref='acme/app#42',execution_id='leader-exec'))
        self.usage.upsert(AgentRuntimeUsage(id='unjoined',organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,provider_id='synthetic',runtime_id='unjoined',runtime_type='test',
            work_item_ref='acme/app#42',execution_id='not-a-visible-execution'))
        self.assert_http_metrics(17,1.25,1,complete=True)

    def test_malformed_foreign_unknown_scope_keeps_history_and_null_totals_without_write(self):
        self.inject('foreign-private-reference','{private-unknown-tenant')
        before = self.snapshot()
        with patch.object(self.usage,'_ensure_records',side_effect=AssertionError('no migration on read')):
            response = self.history_client.get('/api/work-items/acme/app%2342/history')
        self.assertEqual(response.status_code,200,response.text)
        result = response.json()
        self.assertIn('items',result)
        usage = result['teamDelegations']
        self.assertFalse(usage['usage_diagnostics']['complete'])
        for role in ('coordinator','workers'):
            self.assertEqual(usage['usage'][role],{'tokens':None,'costUsd':None,'records':None})
        self.assertNotIn('foreign-private-reference',response.text)
        self.assertNotIn('private-unknown-tenant',response.text)
        self.assertEqual(before,self.snapshot())
        operator = self.history_client.get('/api/work-items/acme/app%2342/operator')
        self.assertEqual(operator.status_code,200,operator.text)
        self.assertFalse(operator.json()['history']['teamDelegations']['usage_diagnostics']['complete'])

    def test_scope_denial_precedes_usage_read(self):
        with patch.object(self.sqlite,'inspect_storage_pages',side_effect=AssertionError('foreign scope read')):
            result = self.history_client.get('/api/work-items/acme/app%2342/history?project_id=foreign')
        self.assertEqual(result.status_code,404)

    def test_valid_rows_future_fields_and_page_bound_are_distinct_from_zero(self):
        item = AgentRuntimeUsage(id='valid',organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,provider_id='synthetic',runtime_id='fixture',runtime_type='test',
            total_tokens=17,cost_usd=1.25)
        self.usage.upsert(item)
        before = self.snapshot()
        rows, diagnostic = self.usage.inspect_for_history()
        self.assertTrue(diagnostic['complete'])
        self.assertEqual([(r.id,r.total_tokens) for r in rows],[('valid',17)])
        self.assertEqual(before,self.snapshot())
        self.inject('future','{"schema_version":"99.0","secret":"not-for-output"}')
        rows, diagnostic = self.usage.inspect_for_history()
        self.assertFalse(diagnostic['complete'])
        self.assertEqual([r.id for r in rows],['valid'])
        self.assertNotIn('secret',str(diagnostic))
        self.assertNotIn('future',str(diagnostic))

    def test_page_limit_is_incomplete_without_unbounded_scan(self):
        for index in range(101):
            self.usage.upsert(AgentRuntimeUsage(id=f'usage-{index:03}',
                organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id,
                provider_id='synthetic',runtime_id='fixture',runtime_type='test'))
        rows, diagnostic = self.usage.inspect_for_history(max_pages=1)
        # The bounded physical page includes one collection marker.
        self.assertEqual(len(rows),99)
        self.assertFalse(diagnostic['complete'])
        self.assertTrue(diagnostic['continuation_available'])
