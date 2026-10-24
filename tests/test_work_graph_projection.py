"""Real canonical storage cost/parity checks, without importing application."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from codex_web.api.executive_management import build_executive_management_router
from codex_web.executive_roles import ExecutiveActivationCreate
from codex_web.identity import TenantScope
from codex_web.models import WorkItemState
from codex_web.services.work_graph import WorkGraphService, WorkGraphError
from codex_web.storage.runtime_state import ModelMapRepository
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.work_graph import WorkGraphStore
from codex_web.work_graph import WorkGraphState, WorkGraphEdge, WorkGraphRelation
from tests import test_executive_management as executive_fixtures


class MeasuredStore(SQLiteStateStore):
    transferred = 0
    full_reads = 0
    projected_reads = 0

    def record_items(self, namespace):
        value = super().record_items(namespace)
        self.transferred += len(json.dumps(value).encode())
        self.full_reads += 1
        return value

    def record_page_projected(self, *args, **kwargs):
        result = super().record_page_projected(*args, **kwargs)
        self.transferred += len(json.dumps(result[0]).encode())
        self.projected_reads += 1
        return result

    def reset_cost(self):
        self.transferred = self.full_reads = self.projected_reads = 0


class WorkGraphProjectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = MeasuredStore(Path(self.temp.name) / 'state.db')
        self.repository = ModelMapRepository(self.storage, namespace='work_item_states',
            legacy_path=Path(self.temp.name) / 'absent.json', model=WorkItemState)
        self.graph_store = WorkGraphStore(self.storage)
        self.scope = TenantScope(organization_id='local', workspace_id='default')
        now = time.time()
        rows = {}
        for index in range(1205):
            ref = f'gitlab:item:{index:05d}'
            rows[ref] = WorkItemState(ref=ref, organization_id='other' if index % 11 == 0 else 'local',
                workspace_id='other' if index % 13 == 0 else 'default',
                project_id='other-project' if index % 7 == 0 else 'project-a',
                title=f'Actual canonical item {index}', current_stage='closed' if index % 9 == 0 else 'implementation_active',
                terminal_outcome='failed' if index % 17 == 0 else None,
                blocker='Policy evidence missing' if index % 19 == 0 else None,
                blocking_findings=['retained exact finding'] if index % 23 == 0 else [],
                notes=['unrelated retained historical detail ' + 'x' * 8192],
                last_meaningful_update_at=now, updated_at=now, created_at=now).model_dump(mode='json')
        self.storage.record_replace('work_item_states', rows)
        self.graph_store.update(lambda _:WorkGraphState(edges=[WorkGraphEdge(
            organization_id='local', workspace_id='default', project_id='project-a',
            relation=WorkGraphRelation.BLOCKS, source_ref='gitlab:item:00001', target_ref='gitlab:item:00002',
            created_by='owner')]))
        self.full = WorkGraphService(self.graph_store, self.repository.load)
        method = getattr(WorkGraphService, 'projected_snapshot_items', None)
        self.projected = (WorkGraphService(self.graph_store, self.repository.load,
            snapshot_items=lambda project, scope:method(self.repository, project, scope))
            if method else self.full)

    def test_complete_snapshot_equal_and_history_transfer_cost_bounded(self):
        self.storage.reset_cost()
        expected = self.full.snapshot('project-a', scope=self.scope)
        full_bytes = self.storage.transferred
        self.assertGreater(full_bytes, 8_000_000)
        self.storage.reset_cost()
        actual = self.projected.snapshot('project-a', scope=self.scope)
        # Old full-history snapshot produces the exact same graph but fails this
        # cost boundary. No timeout, node, edge or context limit was enlarged.
        self.assertLess(self.storage.transferred, 700_000)
        self.assertLess(self.storage.transferred, full_bytes // 10)
        self.assertEqual(self.storage.full_reads, 0)
        self.assertEqual(self.storage.projected_reads, 2)
        print(json.dumps({'full_record_bytes': full_bytes,
                          'projected_header_bytes': self.storage.transferred,
                          'projected_pages': self.storage.projected_reads}), flush=True)
        self.assertEqual(actual.model_dump(mode='json'), expected.model_dump(mode='json'))
        self.assertGreater(len(actual.nodes), 800)
        self.assertEqual(actual.edges, expected.edges)
        self.assertEqual(actual.critical_path, expected.critical_path)

    def test_fresh_header_changes_and_scope_remain_canonical(self):
        before = self.projected.snapshot('project-a', scope=self.scope)
        raw = self.storage.record_get('work_item_states', 'gitlab:item:00001')
        raw.update(terminal_outcome='failed', blocker='new current blocker', blocking_findings=['new finding'])
        self.storage.record_apply('work_item_states', upserts={'gitlab:item:00001': raw})
        after = self.projected.snapshot('project-a', scope=self.scope)
        self.assertNotEqual(before, after)
        self.assertEqual(after, self.full.snapshot('project-a', scope=self.scope))
        self.assertEqual(self.projected.snapshot('absent', scope=self.scope).nodes, ())
        self.assertNotIn('gitlab:item:00000', {n.ref for n in after.nodes})

    def test_unavailable_projection_and_nonadvancing_cursor_fail_closed(self):
        class Unavailable:
            def projected_page(self, **_): raise RuntimeError('storage unavailable')
        with self.assertRaises(RuntimeError):
            WorkGraphService.projected_snapshot_items(Unavailable(), 'project-a', self.scope)
        class Stuck:
            def projected_page(self, **_): return {}, 'same'
        with self.assertRaises(WorkGraphError):
            WorkGraphService.projected_snapshot_items(Stuck(), 'project-a', self.scope)

    async def test_actual_executive_route_prepares_bounded_graph_context_with_unchanged_deadline(self):
        fixture = executive_fixtures.ExecutiveManagementTests()
        await fixture.asyncSetUp()
        self.addAsyncCleanup(fixture.asyncTearDown)
        fixture.service.work_graph = self.projected
        actor = fixture.actor
        scope = actor.tenant
        full = self.full.snapshot('project-a', scope=scope)
        app = FastAPI()

        @app.middleware('http')
        async def identity(request: Request, call_next):
            request.state.identity_actor = actor
            return await call_next(request)

        app.include_router(build_executive_management_router(fixture.service))
        self.storage.reset_cost()
        with TestClient(app) as client:
            started = time.monotonic()
            response = client.post('/api/executive/activations', json=ExecutiveActivationCreate(
                subject='Bounded project strategy', request='Assess current graph priorities.',
                project_id='project-a', requested_role_ids=('cto',)).model_dump(mode='json'))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertLess(time.monotonic() - started, 5)
        saved = response.json()['item']
        context = saved['context']['work_graphs'][0]
        self.assertEqual(context['progress'], full.progress.model_dump(mode='json'))
        self.assertFalse(context['complete'])
        self.assertEqual(context['omitted']['nodes'], len(full.nodes)-len(context['nodes']))
        self.assertLessEqual(len(context['nodes']), 20)
        self.assertLess(self.storage.transferred, 700_000)
        self.assertEqual(self.full.snapshot('project-a', scope=scope), full)
        self.assertEqual(fixture.gateway.requests, [])
