"""Regression coverage for public error projections flagged by CodeQL."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_web import devstatus
from codex_web.api.identity import install_identity_middleware
from codex_web.api.authorization import ApiAuthorizationError
from codex_web.api.automations import build_automations_router
from codex_web.definitions import DefinitionScope
from codex_web.services.automation_definitions import AutomationScheduleMaterializationError


PRIVATE = 'private-token /internal/path'


class PublicErrorTests(unittest.TestCase):
    def test_devstatus_read_failure_does_not_disclose_exception(self):
        errors = []
        with patch.object(Path, 'read_text', side_effect=OSError(PRIVATE)), patch.object(Path, 'exists', return_value=True):
            result = devstatus._load_json_file(Path('/fixture'), errors=errors, label='health report')
        self.assertIsNone(result)
        self.assertIn('Unable to read health report', errors[0])
        self.assertNotIn(PRIVATE, str(errors))

    def test_devstatus_refresh_failure_preserves_fallback_without_exception(self):
        errors = []
        with patch.object(devstatus, '_refresh_dev_health_report', side_effect=RuntimeError(PRIVATE)), patch.object(
            devstatus, '_load_json_file', return_value={'cached': True}
        ):
            result = devstatus._load_dev_health_report(force_refresh=True, errors=errors)
        self.assertEqual(result, {'cached': True})
        self.assertIn('Unable to refresh', errors[0])
        self.assertNotIn(PRIVATE, str(errors))

    def test_authorization_denial_retains_status_without_exception_payload(self):
        for status in (401, 403):
            with self.subTest(status=status):
                app = FastAPI()
                identity = Mock()
                identity.local_trusted_actor.return_value = SimpleNamespace(organization_id='org', workspace_id='ws', tenant=None)
                app.state.api_authorization_service = Mock()
                app.state.api_authorization_service.authorize_request.side_effect = ApiAuthorizationError(PRIVATE, status_code=status)
                install_identity_middleware(app, identity)
                with patch.dict('os.environ', {'CODEX_WEB_IDENTITY_MODE': 'local-trusted'}):
                    response = TestClient(app).get('/api/fixture')
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json(), {'detail': 'API authorization denied'})

    def test_schedule_repair_error_preserves_successful_publication(self):
        app = FastAPI()
        actor = SimpleNamespace(principal_kind='service', service_scopes=('definitions:admin',),
                                workspace_id='ws', organization_id='org', identity_id='operator')
        @app.middleware('http')
        async def identity(request, call_next):
            request.state.identity_actor = actor
            return await call_next(request)
        record = Mock(kind='automation', scope_type=DefinitionScope.WORKSPACE, scope_id='ws', record_id='draft')
        definitions = Mock()
        definitions.registry.get_record.return_value = record
        definitions.publish_draft.return_value.model_dump.return_value = {'record_id': 'published'}
        schedules = Mock()
        schedules.reconcile.side_effect = AutomationScheduleMaterializationError(PRIVATE)
        app.include_router(build_automations_router(definitions, Mock(), Mock(), schedules))
        response = TestClient(app).post('/api/automations/drafts/draft/publish', json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['record'], {'record_id': 'published'})
        self.assertIsNone(response.json()['schedule'])
        self.assertIn('retry reconciliation', response.json()['scheduleError'])
        self.assertNotIn(PRIVATE, response.text)
