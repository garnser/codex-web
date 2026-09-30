from __future__ import annotations

import unittest
from types import SimpleNamespace
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.model_gateway import build_model_gateway_router
from codex_web.identity import AuthenticationAssurance
from codex_web.model_gateway import ModelProviderUpsert, ModelInvocationRecord
from codex_web.services.model_gateway import ModelGatewayService, ModelRegistryConflictError
from codex_web.services.model_provider_administration import ModelProviderAdministration


class ModelProviderAdministrationTests(unittest.TestCase):
    def setUp(self):
        from test_agent_providers import AgentProviderServiceTests
        AgentProviderServiceTests.setUp(self)
        self.addCleanup(self.temp.cleanup)
        self.gateway = ModelGatewayService(self.models)
        self.payload = ModelProviderUpsert(id='model-one', display_name='Model provider', adapter_type='mock')
        self.gateway.upsert_provider(self.payload, actor=self.actor)
        self.admin = ModelProviderAdministration(self.gateway, agents=self.store,
            profiles=SimpleNamespace(load=lambda: SimpleNamespace(revisions=[])))

    def test_provider_fingerprint_conflict_does_not_overwrite_current_configuration(self):
        impact = self.admin.impact('model-one', self.actor)
        changed = self.payload.model_copy(update={'status': 'disabled'})
        result = self.gateway.upsert_provider(changed, actor=self.actor, expected_revision=impact['expected_revision'])
        self.assertEqual(result.status, 'disabled')
        with self.assertRaises(ModelRegistryConflictError):
            self.gateway.upsert_provider(self.payload, actor=self.actor, expected_revision=impact['expected_revision'])
        self.assertEqual(self.gateway.list_providers(self.actor)[0].status, 'disabled')

    def test_retained_invocation_impact_is_metadata_only_and_tenant_filtered(self):
        def apply(state):
            common = dict(organization_id=self.actor.organization_id, workspace_id=self.actor.workspace_id,
                actor_id=self.actor.identity_id, model_class='fast', purpose='test', prompt_template_id='prompt',
                prompt_template_version='1', prompt_template_checksum_sha256='a'*64, rendered_prompt_sha256='b'*64,
                message_count=1, input_character_count=20, policy_fingerprint_sha256='c'*64, route_reason='picked',
                status='succeeded', selected_provider_id='model-one')
            state.invocations = [ModelInvocationRecord(id='invocation-local', **common), ModelInvocationRecord(id='invocation-foreign', **{**common, 'workspace_id': 'other'})]
            return state
        self.models.update(apply)
        result = self.admin.impact('model-one', self.actor)
        self.assertTrue(result['available']); self.assertEqual(result['total'], 1)
        self.assertEqual(result['consumers'][0]['id'], 'invocation-local')
        self.assertNotIn('invocation-foreign', str(result)); self.assertNotIn('rendered_prompt', str(result))
        self.assertIn('remote provider accounts', result['effect'])

    def test_incomplete_inventory_and_low_assurance_are_read_only(self):
        self.admin.profiles = None
        result = self.admin.impact('model-one', self.actor)
        self.assertFalse(result['available']); self.assertFalse(result['can_manage'])
        self.assertIsNone(result['total'])
        low = self.actor.model_copy(update={'assurance': AuthenticationAssurance.PRIMARY})
        self.assertFalse(self.admin.catalog(low)['can_manage'])

    def test_api_current_revision_and_assurance_requirements_are_enforced(self):
        actor = self.actor
        app = FastAPI()
        @app.middleware('http')
        async def identity(request: Request, call_next):
            request.state.identity_actor = actor
            return await call_next(request)
        app.include_router(build_model_gateway_router(self.gateway, administration=self.admin))
        with TestClient(app) as client:
            impact = client.get('/api/model-gateway/providers/model-one/impact').json()
            result = client.put('/api/model-gateway/providers/model-one', params={'expected_revision': impact['expected_revision']}, json=self.payload.model_dump(mode='json'))
            self.assertEqual(result.status_code, 200)
            self.assertEqual(client.put('/api/model-gateway/providers/model-one', params={'expected_revision': impact['expected_revision']}, json=self.payload.model_dump(mode='json')).status_code, 409)
            actor = self.actor.model_copy(update={'assurance': AuthenticationAssurance.PRIMARY})
            self.assertFalse(client.get('/api/model-gateway/provider-administration').json()['can_manage'])
            self.assertEqual(client.put('/api/model-gateway/providers/model-one', json=self.payload.model_dump(mode='json')).status_code, 403)
            self.assertEqual(client.get('/api/model-gateway/providers?project_id=missing').status_code, 404)
