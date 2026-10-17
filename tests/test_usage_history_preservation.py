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
        teams = AgentTeamService(AgentTeamStore(self.sqlite),profiles=profiles,
                                 definitions=definitions,runtime_usage_store=self.usage)
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
        import hashlib
        digest = hashlib.sha256()
        for chunk in self.sqlite.iter_storage_snapshot():
            digest.update(chunk)
        return digest.hexdigest()

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
        self.assertEqual(len(rows),100)
        self.assertFalse(diagnostic['complete'])
        self.assertTrue(diagnostic['continuation_available'])
