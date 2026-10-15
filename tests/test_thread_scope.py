from __future__ import annotations

import asyncio
import threading
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from codex_web.agent_runtime import AgentSession
from codex_web.api.context import build_context_router
from codex_web.api.project_ui_state import build_project_ui_state_router
from codex_web.api.threads import build_threads_router
from codex_web.api.turns import build_turns_router
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, PrincipalKind
from codex_web.models import IndexedThread, Project
from codex_web.services.context import ContextCompactionService
from codex_web.services.agent_runtime import AgentRuntimeRegistry, AgentSessionService
from codex_web.services.project_runtime import ProjectRuntimeService
from codex_web.services.projects import ProjectService
from codex_web.services.thread_scope import ThreadScopeService
from codex_web.services.threads import ThreadService
from codex_web.storage.agent_sessions import AgentSessionStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.thread_index import ThreadIndexRepository


class ThreadScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        store = SQLiteStateStore(root / 'state.sqlite3')
        self.actor = AuthenticationActor(identity_id='actor-a', principal_kind=PrincipalKind.HUMAN,
                                         organization_id='org-a', workspace_id='ws-a', assurance=AuthenticationAssurance.MFA)
        self.projects = [Project(id=id, name=id, path='/shared' if id in ('a', 'b') else '/' + id,
                                 organization_id='org-f' if id == 'foreign' else 'org-a', workspace_id='ws-a')
                         for id in ('a', 'b', 'empty', 'foreign')]
        self.project_service = ProjectService(SimpleNamespace(load=lambda: self.projects))
        self.sessions = AgentSessionService(AgentSessionStore(store), AgentRuntimeRegistry())
        self.index = ThreadIndexRepository(store, root / 'threads.json')
        for n, project in enumerate(self.projects):
            if project.id == 'empty':
                continue
            self.index.upsert(IndexedThread(id='thread-' + project.id, name=project.id,
                              cwd=project.path, project_id=project.id, updatedAt=n))
            self.sessions.store.upsert(AgentSession(organization_id=project.organization_id,
                workspace_id=project.workspace_id, project_id=project.id, provider_id='openai',
                runtime_id='codex', runtime_type='app-server', provider_native_session_id='thread-' + project.id))
        self.scope = ThreadScopeService(self.project_service, self.sessions, self.index)
        self.delegate = SimpleNamespace(**{name: AsyncMock(return_value={'ok': True}) for name in
            ['list', 'read', 'runner', 'rename', 'archive', 'unarchive', 'create', 'interrupt', 'update_primary',
             'update_primary_channel', 'start', 'resume', 'replace', 'steer_latest', 'steer', 'retry_preflight', 'compact']})
        for name in ['update_settings', 'get_settings', 'queue', 'preflight_attempts', 'status']:
            setattr(self.delegate, name, Mock(return_value={'ok': True}))
        self.delegate.binding_state = Mock(return_value={'bindings': {'items': []}})
        self.delegate.etag = Mock(return_value='test-etag')
        self.delegate.list_settings = Mock(return_value={
            'thread-a': {'model': 'a'}, 'thread-b': {'model': 'b'}, 'thread-foreign': {'model': 'foreign'}, 'missing': {}})
        app = FastAPI()
        @app.middleware('http')
        async def identity(request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)
        app.include_router(build_threads_router(self.delegate, self.scope))
        self.original_admission = SimpleNamespace(preview=Mock(return_value={'preview':True}),
            admit=AsyncMock(return_value={'ok':True}))
        app.include_router(build_turns_router(self.delegate, self.scope, operator_admission=self.original_admission))
        app.include_router(build_context_router(self.delegate, self.scope))
        app.include_router(build_project_ui_state_router(self.delegate, self.scope))
        self.app = app
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_foreign_thread_scopes_are_denied_before_any_runtime_or_queue_side_effect(self):
        routes = [('GET', ''), ('GET', '/runner'), ('POST', '/name'), ('POST', '/settings'), ('GET', '/settings'),
                  ('POST', '/primary'), ('POST', '/primary-channel'), ('POST', '/archive'),
                  ('POST', '/unarchive'), ('POST', '/resume'), ('POST', '/replace'), ('POST', '/turns'),
                  ('GET', '/queue'), ('POST', '/queue/steer'), ('POST', '/queue/queued-id/steer'),
                  ('GET', '/preflight-attempts'), ('POST', '/preflight-attempts/attempt/retry'),
                  ('GET', '/preflight-attempts/attempt/operator-admission'),
                  ('POST', '/preflight-attempts/attempt/operator-admission'),
                  ('GET', '/context'), ('POST', '/compact')]
        for thread in ['thread-b', 'thread-foreign', 'missing']:
            for method, suffix in routes:
                with self.subTest(thread=thread, route=suffix):
                    response = self.client.request(method, f'/api/threads/{thread}{suffix}?project_id=a',
                        **({'json': {'project_id': 'a', 'message': 'hello', 'name': 'renamed'}} if method == 'POST' else {}))
                    self.assertEqual(response.status_code, 404, response.text)
        self.original_admission.preview.assert_not_called()
        self.original_admission.admit.assert_not_awaited()
        self.assertEqual(self.client.post('/api/turns/interrupt?thread_id=thread-b&project_id=a').status_code, 404)
        for delegate in vars(self.delegate).values():
            delegate.assert_not_called()

    def test_narrow_binding_projection_rejects_foreign_thread_ids(self):
        self.assertEqual(self.client.get('/api/projects/a/ui-state/bindings?thread_id=thread-b').status_code, 404)
        self.delegate.binding_state.assert_not_called()
        self.assertEqual(self.client.get('/api/projects/a/ui-state/bindings?thread_id=thread-a').status_code, 200)
        self.delegate.binding_state.assert_called_once()

    def test_project_list_create_and_settings_collection_use_authenticated_project(self):
        self.assertEqual(self.client.get('/api/threads?project_id=foreign').status_code, 404)
        self.assertEqual(self.client.post('/api/threads?project_id=foreign').status_code, 404)
        self.assertEqual(self.client.get('/api/threads').status_code, 200)
        self.assertEqual(self.delegate.list.call_args.args[0], 'a')
        self.assertEqual(self.client.post('/api/threads?project_id=b').status_code, 200)
        self.assertEqual(self.delegate.create.call_args.kwargs['project_id'], 'b')
        self.assertEqual(self.client.get('/api/thread-settings?project_id=a').json(), {'thread-a': {'model': 'a'}})
        self.assertEqual(self.client.get('/api/thread-settings?project_id=empty').json(), {})

    def test_missing_project_on_existing_thread_resolves_owner_and_conflicts_fail(self):
        self.assertEqual(self.client.post('/api/threads/thread-b/turns', json={'message': 'hello'}).status_code, 200)
        self.assertEqual(self.delegate.start.call_args.args[1].project_id, 'b')
        self.assertEqual(self.client.post('/api/threads/thread-b/resume').status_code, 200)
        self.assertEqual(self.delegate.resume.call_args.kwargs['project_id'], 'b')
        self.delegate.start.reset_mock()
        self.assertEqual(self.client.post('/api/threads/thread-b/turns?project_id=b',
            json={'message': 'hello', 'project_id': 'a'}).status_code, 404)
        for content_type in ['Application/JSON', 'application/vnd.codex+json']:
            self.assertEqual(self.client.post('/api/threads/thread-b/turns?project_id=b',
                content='{"message":"hello","project_id":"a"}',
                headers={'content-type': content_type}).status_code, 404)
        self.delegate.start.assert_not_called()
        self.assertEqual(self.client.get('/api/threads/thread-foreign').status_code, 404)

    def test_canonical_session_ownership_wins_over_legacy_index_and_ambiguous_native_ids_fail(self):
        self.index.upsert(IndexedThread(id='thread-foreign', name='poisoned', cwd='/shared', project_id='a'))
        self.assertEqual(self.client.get('/api/threads/thread-foreign?project_id=a').status_code, 404)
        self.sessions.store.upsert(AgentSession(organization_id='org-a', workspace_id='ws-a', project_id='b',
            provider_id='other', runtime_id='other', runtime_type='other', provider_native_session_id='thread-a'))
        self.assertEqual(self.client.get('/api/threads/thread-a?project_id=a').status_code, 404)

    def test_legacy_project_ids_remain_supported_but_ambiguous_paths_are_not_ownership(self):
        self.index.upsert(IndexedThread(id='legacy-a', name='legacy', cwd='/shared', project_id='a'))
        self.index.upsert(IndexedThread(id='ambiguous', name='legacy', cwd='/shared'))
        self.assertEqual(self.client.get('/api/threads/legacy-a?project_id=a').status_code, 200)
        self.assertEqual(self.client.get('/api/threads/legacy-a?project_id=b').status_code, 404)
        self.assertEqual(self.client.get('/api/threads/ambiguous?project_id=a').status_code, 404)

    def test_project_id_for_thread_fails_closed_for_ambiguous_or_missing_ownership(self):
        self.assertEqual(self.scope.project_id_for_thread('thread-a'), 'a')
        self.index.upsert(IndexedThread(id='legacy-a', name='legacy', cwd='/a', project_id='a'))
        self.index.upsert(IndexedThread(id='ambiguous', name='legacy', cwd='/shared'))
        self.assertEqual(self.scope.project_id_for_thread('legacy-a'), 'a')
        self.assertIsNone(self.scope.project_id_for_thread('ambiguous'))
        self.assertIsNone(self.scope.project_id_for_thread('missing'))

    async def test_shared_path_pagination_filters_before_limit_and_runtime_cannot_relabel_ownership(self):
        runtime = SimpleNamespace(request=AsyncMock(return_value={'data': [
            {'id': 'thread-b', 'name': 'foreign row'}, {'id': 'unknown-native', 'name': 'unclaimed', 'cwd': '/shared'},
            {'id': 'thread-a', 'name': 'updated A'},
        ]}))
        service = ThreadService(runtime_transport=runtime, runtime_request_for_thread=AsyncMock(), event_sink=lambda _: None,
            project_runtime=ProjectRuntimeService(self.project_service), thread_index=self.index, thread_scope=self.scope,
            control_actor=self.actor)
        self.scope.ownership_snapshot = Mock(wraps=self.scope.ownership_snapshot)
        result = await service.list('a', limit=1, actor=self.actor)
        self.assertEqual([row['id'] for row in result['data']], ['thread-a'])
        self.scope.ownership_snapshot.assert_called_once()
        self.assertEqual(self.index.get('thread-b').project_id, 'b')
        self.assertIsNone(self.index.get('unknown-native'))
        result = await service.list('empty', actor=self.actor)
        self.assertEqual(result['data'], [])

    def test_original_admission_payload_cannot_forge_source_actor_or_scope(self):
        path='/api/threads/thread-a/preflight-attempts/attempt/operator-admission?project_id=a'
        payload={'expected_snapshot_sha256':'a'*64,'expected_queue_sha256':'b'*64,'reason':'Reviewed original'}
        for key, value in [('source','operator-retry'),('agent_profile_actor_id','other'),('project_id','b')]:
            self.assertEqual(self.client.post(path,json={**payload,key:value}).status_code,404 if key == 'project_id' else 422)
        self.original_admission.admit.assert_not_awaited()
        self.assertEqual(self.client.get(path).status_code,200)
        self.assertEqual(self.original_admission.preview.call_args.kwargs['project'].id,'a')
        self.assertEqual(self.client.post(path,json=payload).status_code,200)
        self.assertEqual(self.original_admission.admit.await_args.kwargs['actor'].identity_id,self.actor.identity_id)

    def test_runner_route_uses_canonical_scope_without_runtime_start(self):
        self.delegate.runner = AsyncMock(return_value={'id':'thread-a', 'runner':{'state':'cancelled','live':False}})
        response = self.client.get('/api/threads/thread-a/runner?project_id=a')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['runner']['live'])
        self.delegate.runner.assert_awaited_once_with('thread-a')
        self.delegate.start.assert_not_called()

    def test_exact_scope_uses_global_index_and_never_loads_unrelated_history(self):
        database = self.sessions.store.store
        # Populate data-heavy canonical history once, outside the measured read.
        self.sessions.store.update(lambda state: state.model_copy(update={'sessions': [*state.sessions, *[
            AgentSession(id=f'unrelated-{i}', organization_id='org-other', workspace_id='ws-other',
                project_id='foreign', provider_id='other', runtime_id='other', runtime_type='test',
                provider_native_session_id=f'unrelated-native-{i}') for i in range(2000)]]}))
        # Unrelated invalid persisted history must not even be decoded.
        database.record_apply(self.sessions.store.records_namespace,
            upserts={'sessions:unrelated-invalid': {'untrusted': 'history'}})
        with patch.object(database, 'record_items', side_effect=AssertionError('history scan')), \
             patch.object(self.sessions.store, 'list', side_effect=AssertionError('session scan')), \
             patch.object(database, 'record_page', wraps=database.record_page) as page:
            self.assertEqual(self.client.get('/api/threads/thread-a?project_id=a').status_code, 200)
        self.assertEqual(page.call_count, 1)
        self.assertEqual(page.call_args.kwargs, {'key_prefix': 'native:thread-a:', 'limit': 100})

    def test_global_owner_dedup_preserves_rebinds_across_runtimes_and_rejects_foreign_owners(self):
        for owner, provider, runtime in [('a', 'openai', 'codex'), ('a', 'other', 'other'),
                                         ('b', 'openai', 'codex'), ('foreign', 'other', 'codex')]:
            with self.subTest(owner=owner, provider=provider, runtime=runtime):
                project = next(p for p in self.projects if p.id == owner)
                item = AgentSession(organization_id=project.organization_id, workspace_id=project.workspace_id,
                    project_id=owner, provider_id=provider, runtime_id=runtime, runtime_type='test',
                    provider_native_session_id='thread-a')
                self.sessions.store.upsert(item)
                self.assertEqual(self.client.get('/api/threads/thread-a?project_id=a').status_code, 200 if owner == 'a' else 404)
                # Remove through the canonical transactional update, not an alias-only repair.
                self.sessions.store.update(lambda state: state.model_copy(update={
                    'sessions': [s for s in state.sessions if s.id != item.id]}))

    def test_missing_corrupt_and_truncated_aliases_never_authorize_legacy_fallback(self):
        store = self.sessions.store
        database = store.store
        item = store.find_unique_by_native_id('thread-a', provider_id='openai', runtime_id='codex')
        key = store._native_key(item)
        original = database.record_get(store.records_namespace, key)
        for alias in [{'session_id': 'missing-target'}, {'session_id': item.id, 'unexpected': True},
                      {'session_id': []}, {'session_id': ''}]:
            database.record_apply(store.records_namespace, upserts={key: alias})
            self.assertEqual(self.client.get('/api/threads/thread-a?project_id=a').status_code, 404)
        database.record_apply(store.records_namespace, upserts={key: original})
        other = store.find_unique_by_native_id('thread-b', provider_id='openai', runtime_id='codex')
        database.record_apply(store.records_namespace, upserts={key: {'session_id': other.id}})
        self.assertEqual(self.client.get('/api/threads/thread-a?project_id=a').status_code, 404)
        database.record_apply(store.records_namespace, upserts={key: original})
        with patch.object(database, 'record_page', return_value=({}, 'truncated')):
            self.assertEqual(self.client.get('/api/threads/thread-a?project_id=a').status_code, 404)
        self.delegate.read.assert_not_called()

    async def test_real_context_and_scope_storage_do_not_block_event_loop_and_recheck_scope(self):
        entered, release = threading.Event(), threading.Event()
        database = self.sessions.store.store
        original = database.record_page
        phases = []
        # Both scope lookup and context status use real canonical SQLite reads;
        # controlled stalls are below the composed service, never on the loop.
        def slow_page(*args, **kwargs):
            phases.append('scope')
            entered.set()
            if not release.wait(2): raise TimeoutError('controlled scope timeout')
            return original(*args, **kwargs)
        def queue_depth(thread):
            phases.append('context')
            entered.set()
            if not release.wait(2): raise TimeoutError('controlled context timeout')
            database.record_get(self.sessions.store.records_namespace, 'meta')
            return 0
        context = ContextCompactionService(request_for_thread=AsyncMock(), pending_approvals=lambda: {},
            approval_thread_id=lambda r: r.get('threadId'), queue_depth=queue_depth,
            thread_is_active=lambda _: False, raise_if_thread_replaced=lambda _: None, publish_event=AsyncMock())
        app = FastAPI()
        @app.middleware('http')
        async def identity(request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)
        app.include_router(build_context_router(context, self.scope))
        for blocked_phase in ['scope', 'context']:
            phases.clear(); entered.clear(); release.clear()
            with patch.object(database, 'record_items', side_effect=AssertionError('history scan')), \
                 patch.object(database, 'record_page', side_effect=slow_page if blocked_phase == 'scope' else original):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
                    task = asyncio.create_task(client.get('/api/threads/thread-a/context?project_id=a'))
                    ticks = 0
                    try:
                        async with asyncio.timeout(1):
                            while not entered.is_set(): await asyncio.sleep(.002)
                        for _ in range(10):
                            await asyncio.sleep(.005)
                            ticks += 1
                        self.assertFalse(task.done())
                        self.assertEqual(ticks, 10, 'request-loop heartbeat stalled')
                        if blocked_phase == 'context':
                            item = self.sessions.store.find_unique_by_native_id('thread-a', provider_id='openai', runtime_id='codex')
                            self.sessions.store.upsert(item.model_copy(update={'project_id': 'b'}))
                    finally:
                        release.set()
                    response = await asyncio.wait_for(task, 2)
                    self.assertEqual(response.status_code, 200 if blocked_phase == 'scope' else 404, response.text)
            self.assertIn(blocked_phase, phases)

    def test_later_foreign_owner_after_same_owner_aliases_is_not_hidden_by_first_two(self):
        for index, owner in enumerate(['a', 'a', 'foreign']):
            project = next(p for p in self.projects if p.id == owner)
            self.sessions.store.upsert(AgentSession(id=f'rebound-{index}', organization_id=project.organization_id,
                workspace_id=project.workspace_id, project_id=owner, provider_id=f'runtime-{index}',
                runtime_id=f'runtime-{index}', runtime_type='test', provider_native_session_id='rebound-native'))
            if index == 1:
                self.assertEqual(self.client.get('/api/threads/rebound-native?project_id=a').status_code, 200)
                self.assertEqual(self.scope.project_id_for_thread('rebound-native'), 'a')
                self.delegate.read.reset_mock()
        self.index.upsert(IndexedThread(id='rebound-native', name='legacy poison', project_id='a', cwd='/shared'))
        self.assertEqual(self.client.get('/api/threads/rebound-native?project_id=a').status_code, 404)
        self.assertIsNone(self.scope.project_id_for_thread('rebound-native'))
        self.delegate.read.assert_not_called()

    def test_same_owner_alias_budget_exhaustion_is_unknown_without_fallback(self):
        store = self.sessions.store
        store.update(lambda state: state.model_copy(update={'sessions': [*state.sessions, *[
            AgentSession(id=f'retained-{i}', organization_id='org-a', workspace_id='ws-a', project_id='a',
                provider_id='provider', runtime_id=f'runtime-{i}', runtime_type='test',
                provider_native_session_id='many-retained') for i in range(100)]]}))
        self.index.upsert(IndexedThread(id='many-retained', name='legacy', project_id='a', cwd='/shared'))
        self.assertEqual(self.client.get('/api/threads/many-retained?project_id=a').status_code, 200)
        store.upsert(AgentSession(id='retained-100', organization_id='org-a', workspace_id='ws-a', project_id='a',
            provider_id='provider', runtime_id='runtime-100', runtime_type='test', provider_native_session_id='many-retained'))
        self.assertEqual(self.client.get('/api/threads/many-retained?project_id=a').status_code, 404)
        self.assertIsNone(self.scope.project_id_for_thread('many-retained'))

    async def test_supported_canonical_create_adopt_and_resume_keep_same_owner_scope(self):
        from codex_web.agent_runtime import AgentRuntimeResult, AgentRuntimeSessionRequest
        from tests.test_agent_runtime import _Runtime
        runtime = _Runtime()
        runtime.native_id = 'continuation-native'
        self.sessions.registry.register(runtime)
        request = AgentRuntimeSessionRequest(project_id='a', execution_id='original-execution', assignment_id='original-assignment')
        first = await self.sessions.create(provider_id=runtime.provider_id, runtime_id=runtime.runtime_id,
            request=request, actor=self.actor)
        continuation = await self.sessions.create(provider_id=runtime.provider_id, runtime_id=runtime.runtime_id,
            request=request.model_copy(update={'execution_id':'continuation-execution','assignment_id':'continuation-assignment'}), actor=self.actor)
        self.assertNotEqual(first.id, continuation.id)
        from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingService
        from codex_web.storage.thread_bootstrap_bindings import ThreadBootstrapBindingStore
        bindings = ThreadBootstrapBindingService(ThreadBootstrapBindingStore(self.sessions.store.store))
        bindings.bind(bootstrap_id='original-bootstrap', thread_id=runtime.native_id,
            execution_id=first.execution_id, assignment_id=first.assignment_id,
            execution_workspace_id='original-workspace', actor=self.actor)
        rebound = bindings.rebind(bootstrap_id='continuation-bootstrap', thread_id=runtime.native_id,
            execution_id=continuation.execution_id, assignment_id=continuation.assignment_id,
            execution_workspace_id='continuation-workspace', actor=self.actor)
        self.assertEqual(rebound.thread_id, first.provider_native_session_id)
        self.assertEqual(bindings.get_by_thread(runtime.native_id, self.actor).assignment_id, continuation.assignment_id)
        adopted = self.sessions.adopt(provider_id=runtime.provider_id, runtime_id=runtime.runtime_id,
            runtime_type=runtime.runtime_type, provider_native_session_id=runtime.native_id, request=request,
            actor=self.actor, capability_snapshot=runtime.capabilities)
        self.assertIn(adopted.id, {first.id, continuation.id})
        runtime.resume_session = AsyncMock(return_value=AgentRuntimeResult(provider_native_session_id=runtime.native_id))
        resumed = await self.sessions.resume(continuation.id, request=request, actor=self.actor)
        self.assertEqual(resumed.id, continuation.id)
        self.assertEqual(self.client.get('/api/threads/continuation-native?project_id=a').status_code, 200)
        other = _Runtime(); other.runtime_id = 'runtime-other'; other.native_id = runtime.native_id
        self.sessions.registry.register(other)
        await self.sessions.create(provider_id=other.provider_id, runtime_id=other.runtime_id, request=request, actor=self.actor)
        self.assertEqual(self.client.get('/api/threads/continuation-native?project_id=a').status_code, 200)
        self.assertEqual(self.scope.project_id_for_thread(runtime.native_id), 'a')
        await self.sessions.create(provider_id=other.provider_id, runtime_id=other.runtime_id,
            request=request.model_copy(update={'project_id':'b'}), actor=self.actor)
        self.assertEqual(self.client.get('/api/threads/continuation-native?project_id=a').status_code, 404)
        self.assertIsNone(self.scope.project_id_for_thread(runtime.native_id))
