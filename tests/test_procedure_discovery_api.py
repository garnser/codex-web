import unittest
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from codex_web.api.procedure_discovery import build_procedure_discovery_router
from codex_web.action_providers import ActionRequest
from codex_web.identity import TenantScope
from codex_web.services.action_intents import ActionIntentService
from tests import test_procedure_action_admission as fixtures


class ProcedurePreviewApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ProcedureActionAdmissionTests.asyncSetUp
    asyncTearDown = fixtures.ProcedureActionAdmissionTests.asyncTearDown

    async def test_preview_uses_authenticated_actor_and_does_not_execute_or_create_intent(self):
        app = FastAPI()
        @app.middleware('http')
        async def authenticate(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)
        projects = SimpleNamespace(get=lambda project_id, scope: None)
        app.include_router(build_procedure_discovery_router(self.discovery, self.execution, self.service, projects))
        payload = {'execution_id':self.assignment.execution_id, 'binding_id':self.ops_binding.id, 'request':{
            'action_id':'service.rotate', 'organization_id':'local', 'workspace_id':'default', 'resource_ids':[self.target.id]}}
        with TestClient(app) as client:
            response = client.post('/api/skills/procedure-applicability', json=payload)
            self.assertEqual(response.status_code,200,response.text)
            data = response.json()
            self.assertFalse(data['authorizes_mutation'])
            self.assertEqual(data['discovery']['outcome'],'matched')
            self.assertEqual(data['discovery']['selected'][0]['record_id'],self.published['recordId'])
            payload['task'] = 'operator.preview'
            payload['expected_environment'] = 'production'
            conflict = client.post('/api/skills/procedure-applicability', json=payload)
            self.assertEqual(conflict.status_code, 200, conflict.text)
            self.assertEqual(conflict.json()['input_context']['task'], 'operator.preview')
            self.assertNotEqual(conflict.json()['input_context']['environment'], 'production')
            self.assertIn('procedure_preview_environment_conflict', conflict.json()['discovery']['blockers'])
            self.assertFalse(conflict.json()['authorizes_mutation'])
            payload['task'] = 'invalid task with spaces'
            self.assertEqual(client.post('/api/skills/procedure-applicability', json=payload).status_code, 422)
            payload.pop('task')
            payload['request']['workspace_id'] = 'foreign'
            denied = client.post('/api/skills/procedure-applicability',json=payload)
            self.assertEqual(denied.status_code,403)
        self.assertEqual(self.ops.calls,[])
        self.assertEqual(self.service.store.load().intents,[])
