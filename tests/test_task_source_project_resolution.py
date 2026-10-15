"""Exercise configured-source HTTP paths through the application service composition."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_web.api.work_items import build_work_items_router
from codex_web.identity import Membership, MembershipRole, TenantScope
from codex_web.integrations.github_client import GitHubClient
from codex_web.models import GitHubTaskSourceSettings, Project, TaskSourceConfiguration, TaskSourceIdentity, WorkItemState
from codex_web.secret_backends import LocalFileSecretBackend
from codex_web.secrets import SecretCreate
from codex_web.services.identity import IdentityService
from codex_web.services.github_task_source import GitHubTaskSource
from codex_web.services.task_sources import TaskSourceCapabilities, TaskSourceCapability
from codex_web.services.secrets import SecretBroker
from codex_web.services.task_source_runtime import TaskSourceResolutionError
from codex_web.services.work_item_operator import WorkItemOperatorService
from codex_web.services.work_items import WorkItemService
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests.test_work_item_operator import _Host


class ProjectSourceResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        sqlite = SQLiteStateStore(root / 'state.sqlite3')
        self.identity = IdentityService(IdentityStateStore(sqlite))
        self.actor = self.identity.local_trusted_actor()
        self.secrets = SecretBroker(SecretStateStore(sqlite),
            {'local': LocalFileSecretBackend(root / 'secret-material')})
        self.secret = self.secrets.create(SecretCreate(name='Fixture GitHub',
            provider='github', value='fixture-token',
            allowed_identity_ids=['service-task-source-runtime']), actor=self.actor)
        self.config = TaskSourceConfiguration(source_type='github',
            source_instance='https://api.github.test', scope='acme/app',
            credential_secret_id=self.secret.id,
            provider_settings=GitHubTaskSourceSettings(assignee_mapping={'james': 'engineer'}))
        self.project = Project(id='project-a', name='A', path=str(root),
            authoritative_task_source=self.config)
        self.host = _Host(root, self.project)
        # Both pauses are inert fixture state; explicit sync must never dispatch.
        self.host.autonomy_paused = True
        self.host.work_paused = True
        self.service = WorkItemService(self.host, identity_service=self.identity,
            secret_broker=self.secrets)
        self.operator = WorkItemOperatorService(self.service)
        self.calls = []
        def respond(request):
            self.calls.append((request.method, request.url.path))
            self.assertEqual(request.method, 'GET')
            self.assertEqual(request.url.path, '/repos/acme/app/issues')
            self.assertEqual(request.headers['authorization'], 'Bearer fixture-token')
            self.assertLessEqual(len(self.calls), 1)
            return httpx.Response(200, json=[{'number': 42, 'title': 'Current issue',
                'body': 'Bounded fixture', 'state': 'open', 'labels': [],
                'assignees': [{'login': 'engineer'}], 'updated_at': '2026-10-14T00:00:00Z',
                'html_url': 'https://github.test/acme/app/issues/42'}])
        self.service.builtin_task_source_runtime.github_client = GitHubClient(
            transport=httpx.MockTransport(respond))
        app = FastAPI()
        @app.middleware('http')
        async def authenticated_fixture(request, call_next):
            request.state.identity_actor = self.actor
            request.state.tenant_scope = self.actor.tenant
            return await call_next(request)
        app.include_router(build_work_items_router(self.service))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def catalog_item(self):
        response = self.client.get('/api/task-sources')
        self.assertEqual(response.status_code, 200)
        return next(item for item in response.json()['items']
                    if item['project_id'] == self.project.id)

    def sync(self):
        return self.client.post('/api/work-items/sync/project-a', json={'reason': 'fixture'})

    def test_catalog_resolves_without_a_work_item(self):
        item = self.catalog_item()
        self.assertTrue(item['available'], item)
        self.assertIn('discovery', item['capabilities'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.host.states, {})

    def test_sync_imports_through_projector_without_dispatch_or_provider_writes(self):
        response = self.sync()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['synced'], 1)
        state = self.host.states['acme/app#42']
        self.assertEqual(state.project_id, self.project.id)
        self.assertEqual(state.source_identity.external_id, 'acme/app#42')
        self.assertEqual(state.title, 'Current issue')
        self.assertEqual(state.current_owner, 'james')
        self.assertEqual(self.host.dispatches, [])
        self.assertTrue(self.host.autonomy_paused and self.host.work_paused)
        self.assertEqual(self.calls, [('GET', '/repos/acme/app/issues')])
        self.assertEqual(self.host.hub.events[-1]['type'], 'work-item.sync')

    def assert_unavailable(self):
        self.assertFalse(self.catalog_item()['available'])
        response = self.sync()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()['detail']['code'], 'task_source_unavailable')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.host.states, {})
        self.assertEqual(self.host.dispatches, [])

    def test_missing_project_factory_does_not_fall_back_to_item_factory(self):
        del self.service.task_source_registry._project_factories['github']
        self.assert_unavailable()

    def test_missing_credential_is_truthfully_unavailable(self):
        self.project.authoritative_task_source = self.config.model_copy(
            update={'credential_secret_id': None})
        self.assert_unavailable()

    def test_foreign_credential_is_denied_without_http_or_material(self):
        self.move_project_to_nondefault_tenant()
        # This reference belongs to local/default; Project scope cannot use it.
        self.assert_unavailable()
        self.assertNotIn('fixture-token', str(self.catalog_item()))

    def move_project_to_nondefault_tenant(self):
        self.identity.create_organization(name='Other', organization_id='org-b')
        self.identity.create_workspace(organization_id='org-b', name='B', workspace_id='ws-b')
        self.identity.add_membership(Membership(identity_id=self.actor.identity_id,
            principal_kind=self.actor.principal_kind, organization_id='org-b',
            workspace_id='ws-b', roles=[MembershipRole.ADMIN]))
        self.actor = self.identity.actor_for_identity(self.actor.identity_id,
            scope=TenantScope(organization_id='org-b', workspace_id='ws-b'))
        self.project.organization_id = 'org-b'
        self.project.workspace_id = 'ws-b'

    def test_valid_nondefault_tenant_uses_its_own_credential_and_projection_scope(self):
        self.move_project_to_nondefault_tenant()
        secret = self.secrets.create(SecretCreate(name='Scoped GitHub', provider='github',
            value='fixture-token', allowed_identity_ids=['service-task-source-runtime']),
            actor=self.actor)
        self.project.authoritative_task_source = self.config.model_copy(
            update={'credential_secret_id': secret.id})
        self.assertTrue(self.catalog_item()['available'])
        response = self.sync()
        self.assertEqual(response.status_code, 200, response.text)
        state = self.host.states['acme/app#42']
        self.assertEqual((state.organization_id, state.workspace_id), ('org-b', 'ws-b'))
        self.assertEqual(self.host.dispatches, [])

    def test_foreign_tenant_cannot_catalog_or_sync_project(self):
        self.project.organization_id = 'foreign-org'
        self.project.workspace_id = 'foreign-ws'
        response = self.client.get('/api/task-sources')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(any(item['project_id'] == self.project.id
                             for item in response.json()['items']))
        self.assertEqual(self.sync().status_code, 404)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.host.states, {})

    def test_foreign_project_configuration_and_missing_project_fail_closed(self):
        foreign = Project(id='project-b', name='B', path=self.temp.name,
            authoritative_task_source=self.config.model_copy(update={'scope': 'other/repo'}))
        self.host.projects.append(foreign)
        with self.assertRaises(TaskSourceResolutionError):
            self.operator._source_for_configuration(foreign.id, self.config, required=True)
        with self.assertRaises(TaskSourceResolutionError):
            self.operator._source_for_configuration(self.project.id, foreign.authoritative_task_source, required=True)
        with self.assertRaises(TaskSourceResolutionError):
            self.operator._source_for_configuration('missing', self.config, required=True)
        self.assertEqual(self.calls, [])

    def test_binding_change_between_operator_and_factory_is_rechecked(self):
        stale = self.project.model_copy(deep=True)
        stale.authoritative_task_source = self.config.model_copy(update={'scope': 'other/repo'})
        # Real factory sees a newly persisted binding after the operator snapshot.
        self.service.builtin_task_source_runtime.load_projects = lambda: [stale]
        self.assert_unavailable()
        self.assertIn('binding changed', self.sync().json()['detail']['message'])

    def test_unsupported_discovery_is_typed_and_performs_no_http(self):
        with patch.object(GitHubTaskSource, 'capabilities',
                          TaskSourceCapabilities(frozenset({TaskSourceCapability.READ}))):
            item = self.catalog_item()
            self.assertTrue(item['available'])
            self.assertNotIn('discovery', item['capabilities'])
            response = self.sync()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail'],
            {'code': 'task_source_capability_unsupported', 'capability': 'discovery'})
        self.assertEqual(self.calls, [])
        self.assertEqual(self.host.states, {})

    def test_actual_item_identity_validation_remains_strict(self):
        for external_id in ('__configuration_probe__', 'other/repo#42', 'acme/app#invalid'):
            with self.subTest(external_id=external_id):
                state = WorkItemState(ref='fixture', project_id=self.project.id,
                    source_identity=TaskSourceIdentity(source_type='github',
                        source_instance=self.config.source_instance, external_id=external_id),
                    last_meaningful_update_at=1, created_at=1, updated_at=1)
                with self.assertRaises(TaskSourceResolutionError):
                    self.service.task_source_registry.resolve(state, required=True)
        state.source_identity = state.source_identity.model_copy(update={'external_id': 'acme/app#42'})
        self.assertIsNotNone(self.service.task_source_registry.resolve(state, required=True))
        self.assertEqual(self.calls, [])

    def test_enterprise_catalog_and_sync_keep_registered_project_factories(self):
        from codex_web.models import JiraTaskSourceSettings, ServiceNowTaskSourceSettings
        from codex_web.integrations.gitlab_client import GitLabClient
        from tests.test_builtin_task_source_runtime import _JiraClient, _ServiceNowClient
        runtime = self.service.builtin_task_source_runtime
        runtime.jira_client = _JiraClient()
        runtime.servicenow_client = _ServiceNowClient()
        gitlab_calls = []
        def gitlab_response(request):
            gitlab_calls.append(request.method)
            self.assertEqual(request.method, 'GET')
            self.assertEqual(request.headers['PRIVATE-TOKEN'], 'fixture-token')
            return httpx.Response(200, json=[{'iid': 42, 'title': 'GitLab issue',
                'state': 'opened', 'references': {'full': 'acme/app#42'},
                'labels': [], 'assignees': [], 'updated_at': '2026-10-14T00:00:00Z'}])
        self.service.gitlab = GitLabClient(transport=httpx.MockTransport(gitlab_response))
        for provider, settings in [('gitlab', None), ('jira', JiraTaskSourceSettings()),
                                   ('servicenow', ServiceNowTaskSourceSettings())]:
            with self.subTest(provider=provider):
                self.host.states.clear()
                secret = self.secrets.create(SecretCreate(name=provider, provider=provider,
                    value='fixture-token', allowed_identity_ids=['service-task-source-runtime']),
                    actor=self.actor)
                self.project.authoritative_task_source = TaskSourceConfiguration(
                    source_type=provider, source_instance='https://provider.test', scope='acme',
                    credential_secret_id=secret.id, provider_settings=settings)
                self.assertTrue(self.catalog_item()['available'])
                response = self.sync()
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['synced'], 1)
                self.assertEqual(next(iter(self.host.states.values())).source_identity.source_type, provider)
        self.assertEqual(gitlab_calls, ['GET'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.host.dispatches, [])
