from __future__ import annotations

import unittest
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from codex_web.agent_runtime_usage import AgentRuntimeUsage
from codex_web.storage.agent_runtime_usage import AgentRuntimeUsageStore
from tests import test_agent_runtime_usage_api_responsiveness as api_fixtures
from tests import test_agent_runtime_telemetry as telemetry_fixtures
from tests.test_usage_resources import _record, _resource


class UsageSummaryScalingTests(unittest.TestCase):
    def setUp(self):
        fixture = telemetry_fixtures.AgentRuntimeTelemetryTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.store, self.service, self.actor = fixture.usage, fixture.service, fixture.actor

    def test_header_summary_aggregates_five_thousand_records_without_serializing_items(self):
        records = [_record(_resource(consumed=1, observed_at=i), suffix=str(i)) for i in range(5000)]
        self.store.store.record_replace(self.store.namespace, {
            item.id: item.model_dump(mode='json') for item in records
        })
        actor = self.actor.model_copy(update={'organization_id': 'org-a', 'workspace_id': 'workspace-a'})
        with patch.object(AgentRuntimeUsage, 'model_dump', side_effect=AssertionError('history serialization')), \
             patch.object(self.store, 'list', wraps=self.store.list) as listing:
            first = self.service.summary(actor, project_id='project-a')
            for _ in range(10):
                self.assertEqual(self.service.summary(actor, project_id='project-a'), first)
        self.assertEqual(listing.call_count, 1)
        self.assertNotIn('items', first)
        self.assertEqual(first['count'], 5000)
        self.assertEqual(first['resources'][0]['consumed'], 5000)
        self.assertEqual(first['freshness']['max_age_seconds'], 5)

    def test_summary_filters_tenants_and_projects_and_does_not_leak_mutable_cache(self):
        record = _record(_resource(consumed=12), suffix='one')
        self.store.upsert(record)
        actor = self.actor.model_copy(update={'organization_id': 'org-a', 'workspace_id': 'workspace-a'})
        first = self.service.summary(actor, project_id='project-a')
        first['resources'][0]['consumed'] = 9000
        self.assertEqual(self.service.summary(actor, project_id='project-a')['resources'][0]['consumed'], 12)
        self.assertEqual(self.service.summary(actor, project_id='other')['count'], 0)
        foreign = actor.model_copy(update={'workspace_id': 'foreign'})
        self.assertEqual(self.service.summary(foreign, project_id='project-a')['count'], 0)

    def test_expiry_checks_revision_and_external_changes_without_repeated_history_reads(self):
        now = [0.0]
        self.service._summary_clock = lambda: now[0]
        other = AgentRuntimeUsageStore(self.store.store)
        with patch.object(self.store, 'list', wraps=self.store.list) as listing:
            self.assertEqual(self.service.summary(self.actor)['count'], 0)
            now[0] = 6
            self.assertEqual(self.service.summary(self.actor)['count'], 0)
            self.assertEqual(listing.call_count, 1)
            other.upsert(AgentRuntimeUsage(id='external', organization_id=self.actor.organization_id,
                workspace_id=self.actor.workspace_id, provider_id='openai', runtime_id='codex', runtime_type='test'))
            now[0] = 12
            self.assertEqual(self.service.summary(self.actor)['count'], 1)
            self.store.store.record_apply(self.store.namespace, upserts={}, deletes=('external',))
            now[0] = 18
            self.assertEqual(self.service.summary(self.actor)['count'], 0)
            self.assertEqual(listing.call_count, 3)

    def test_concurrent_readers_share_one_projection(self):
        with patch.object(self.store, 'list', wraps=self.store.list) as listing:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: self.service.summary(self.actor), range(20)))
        self.assertEqual(listing.call_count, 1)
        self.assertTrue(all(result == results[0] for result in results))

    def test_cache_bounds_and_errors(self):
        for i in range(40):
            self.service.summary(self.actor, project_id=str(i))
        self.assertEqual(len(self.service._summary_cache), self.service.summary_max_entries)
        self.service.summary_max_bytes = 1
        self.service.summary(self.actor, project_id='too-large')
        self.assertFalse(any(('project_id', 'too-large') in key for key in self.service._summary_cache))
        with patch.object(self.store, 'list', side_effect=ValueError('invalid history')):
            with self.assertRaisesRegex(ValueError, 'invalid history'):
                self.service.summary(self.actor, project_id='uncached')


class UsageSummaryApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = api_fixtures.RuntimeUsageApiResponsivenessTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.service = fixture.fixture, fixture.service
        self.request, self.endpoint = fixture.request, fixture.endpoint

    async def test_resources_only_projection_is_opt_in_and_runs_off_loop(self):
        import threading
        main = threading.get_ident()
        threads = []
        def summary(actor, **filters):
            self.assertIs(actor, self.fixture.actor)
            self.assertEqual(filters['project_id'], 'p')
            threads.append(threading.get_ident())
            return {'count': 0, 'resources': [], 'freshness': {'max_age_seconds': 5}}
        with patch.object(self.service, 'summary', side_effect=summary), \
             patch.object(self.service, 'list', side_effect=AssertionError('detailed response')):
            result = await self.endpoint(self.request, project_id='p', include_items=False)
        result = json.loads(result.body)
        self.assertNotIn('items', result)
        self.assertNotEqual(threads, [main])
