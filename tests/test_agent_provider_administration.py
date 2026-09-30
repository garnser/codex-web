from __future__ import annotations

import unittest
from types import SimpleNamespace
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.agent_providers import AgentProviderUpsert
from codex_web.identity import AuthenticationAssurance
from codex_web.services.agent_provider_administration import AgentProviderAdministration
from codex_web.services.agent_providers import AgentProviderConflictError
from codex_web.api.agent_providers import build_agent_providers_router


class AgentProviderAdministrationTests(unittest.TestCase):
    def setUp(self):
        from test_agent_providers import AgentProviderServiceTests
        AgentProviderServiceTests.setUp(self)
        self.addCleanup(self.temp.cleanup)
        self.payload = AgentProviderUpsert(id='runner', display_name='Runner', declared_capabilities=('agent_execution',), granted_capabilities=('agent_execution',))
        self.service.upsert(self.payload, actor=self.actor)
        self.sessions = []
        self.profiles = []
        self.config = []
        self.definitions = []
        self.admin = AgentProviderAdministration(self.service,
            runtimes=SimpleNamespace(list_registrations=lambda: (SimpleNamespace(provider_id='runner', runtime_id='cli', capability_revision=2),)),
            sessions=SimpleNamespace(list=lambda: self.sessions),
            profiles=SimpleNamespace(load=lambda: SimpleNamespace(revisions=self.profiles)),
            configuration=SimpleNamespace(load=lambda: self.config),
            definitions=SimpleNamespace(load=lambda: self.definitions),
            projects=SimpleNamespace(list=lambda scope: [SimpleNamespace(id='owned')]),
            resources=SimpleNamespace(list=lambda actor: []))

    def test_atomic_revision_checks_preserve_current_binding_and_creation_boundary(self):
        updated = self.service.upsert(self.payload.model_copy(update={'display_name': 'New name'}), actor=self.actor, expected_revision=1)
        self.assertEqual(updated.revision, 2)
        with self.assertRaises(AgentProviderConflictError):
            self.service.upsert(self.payload.model_copy(update={'lifecycle': 'disabled'}), actor=self.actor, expected_revision=1)
        self.assertEqual(self.service.get('runner', self.actor).display_name, 'New name')
        with self.assertRaises(AgentProviderConflictError):
            self.service.upsert(self.payload, actor=self.actor, expected_revision=0)
        self.service.upsert(self.payload.model_copy(update={'id': 'new'}), actor=self.actor, expected_revision=0)

    def test_impact_is_read_only_scoped_and_does_not_claim_remote_control(self):
        base = dict(organization_id=self.actor.organization_id, workspace_id=self.actor.workspace_id,
                    provider_id='runner', project_id='owned', status=SimpleNamespace(value='running'))
        self.sessions[:] = [SimpleNamespace(**base, id='session-local'), SimpleNamespace(**{**base, 'workspace_id': 'other'}, id='session-foreign')]
        result = self.admin.impact('runner', self.actor)
        self.assertTrue(result['available'])
        self.assertEqual(result['total'], 2)
        self.assertIn('session-local', str(result['consumers']))
        self.assertNotIn('session-foreign', str(result))
        self.assertIn('does not cancel', result['effect'])
        self.assertEqual(self.service.get('runner', self.actor).revision, 1)

    def test_corrupt_missing_and_overbound_inventory_never_become_zero_consumers(self):
        self.admin.sessions = None
        result = self.admin.impact('runner', self.actor)
        self.assertFalse(result['available']); self.assertFalse(result['can_manage']); self.assertIsNone(result['total'])
        self.admin.sessions = SimpleNamespace(list=lambda: [None] * 5001)
        self.assertFalse(self.admin.impact('runner', self.actor)['available'])

    def test_display_bound_keeps_total_and_marks_truncation(self):
        self.sessions[:] = [SimpleNamespace(id=str(index), provider_id='runner', project_id='owned',
                organization_id=self.actor.organization_id, workspace_id=self.actor.workspace_id,
                status=SimpleNamespace(value='closed')) for index in range(110)]
        result = self.admin.impact('runner', self.actor)
        self.assertTrue(result['available']); self.assertTrue(result['truncated'])
        self.assertEqual(result['total'], 111); self.assertEqual(len(result['consumers']), 100)

    def test_published_configuration_and_routing_definitions_filter_scope_and_expose_only_metadata(self):
        def config(key, scope, target, value):
            return SimpleNamespace(key=key, scope_type=SimpleNamespace(value=scope), scope_id=target,
                value=value, state=SimpleNamespace(value='published'), revision=3)
        self.config[:] = [config('agent.routing.preferred_provider_ids', 'project', 'owned', ['runner']),
                         config('agent.routing.preferred_provider_ids', 'project', 'foreign', ['runner']),
                         config('unrelated.secret.key', 'global', None, 'must-not-render')]
        self.definitions[:] = [SimpleNamespace(kind='agent-routing-policy', lifecycle=SimpleNamespace(value='published'),
            scope_type=SimpleNamespace(value='workspace'), scope_id=self.actor.workspace_id, definition_id='routing.default', revision=4,
            payload={'roles': [{'role_id': 'engineer', 'preferred_provider_ids': ['runner']}]})]
        result = self.admin.impact('runner', self.actor)
        self.assertTrue(result['available']); self.assertEqual(result['total'], 3)
        self.assertNotIn('foreign', str(result)); self.assertNotIn('must-not-render', str(result))
        self.assertIn('routing.default', str(result['consumers']))

    def test_synthesized_model_provider_retains_model_gateway_owner(self):
        from test_agent_providers import AgentProviderServiceTests
        AgentProviderServiceTests._model_provider(self, 'model-only')
        provider = next(item for item in self.service.list(self.actor) if item.synthesized_from_model_gateway)
        result = self.admin.impact(provider.id, self.actor)
        self.assertTrue(result['available']); self.assertFalse(result['can_manage'])
        self.assertEqual(result['owner'], 'model_gateway')

    def test_low_assurance_metadata_is_read_only_and_put_requires_current_admin_authority(self):
        actor = self.actor.model_copy(update={'assurance': AuthenticationAssurance.PRIMARY})
        self.assertFalse(self.admin.catalog(actor)['can_manage'])
        app = FastAPI()
        @app.middleware('http')
        async def identity(request: Request, call_next):
            request.state.identity_actor = actor
            return await call_next(request)
        app.include_router(build_agent_providers_router(self.service, administration=self.admin))
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/agent-providers/administration').status_code, 200)
            self.assertFalse(client.get('/api/agent-providers/runner/impact').json()['can_manage'])
            self.assertEqual(client.put('/api/agent-providers/runner?expected_revision=1', json=self.payload.model_dump(mode='json')).status_code, 403)
            self.assertEqual(client.get('/api/agent-providers/administration?project_id=missing').status_code, 404)
            actor = self.actor
            self.assertEqual(client.put('/api/agent-providers/runner?expected_revision=1', json=self.payload.model_dump(mode='json')).status_code, 200)
            self.assertEqual(client.put('/api/agent-providers/runner?expected_revision=1', json=self.payload.model_dump(mode='json')).status_code, 409)
