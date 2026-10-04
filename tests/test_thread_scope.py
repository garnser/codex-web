from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from codex_web.agent_runtime import AgentSession
from codex_web.api.context import build_context_router
from codex_web.api.project_ui_state import build_project_ui_state_router
from codex_web.api.threads import build_threads_router
from codex_web.api.turns import build_turns_router
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, PrincipalKind
from codex_web.models import IndexedThread, Project
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
            ['list', 'read', 'rename', 'archive', 'unarchive', 'create', 'interrupt', 'update_primary',
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
        app.include_router(build_turns_router(self.delegate, self.scope))
        app.include_router(build_context_router(self.delegate, self.scope))
        app.include_router(build_project_ui_state_router(self.delegate, self.scope))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_foreign_thread_scopes_are_denied_before_any_runtime_or_queue_side_effect(self):
        routes = [('GET', ''), ('POST', '/name'), ('POST', '/settings'), ('GET', '/settings'),
                  ('POST', '/primary'), ('POST', '/primary-channel'), ('POST', '/archive'),
                  ('POST', '/unarchive'), ('POST', '/resume'), ('POST', '/replace'), ('POST', '/turns'),
                  ('GET', '/queue'), ('POST', '/queue/steer'), ('POST', '/queue/queued-id/steer'),
                  ('GET', '/preflight-attempts'), ('POST', '/preflight-attempts/attempt/retry'),
                  ('GET', '/context'), ('POST', '/compact')]
        for thread in ['thread-b', 'thread-foreign', 'missing']:
            for method, suffix in routes:
                with self.subTest(thread=thread, route=suffix):
                    response = self.client.request(method, f'/api/threads/{thread}{suffix}?project_id=a',
                        **({'json': {'project_id': 'a', 'message': 'hello', 'name': 'renamed'}} if method == 'POST' else {}))
                    self.assertEqual(response.status_code, 404, response.text)
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
