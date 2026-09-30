from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_web.api.execution_profiles import build_execution_profiles_router
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, PrincipalKind
from codex_web.models import IndexedThread, Project, ThreadRunSettings
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.execution_profile_definitions import install_execution_profile_definitions
from codex_web.services.execution_profile_usage import ExecutionProfileUsageService
from codex_web.services.projects import ProjectService
from codex_web.services.thread_scope import ThreadScopeService
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.thread_index import ThreadIndexRepository


class ExecutionProfileUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        store = SQLiteStateStore(root / 'state.sqlite3')
        self.catalogs = install_execution_profile_definitions(DefinitionRegistryService(DefinitionRegistryStore(store)))
        self.actor = AuthenticationActor(identity_id='reader', principal_kind=PrincipalKind.HUMAN,
            organization_id='org', workspace_id='ws', assurance=AuthenticationAssurance.MFA)
        projects = [Project(id=id, name=id, path='/' + id,
                            organization_id='other' if id == 'foreign' else 'org', workspace_id='ws')
                    for id in ['a', 'b', 'foreign']]
        self.projects = ProjectService(SimpleNamespace(load=lambda: projects))
        index = ThreadIndexRepository(store, root / 'threads.json')
        for project in projects:
            index.upsert(IndexedThread(id='thread-' + project.id, name='Thread', cwd=project.path, project_id=project.id))
        thread_scope = ThreadScopeService(self.projects, SimpleNamespace(store=SimpleNamespace(list=lambda: [])), index)
        self.profile_rows = [SimpleNamespace(profile_id=id, name=name, lifecycle='active', revision=1,
            execution_profile_id='repository-write', visible=visible)
            for id, name, visible in [('visible', 'Visible profile', True), ('private', 'Restricted identity', False)]]
        profiles = SimpleNamespace(store=SimpleNamespace(list_revisions=lambda **_: self.profile_rows),
                                   can_view=lambda item, **_: item.visible)
        self.settings = {'thread-' + project.id: ThreadRunSettings(execution_profile_id='repository-write') for project in projects}
        self.queues = {'thread-a': [SimpleNamespace(id='queued-a', project_id='a', execution_profile_id=None, message='private prompt')],
                       'thread-b': [SimpleNamespace(id='queued-b', project_id='b', execution_profile_id='repository-write')]}
        self.assignments = [SimpleNamespace(id='assignment-' + project.id, organization_id=project.organization_id,
            workspace_id=project.workspace_id, project_id=project.id, execution_profile_id='repository-write',
            status='running', execution_profile_definition=SimpleNamespace(revision=1)) for project in projects]
        self.service = ExecutionProfileUsageService(catalogs=self.catalogs, projects=self.projects, profiles=profiles,
            assignments=lambda _: self.assignments, queues=SimpleNamespace(list_queues=lambda: self.queues),
            settings=lambda: self.settings, thread_scope=thread_scope)
        self.catalogs.usage_loader = self.service.snapshot
        app = FastAPI()
        @app.middleware('http')
        async def identity(request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)
        app.include_router(build_execution_profiles_router(self.catalogs, self.projects))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_usage_is_project_scoped_and_does_not_expose_private_consumers_or_prompt_content(self):
        response = self.client.get('/api/execution-profiles/repository-write/usage?project_id=a')
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data['schema_version'], '1.0')
        self.assertEqual(data['project_id'], 'a')
        self.assertEqual(data['count'], 6)
        self.assertEqual(data['restricted_count'], 1)
        self.assertEqual({item['object_id'] for item in data['items']},
                         {'a', 'visible', 'thread-a', 'queued-a', 'assignment-a'})
        self.assertNotIn('Restricted identity', response.text)
        self.assertNotIn('private prompt', response.text)
        self.assertNotIn('thread-b', response.text)
        self.assertEqual(next(item for item in data['items'] if item['object_type'] == 'execution')['binding'], 'pinned')
        self.assertEqual(data['definition']['record_id'], self.catalogs.record(project_id='a').record_id)

    def test_project_context_and_profile_identity_are_required_and_foreign_scopes_fail(self):
        for path, status in [('/api/execution-profiles/repository-write/usage', 422),
                             ('/api/execution-profiles/repository-write/usage?project_id=foreign', 404),
                             ('/api/execution-profiles/missing/usage?project_id=a', 404)]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, status)

    def test_unavailable_or_over_budget_usage_is_explicit_and_never_reported_as_empty(self):
        self.catalogs.usage_loader = None
        response = self.client.get('/api/execution-profiles/repository-write/usage?project_id=a')
        self.assertEqual(response.status_code, 503)
        self.catalogs.usage_loader = self.service.snapshot
        self.service.MAX_SCAN = 1
        response = self.client.get('/api/execution-profiles/repository-write/usage?project_id=a')
        self.assertEqual(response.status_code, 503)
        self.assertIn('bounded scan', response.json()['detail'])

    def test_display_limit_retains_total_and_restricted_counts(self):
        self.service.MAX_DISPLAY = 1
        data = self.service.snapshot('repository-write', 'a', self.actor)
        self.assertEqual(len(data['items']), 1)
        self.assertTrue(data['truncated'])
        self.assertEqual(data['count'], 6)
        self.assertEqual(data['restricted_count'], 1)

    def test_latest_profile_revision_replaces_historical_configuration_in_current_usage(self):
        self.profile_rows.append(SimpleNamespace(profile_id='visible', name='Changed profile', lifecycle='active', revision=2,
            execution_profile_id='orchestration-only', visible=True))
        data = self.service.snapshot('repository-write', 'a', self.actor)
        self.assertNotIn('visible', [item['object_id'] for item in data['items']])
        data = self.service.snapshot('orchestration-only', 'a', self.actor)
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['items'][0]['revision'], 2)
