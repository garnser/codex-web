from __future__ import annotations

import copy
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from codex_web.identity import AuthenticationActor, AuthenticationAssurance, PrincipalKind
from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingService
from codex_web.storage.thread_bootstrap_bindings import ThreadBootstrapBindingStore
from codex_web.thread_bootstrap import ThreadBootstrapBinding
from codex_web.models import IndexedThread
from codex_web.services.threads import ThreadService
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.thread_history import ThreadHistoryRepository
from codex_web.storage.thread_index import ThreadIndexRepository


class ThreadRunnerProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_stopped_process_overrides_live_lease_without_mutation(self):
        current = self.assignments['successor']
        before = dict(vars(current))
        local = SimpleNamespace(fence=current.fence, worker_id=current.assigned_worker_id,
            running=False, diagnostic_state='unavailable', pending_executor_work=1)
        self.service.session_manager = SimpleNamespace(get=lambda _:SimpleNamespace(status=lambda:local))
        projection = self.service._runner_projection('successor')
        self.assertEqual(projection['state'], 'stopped')
        self.assertFalse(projection['live'])
        self.assertTrue(projection['leaseValid'])
        self.assertEqual(projection['diagnosticState'], 'unavailable')
        self.assertEqual(vars(current), before)
        local.fence -= 1
        self.assertTrue(self.service._runner_projection('successor')['live'], 'old process cannot mask a successor')

    async def test_lost_and_canonical_retry_are_distinct_from_unknown(self):
        self.assignments['successor'].status = 'lost'
        self.assignments['successor'].lease = None
        self.assertEqual(self.service._runner_projection('successor')['state'], 'lost')
        self.assignments['successor'].status = 'pending'
        self.assertEqual(self.service._runner_projection('successor')['state'], 'recovering')
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.tmp.name) / 'state.db')
        self.history = ThreadHistoryRepository(self.store)
        self.index = ThreadIndexRepository(self.store, Path(self.tmp.name) / 'index.json')
        self.actor = AuthenticationActor(identity_id='test', assurance=AuthenticationAssurance.MFA, principal_kind=PrincipalKind.SERVICE, organization_id='org', workspace_id='workspace')
        self.assignments = {}
        self.bindings = {}
        self.worker = SimpleNamespace(organization_id='org', workspace_id='workspace', lifecycle='active')
        for thread_id, status in [('predecessor', 'cancelled'), ('successor', 'running')]:
            self.history.start_turn(thread_id, turn_id='retained-'+thread_id, message='Preserve this history',
                                    provider_id='openai', runtime_id='codex', created_at=10)
            self.index.upsert(IndexedThread(id=thread_id, name=thread_id, project_id='home', cwd='/repo', updatedAt=10))
            lease = None if status == 'cancelled' else SimpleNamespace(
                worker_id='worker', fence=2, expires_at=time.time()+3600, lease_token='never-public')
            self.assignments[thread_id] = SimpleNamespace(id=thread_id, status=status,
                organization_id='org', workspace_id='workspace', execution_id='exec-'+thread_id,
                project_id='home', execution_workspace_id='workspace-'+thread_id, deadline_at=None,
                created_at=10, updated_at=20, assigned_worker_id='worker', fence=2, lease=lease,
                failure_code='thread_bootstrap_cancelled' if status == 'cancelled' else None)
            self.bindings[thread_id] = ThreadBootstrapBinding(id='binding-'+thread_id, organization_id='org', workspace_id='workspace', created_by='test', bootstrap_id='bootstrap-'+thread_id, thread_id=thread_id, assignment_id=thread_id,
                execution_id='exec-'+thread_id, execution_workspace_id='workspace-'+thread_id)
        self.active = {key:SimpleNamespace(assignment_id=key, fence=2,
                       turn_id='retained-'+key, updated_at=20) for key in self.assignments}
        self.runtime_status = {'predecessor':'notLoaded', 'successor':'active'}
        self.runtime_turns = {}
        async def runtime(method, params):
            if method == 'thread/list':
                return {'data':[{'id':key, 'cwd':'/repo', 'status':{'type':self.runtime_status[key]}} for key in self.assignments]}
            return {'thread': {'id':params['threadId'], 'status':{'type':self.runtime_status[params['threadId']]}, 'turns':self.runtime_turns.get(params['threadId'], [])}}
        self.binding_store = ThreadBootstrapBindingStore(self.store)
        self.binding_store.update(lambda state:state.model_copy(update={'bindings':list(self.bindings.values())}))
        self.service = ThreadService(runtime_transport=SimpleNamespace(request=runtime),
            runtime_request_for_thread=lambda _id, method, params: runtime(method, params), event_sink=lambda _:None,
            thread_index=self.index, thread_history=self.history,
            project_runtime=SimpleNamespace(get=lambda _:SimpleNamespace(id='home',path='/repo',model=None)),
            recovery=SimpleNamespace(raise_if_thread_replaced=lambda _:None),
            resume_runtime=SimpleNamespace(active_task=lambda _:None,is_timeout_error=lambda _:False),
            active_turn_getter=self.active.get,
            bootstrap_bindings=ThreadBootstrapBindingService(self.binding_store), control_actor=self.actor,
            binding_service=SimpleNamespace(workers=SimpleNamespace(store=SimpleNamespace(
                assignment=self.assignments.get, worker=lambda _:self.worker))))

    def tearDown(self):
        self.tmp.cleanup()

    async def test_cancelled_predecessor_and_running_successor_agree_in_list_and_detail(self):
        before = copy.deepcopy(self.history.thread('predecessor'))
        rows = {row['id']:row for row in (await self.service.list('home'))['data']}
        for key, expected in [('predecessor','cancelled'),('successor','running')]:
            detail = (await self.service.read(key))['thread']
            self.assertEqual(rows[key]['runner']['state'], expected)
            self.assertEqual(detail['runner']['state'], expected)
            self.assertEqual(detail['runner']['live'], key == 'successor')
            self.assertNotIn('lease_token', str(detail['runner']))
        self.assertEqual(rows['predecessor']['status']['type'], 'idle')
        self.assertEqual(self.history.thread('predecessor'), before)
        self.assertEqual((await self.service.read('predecessor'))['thread']['turns'][0]['status'], 'inProgress')

    def test_missing_expired_and_stale_fence_are_unknown_without_recovery(self):
        assignment = self.assignments['successor']
        for lease in [None, SimpleNamespace(worker_id='worker',fence=2,expires_at=0),
                      SimpleNamespace(worker_id='worker',fence=1,expires_at=time.time()+3600)]:
            assignment.lease = lease
            runner = self.service._runner_projection('successor')
            self.assertEqual(runner['state'],'unknown')
            self.assertFalse(runner['live'])

    def test_cross_tenant_projection_redacts_assignment_identity(self):
        self.assignments['successor'].organization_id = 'other'
        runner = self.service._runner_projection('successor')
        self.assertEqual(runner['reason'], 'assignment_scope_mismatch')
        self.assertNotIn('assignmentId', runner)

    async def test_rename_unavailability_does_not_change_canonical_name(self):
        from codex_web.services.thread_naming import ThreadNamingService
        from fastapi import HTTPException
        request = AsyncMock(side_effect=HTTPException(503,'runtime session unavailable'))
        self.service.naming = ThreadNamingService(request,self.index,lambda:[],event_sink=lambda _:None)
        with self.assertRaises(HTTPException):
            await self.service.rename('predecessor','New name')
        self.assertEqual(self.index.get('predecessor').name,'predecessor')

    async def test_page_reads_binding_namespace_once_and_only_page_assignments(self):
        assignments = Mock(side_effect=self.assignments.get)
        self.service.binding_service.workers.store.assignment = assignments
        with patch.object(self.binding_store, 'load', wraps=self.binding_store.load) as load:
            rows = (await self.service.list('home', limit=2))['data']
            load.assert_called_once()
        self.assertEqual(len(rows), 2)
        self.assertEqual(assignments.call_count, 2)
        self.assertEqual({call.args[0] for call in assignments.call_args_list}, {row['id'] for row in rows})

    async def test_waiting_renewal_rebind_and_unavailable_worker_are_current_reads(self):
        assignment = self.assignments['successor']
        assignment.status = 'pending'
        self.assertEqual((await self.service.runner('successor'))['runner']['state'], 'recovering')
        assignment.status = 'running'
        self.assertTrue((await self.service.runner('successor'))['runner']['live'])
        assignment.lease.expires_at = 0
        self.assertFalse((await self.service.runner('successor'))['runner']['live'])
        assignment.fence = assignment.lease.fence = 3
        assignment.lease.expires_at = time.time()+3600
        projection = (await self.service.runner('successor'))['runner']
        self.assertTrue(projection['live'])
        self.assertEqual(projection['fence'], 3)
        for lifecycle in ['offline', 'quarantined', 'revoked']:
            self.worker.lifecycle = lifecycle
            self.assertEqual(self.service._runner_projection('successor')['state'], 'unknown')

    def test_scope_and_lease_mismatches_redact_or_fail_closed(self):
        assignment = self.assignments['successor']
        for attr in ['workspace_id', 'project_id', 'execution_id', 'execution_workspace_id', 'id']:
            original = getattr(assignment, attr)
            setattr(assignment, attr, 'foreign')
            runner = self.service._runner_projection('successor')
            self.assertEqual(runner['state'], 'unknown')
            self.assertNotIn('assignmentId', runner)
            setattr(assignment, attr, original)
        assignment.lease.worker_id = 'other'
        self.assertFalse(self.service._runner_projection('successor')['live'])

    async def test_lookup_failure_and_missing_assignment_preserve_history(self):
        before = copy.deepcopy(self.history.thread('successor'))
        self.service.binding_service.workers.store.assignment = Mock(side_effect=RuntimeError('unavailable'))
        self.assertEqual((await self.service.read('successor'))['thread']['runner']['state'], 'unknown')
        self.service.binding_service.workers.store.assignment = lambda _:None
        self.assertEqual(self.service._runner_projection('successor')['reason'], 'assignment_missing')
        self.assertEqual(self.history.thread('successor'), before)

    async def test_api_list_detail_runner_and_rename_use_actual_projection(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from codex_web.api.threads import build_threads_router
        app = FastAPI()
        @app.middleware('http')
        async def actor(request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)
        scope = SimpleNamespace(project=lambda *_:SimpleNamespace(id='home'),
                                for_thread=lambda *_:SimpleNamespace(id='home'))
        app.include_router(build_threads_router(self.service, scope))
        before = copy.deepcopy(self.history.thread('predecessor'))
        with TestClient(app) as client:
            rows = client.get('/api/threads?project_id=home').json()['data']
            self.assertEqual({row['runner']['state'] for row in rows}, {'running','cancelled'})
            detail = client.get('/api/threads/predecessor?project_id=home').json()['thread']
            self.assertEqual(detail['runner']['state'], 'cancelled')
            self.assertEqual(detail['turns'][0]['id'], 'retained-predecessor')
            self.assertEqual(client.get('/api/threads/successor/runner').json()['runner']['state'], 'running')
            self.active.pop('successor')
            self.runtime_status['successor'] = 'idle'
            self.runtime_turns['successor'] = [{'id':'completed-turn','status':'completed','items':[]}]
            self.assignments['successor'].lease.expires_at = time.time()+7200
            for route in ['/api/threads/successor', '/api/threads/successor/runner']:
                result = client.get(route).json()
                thread = result.get('thread', result)
                self.assertTrue(thread['runner']['live'])
                self.assertEqual(thread['status']['type'], 'idle')
            self.assertEqual(client.get('/api/threads/successor').json()['thread']['turns'][0]['id'], 'completed-turn')
        self.assertEqual(self.history.thread('predecessor'), before)

    async def test_live_renewed_custody_does_not_manufacture_activity(self):
        assignment = self.assignments['successor']
        assignment.lease.expires_at = time.time()+7200
        self.active.pop('successor')
        self.runtime_status['successor'] = 'idle'
        before = copy.deepcopy(self.history.thread('successor'))
        rows = {row['id']:row for row in (await self.service.list('home'))['data']}
        detail = (await self.service.read('successor'))['thread']
        for thread in [rows['successor'], detail, await self.service.runner('successor')]:
            self.assertTrue(thread['runner']['live'])
            self.assertEqual(thread['status']['type'], 'idle')
        self.assertEqual(self.history.thread('successor'), before)
        self.active['successor'] = SimpleNamespace(assignment_id='successor',fence=2,turn_id='next-turn')
        projection = await self.service.runner('successor')
        self.assertEqual(projection['activity']['turnId'], 'next-turn')
        self.assertEqual(projection['status']['type'], 'active')
        self.active['successor'].fence = 1
        self.assertEqual((await self.service.runner('successor'))['activity']['state'], 'unknown')
