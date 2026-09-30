from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_web.api.secrets import build_secrets_router
from codex_web.configuration import ConfigurationValueKind
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, MembershipRole, PrincipalKind
from codex_web.models import Project, TaskSourceConfiguration
from codex_web.secret_backends import LocalFileSecretBackend
from codex_web.secrets import SecretCreate
from codex_web.services.projects import ProjectService
from codex_web.services.secret_usage import SecretUsageService
from codex_web.services.secrets import SecretBroker
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class ProjectSecretsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.broker = SecretBroker(SecretStateStore(SQLiteStateStore(root / 'state.sqlite3')),
                                   {'local': LocalFileSecretBackend(root / 'material')})
        self.actor = AuthenticationActor(identity_id='admin', principal_kind=PrincipalKind.HUMAN,
            organization_id='org', workspace_id='ws', roles=(MembershipRole.ADMIN,), assurance=AuthenticationAssurance.MFA)
        self.secret = self.broker.create(SecretCreate(name='Shared credential', value='test-only-secret-material'), actor=self.actor)
        sid = self.secret.id
        self.projects = [Project(id=id, name=id, path='/' + id, organization_id='foreign' if id == 'foreign' else 'org', workspace_id='ws',
            authoritative_task_source=TaskSourceConfiguration(source_type='github', source_instance='fixture', scope='repo', credential_secret_id=sid))
            for id in ['a', 'b', 'foreign']]
        self.project_service = ProjectService(NS(load=lambda: self.projects))
        self.config_rows = [NS(id='config-' + scope_id, key='runtime.auth', scope_type=scope, scope_id=scope_id,
            value={'kind': 'secret', 'secret_id': sid}, state='published', revision=1)
            for scope, scope_id in [('project', 'a'), ('project', 'b'), ('workspace', 'ws'), ('workspace', 'other'), ('resource', 'resource-a')]]
        self.service = SecretUsageService(projects=self.project_service,
            resources=NS(list=lambda _: [NS(id='resource-a')], project_resources=lambda project, **_: [NS(id='resource-a')] if project.id == 'a' else []),
            configuration=NS(list_records=lambda: self.config_rows, specs=NS(get=lambda _: NS(value_kind=ConfigurationValueKind.SECRET_REF))),
            providers=NS(list_bindings=lambda _: [NS(id='provider-a', project_id='a', credential_ref=sid, enabled=True)]),
            models=NS(list_providers=lambda _: [NS(id='model-provider', credential_ref=sid, status='active')]),
            agents=NS(list=lambda _: [NS(id='agent-provider', credential_refs=[sid], lifecycle='active')]),
            extensions=NS(list=lambda _: [NS(id='extension', secret_bindings={'auth': sid}, lifecycle='enabled')]),
            actions=NS(list=lambda _: [NS(id='action-a', credential_ref=sid, project_id='a', status='succeeded')]),
            assignments=lambda _: [NS(id='execution-a', secret_refs=[sid], project_id='a', organization_id='org', workspace_id='ws', status='running')],
            connections=lambda: [NS(id='bot-a', project_id='a', bot_token_secret_id=sid, slack_app_token_secret_id=None, signing_secret_secret_id=None, webhook_secret_secret_id=None, bot_token='legacy-private-value')])
        self.broker.usage_service = self.service
        app = FastAPI()
        @app.middleware('http')
        async def identity(request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)
        app.include_router(build_secrets_router(self.broker, self.project_service))
        self.client = TestClient(app); self.addCleanup(self.client.close)

    def test_shared_ownership_consumer_categories_and_outside_project_counts(self):
        listed = self.client.get('/api/secrets?project_id=a')
        self.assertEqual(listed.status_code, 200)
        row = listed.json()['items'][0]
        self.assertEqual(row['scope_type'], 'workspace')
        self.assertTrue(row['use_allowed']); self.assertFalse(row['reveal_api_available'])
        usage = self.client.get(f'/api/secrets/{self.secret.id}/usage?project_id=a')
        self.assertEqual(usage.status_code, 200, usage.text)
        data = usage.json()
        self.assertEqual(data['outside_view_count'], 2)
        self.assertEqual({item['object_type'] for item in data['items']}, {'task_source', 'configuration', 'action_provider', 'model_provider', 'agent_provider', 'extension', 'execution', 'action_intent', 'bot_connection'})
        for forbidden in ['test-only-secret-material', 'legacy-private-value', 'config-b', 'config-other', 'foreign']:
            self.assertNotIn(forbidden, usage.text)
        self.assertTrue(any(item['revision'] == 1 for item in data['items']))

    def test_foreign_and_missing_project_fail_before_all_mutations(self):
        for project in ['foreign', 'missing', '']:
            query = '?project_id=' + project
            self.assertEqual(self.client.get('/api/secrets' + query).status_code, 404)
            self.assertEqual(self.client.post('/api/secrets' + query, json={'name': 'Denied', 'value': 'not-written'}).status_code, 404)
            self.assertEqual(self.client.post(f'/api/secrets/{self.secret.id}/rotate' + query, json={'value': 'not-written'}).status_code, 404)
            self.assertEqual(self.client.delete(f'/api/secrets/{self.secret.id}' + query).status_code, 404)
        self.assertEqual(self.broker.metadata(self.secret.id, actor=self.actor).rotation, 0)

    def test_missing_and_revoked_references_are_actionable_metadata_only(self):
        self.config_rows.append(NS(id='broken-config', key='runtime.auth', scope_type='project', scope_id='a', value={'kind': 'secret', 'secret_id': 'missing-reference'}, state='draft', revision=2))
        self.client.delete(f'/api/secrets/{self.secret.id}?project_id=a')
        response = self.client.get('/api/secrets?project_id=a')
        statuses = {item['secret_id']: item['status'] for item in response.json()['broken_references']}
        self.assertEqual(statuses[self.secret.id], 'revoked')
        self.assertEqual(statuses['missing-reference'], 'missing_or_unavailable')
        self.assertNotIn('test-only-secret-material', response.text)

    def test_non_admin_denials_and_private_secret_metadata(self):
        self.actor = self.actor.model_copy(update={'identity_id': 'reader', 'roles': (MembershipRole.MEMBER,)})
        self.assertEqual(self.client.get('/api/secrets?project_id=a').json()['items'], [])
        self.assertEqual(self.client.get(f'/api/secrets/{self.secret.id}/usage?project_id=a').status_code, 404)
        self.assertEqual(self.client.post('/api/secrets?project_id=a', json={'name': 'Denied', 'value': 'not-written'}).status_code, 403)
        self.assertEqual(self.client.post(f'/api/secrets/{self.secret.id}/rotate?project_id=a', json={'value': 'not-written'}).status_code, 403)
        self.assertEqual(self.client.delete(f'/api/secrets/{self.secret.id}?project_id=a').status_code, 403)

    def test_usage_requires_project_context(self):
        self.assertEqual(self.client.get(f'/api/secrets/{self.secret.id}/usage').status_code, 422)

    def test_scan_exhaustion_never_reports_empty_impact(self):
        self.service.MAX_SCAN = 1
        listed = self.client.get('/api/secrets?project_id=a').json()
        self.assertFalse(listed['impact_available']); self.assertIn('impact_error', listed)
        self.assertEqual(self.client.get(f'/api/secrets/{self.secret.id}/usage?project_id=a').status_code, 503)

    def test_write_only_values_never_return_in_validation_errors_and_mutations_are_audited(self):
        marker = 'test-only-rejected-secret'
        for path, payload in [('/api/secrets', [{'value': marker}]), ('/api/secrets', {'name': {}, 'value': marker}), (f'/api/secrets/{self.secret.id}/rotate', {'value': {'nested': marker}})]:
            response = self.client.post(path + '?project_id=a', json=payload)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(marker, response.text)
        created = self.client.post('/api/secrets?project_id=a', json={'name': 'New', 'value': 'test-create-value'})
        self.assertEqual(created.status_code, 200)
        sid = created.json()['item']['id']
        self.assertEqual(self.client.post(f'/api/secrets/{sid}/rotate?project_id=a', json={'value': 'test-rotate-value'}).status_code, 200)
        self.assertEqual(self.client.delete(f'/api/secrets/{sid}?project_id=a').status_code, 200)
        audit = self.client.get('/api/secrets/audit?project_id=a')
        self.assertEqual({item['action'] for item in audit.json()['items'] if item['secret_id'] == sid}, {'create', 'rotate', 'revoke'})
        self.assertNotIn('test-create-value', audit.text); self.assertNotIn('test-rotate-value', audit.text)
