from __future__ import annotations

import asyncio
import contextvars
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from codex_web.agent_runtime_usage import AgentRuntimeUsage
from codex_web.api.agent_runtime_usage import build_agent_runtime_usage_router
from tests import test_agent_runtime_telemetry as fixture


class RuntimeUsageApiResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixture.AgentRuntimeTelemetryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.service = self.fixture.service
        self.request = Request({'type': 'http', 'state': {'identity_actor': self.fixture.actor}})
        self.endpoint = build_agent_runtime_usage_router(self.service).routes[0].endpoint

    async def test_storage_barrier_keeps_loop_responsive_actor_and_context(self):
        started, release = threading.Event(), threading.Event()
        context = contextvars.ContextVar('usage_route_context', default=None)
        context.set('correlated-request')
        observed = []
        original = self.service.store.list
        def blocking():
            observed.append((threading.get_ident(), context.get()))
            started.set()
            release.wait(2)
            return original()
        with patch.object(self.service.store, 'list', side_effect=blocking):
            task = asyncio.create_task(self.endpoint(self.request, project_id='project-a'))
            try:
                await asyncio.sleep(0)
                self.assertFalse(task.done(), 'synchronous storage blocked the request loop until completion')
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                self.assertNotEqual(observed[0][0], threading.get_ident())
                self.assertEqual(observed[0][1], 'correlated-request')
                unrelated = asyncio.Event()
                asyncio.get_running_loop().call_soon(unrelated.set)
                await unrelated.wait()
                self.assertFalse(task.done())
            finally:
                release.set()
                await task

    async def test_complete_projection_runs_offloop_in_original_order(self):
        calls = []
        main = threading.get_ident()
        class Value:
            def __init__(self, kind): self.kind = kind
            def model_dump(self, **kwargs):
                calls.append((self.kind, threading.get_ident()))
                return {'kind': self.kind}
        def list_records(actor, **filters):
            self.assertIs(actor, self.fixture.actor)
            self.assertEqual(filters, dict(project_id='p', execution_id='e', agent_session_id='s',
                provider_id='provider', runtime_id='runtime', model_id='model'))
            calls.append(('list', threading.get_ident()))
            return [Value('item')]
        def aggregate(items):
            self.assertEqual(len(items), 1)
            calls.append(('aggregate', threading.get_ident()))
            return [Value('resource')]
        service = SimpleNamespace(list=list_records, aggregate_resources=aggregate)
        endpoint = build_agent_runtime_usage_router(service).routes[0].endpoint
        result = await endpoint(self.request, 'p', 'e', 's', 'provider', 'runtime', 'model')
        self.assertEqual(result, {'items': [{'kind': 'item'}], 'count': 1,
            'resources': [{'kind': 'resource'}]})
        self.assertEqual([x[0] for x in calls], ['list', 'aggregate', 'item', 'resource'])
        self.assertTrue(all(thread != main for _,thread in calls))

    async def test_real_store_preserves_tenant_and_all_filters_and_output(self):
        record = AgentRuntimeUsage(id='wanted', organization_id=self.fixture.actor.organization_id,
            workspace_id=self.fixture.actor.workspace_id, provider_id='openai', runtime_id='codex',
            runtime_type='codex', project_id='p', execution_id='e', agent_session_id='s',
            observed_model_ids=('model',), observed_at=10)
        self.service.store.upsert(record)
        for changes in ({'organization_id':'foreign'}, {'workspace_id':'foreign'}, {'project_id':'other'},
            {'execution_id':'other'}, {'agent_session_id':'other'}, {'provider_id':'other'},
            {'runtime_id':'other'}, {'observed_model_ids':('other',)}):
            self.service.store.upsert(record.model_copy(update={'id': 'other-'+str(len(changes))+'-'+next(iter(changes)), **changes}))
        result = await self.endpoint(self.request, 'p', 'e', 's', 'openai', 'codex', 'model')
        self.assertEqual(result, {'items': [record.model_dump(mode='json')], 'count': 1, 'resources': []})

    async def test_unauthenticated_request_denies_before_catalog_read(self):
        request = Request({'type': 'http', 'state': {}})
        with patch.object(self.service, 'list') as listing:
            with self.assertRaises(HTTPException) as denied: await self.endpoint(request)
        self.assertEqual(denied.exception.status_code, 401)
        listing.assert_not_called()

    async def test_read_failure_propagates_without_aggregation(self):
        with patch.object(self.service.store, 'list', side_effect=ValueError('corrupt catalog')), \
             patch.object(self.service, 'aggregate_resources') as aggregate:
            with self.assertRaisesRegex(ValueError, 'corrupt catalog'): await self.endpoint(self.request)
        aggregate.assert_not_called()

    async def test_cancelled_request_returns_no_projection_and_worker_can_finish(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def blocking():
            started.set()
            release.wait(2)
            finished.set()
            return []
        with patch.object(self.service.store, 'list', side_effect=blocking):
            task = asyncio.create_task(self.endpoint(self.request))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
            finally:
                release.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 2))
