"""Operator inspection/authentication must leave native lease callbacks runnable."""
import asyncio
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from fastapi import FastAPI, Request

from codex_web.api.identity import SESSION_COOKIE, install_identity_middleware
from codex_web.api.control_plane_broker import build_control_plane_broker_router
from codex_web.api.authorization import ApiAuthorizationError
from codex_web.identity import AuthenticationActor
from codex_web.services.identity import AuthenticationError


class OperatorHttpResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.actor = AuthenticationActor(identity_id='operator', principal_kind='human',
            organization_id='org', workspace_id='space', roles=('admin',), assurance='mfa')

    def lease_callback_gate(self, result):
        """A loop callback must run while the synchronous collaborator is busy.

        Scheduling from inside the call makes the baseline fail deterministically:
        a call on the event loop prevents its own completion callback from running.
        """
        loop = asyncio.get_running_loop()
        def blocked(*args, **kwargs):
            observed = threading.Event()
            loop.call_soon_threadsafe(observed.set)
            if not observed.wait(2):
                raise AssertionError('operator IO blocked the lease observer loop')
            return result
        return Mock(side_effect=blocked)

    async def test_all_authentication_paths_allow_lease_observation(self):
        for mode in ('local', 'cookie', 'bearer_session', 'bearer_service'):
            with self.subTest(mode=mode):
                session = SimpleNamespace(actor=self.actor)
                service = SimpleNamespace(local_trusted_actor=Mock(return_value=self.actor),
                    authenticate_session=Mock(return_value=session),
                    authenticate_service_token=Mock(return_value=self.actor))
                headers = {}; cookies = {}
                if mode == 'local':
                    service.local_trusted_actor = self.lease_callback_gate(self.actor)
                elif mode == 'cookie':
                    cookies[SESSION_COOKIE] = 'session-token'
                    service.authenticate_session = self.lease_callback_gate(session)
                elif mode == 'bearer_session':
                    headers['Authorization'] = 'Bearer session-token'
                    service.authenticate_session = self.lease_callback_gate(session)
                else:
                    headers['Authorization'] = 'Bearer service-token'
                    service.authenticate_session.side_effect = AuthenticationError('not a session')
                    service.authenticate_service_token = self.lease_callback_gate(self.actor)
                app = FastAPI(); install_identity_middleware(app, service)
                @app.get('/probe')
                async def probe(request: Request):
                    return {'actor': request.state.identity_actor.model_dump(mode='json')}
                with patch.dict(os.environ, {'CODEX_WEB_IDENTITY_MODE': 'local-trusted'}):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                            base_url='http://test', cookies=cookies) as client:
                        response = await client.get('/probe', headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['actor'], self.actor.model_dump(mode='json'))
                if mode == 'cookie':
                    service.authenticate_session.assert_called_once_with('session-token',
                        csrf_token=None, require_csrf=False)
                elif mode == 'bearer_session':
                    service.authenticate_session.assert_called_once_with('session-token', require_csrf=False)
                elif mode == 'bearer_service':
                    service.authenticate_service_token.assert_called_once_with('service-token')

    async def test_api_authority_io_allows_callback_and_retains_denial(self):
        for denied in (False, True):
            with self.subTest(denied=denied):
                app = FastAPI()
                service = SimpleNamespace(local_trusted_actor=Mock(return_value=self.actor))
                install_identity_middleware(app, service)
                gate = self.lease_callback_gate(None)
                def authorize(request, actor):
                    gate(request, actor)
                    self.assertEqual(actor, self.actor)
                    if denied:
                        raise ApiAuthorizationError('current authority denied', status_code=403)
                app.state.api_authorization_service = SimpleNamespace(authorize_request=authorize)
                endpoint = Mock()
                @app.get('/probe')
                async def probe():
                    endpoint(); return {'ok': True}
                with patch.dict(os.environ, {'CODEX_WEB_IDENTITY_MODE': 'local-trusted'}):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                        response = await client.get('/probe')
                self.assertEqual(response.status_code, 403 if denied else 200)
                self.assertEqual(endpoint.call_count, 0 if denied else 1)

    async def test_broker_audit_io_allows_callback_and_preserves_scoped_arguments(self):
        gate = self.lease_callback_gate([])
        service = SimpleNamespace(audit_events=gate)
        app = FastAPI(); app.include_router(build_control_plane_broker_router(service))
        with patch('codex_web.api.control_plane_broker._operator_actor', return_value=self.actor):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.get('/api/control-plane-broker/audit',
                    params={'assignment_id': 'exact-assignment', 'limit': 7})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'items': [], 'count': 0})
        gate.assert_called_once_with(actor=self.actor, assignment_id='exact-assignment', limit=7)
