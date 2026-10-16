"""Actual HTTP identity/router/registry/ActionIntent/Recovery composition; synthetic only."""
import asyncio
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from codex_web.api.identity import CSRF_HEADER, SESSION_COOKIE, install_identity_middleware
from codex_web.api.recovery import build_recovery_router
from codex_web.identity import AuthenticationAssurance
from codex_web.services.identity import AuthenticationError
from codex_web.services.recovery_operator import RecoveryOperatorActions
from tests import test_recovery_action_intent as fixtures


class RecoveryOperatorApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RecoveryActionIntentTests.asyncSetUp
    asyncTearDown = fixtures.RecoveryActionIntentTests.asyncTearDown

    def client(self):
        self.operator = RecoveryOperatorActions(self.recovery, self.service, self.worker_actor)
        credentials = self.identity.create_session(identity_id=self.actor.identity_id,
            scope=self.actor.tenant, assurance=AuthenticationAssurance.MFA)
        self.credentials = credentials
        app = FastAPI()
        install_identity_middleware(app, self.identity)
        app.include_router(build_recovery_router(self.recovery, actions=self.operator, identity=self.identity))
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture',
            cookies={SESSION_COOKIE: credentials.session_token}, headers={CSRF_HEADER: credentials.csrf_token})

    def url(self, suffix='/reconcile'):
        return '/api/recovery/scheduled-firings/' + self.payload.event_id + suffix

    def body(self):
        return {'binding_id': self.recovery_binding.id, 'request': self.payload.model_dump(mode='json')}

    async def test_application_composition_registers_without_binding_grant_or_activation(self):
        bindings = self.registry.store.load()
        state = self.recovery.store.load()
        self.operator = RecoveryOperatorActions(self.recovery, self.service, self.worker_actor)
        self.assertIs(self.registry.provider('recovery', 'canonical-recovery'), self.operator.provider)
        self.assertEqual(self.registry.store.load(), bindings)
        self.assertEqual(self.recovery.store.load(), state)
        self.assertEqual(state.schema_version, '1.2')
        with self.assertRaises(AuthenticationError):
            self.operator.resolve_actor(self.actor.identity_id)

    async def test_real_operator_route_commits_once_preserving_unknown_and_current_preview(self):
        async with self.client() as client:
            preview = await client.get(self.url('/reconciliation-preview'))
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertEqual(preview.json()['expected_state_sha256'], self.payload.expected_state_sha256)
            with patch.object(self.operator.provider.reconcile, 'reconcile', wraps=self.operator.provider.reconcile.reconcile) as engine, \
                 patch.object(self.recovery, 'create_backup', side_effect=AssertionError('original effect replay')):
                response = await client.post(self.url(), json=self.body())
                self.assertEqual(response.status_code, 200, response.text)
                intent = response.json()['item']
                self.assertEqual(intent['status'], 'succeeded', intent.get('last_error'))
                again = await client.post(self.url(), json=self.body())
                self.assertEqual(again.status_code, 200, again.text)
                self.assertEqual(again.json()['item']['id'], intent['id'])
                self.assertEqual(engine.call_count, 1)
            record = self.recovery.store.load().reconciliations[self.payload.event_id]
            self.assertEqual(record.outcome, 'unknown')
            self.assertEqual(record.original.effect_id, next(iter(self.recovery.store.load().scheduled_firings.values())).effect_id)
            self.assertFalse(self.recovery.health(actor=self.actor).recovery_qualified)

    async def test_disabled_binding_refuses_without_state_activation(self):
        async with self.client() as client:
            self.registry.set_enabled(self.recovery_binding.id, False, actor=self.actor)
            before = self.recovery.store.load()
            response = await client.post(self.url(), json=self.body())
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(self.recovery.store.load(), before)

    async def test_stale_or_unqualified_activation_cannot_commit(self):
        async with self.client() as client:
            before = self.recovery.store.load()
            body = self.body()
            body['request']['expected_state_sha256'] = '0' * 64
            response = await client.post(self.url(), json=body)
            self.assertLess(response.status_code, 500, response.text)
            if response.status_code == 200:
                self.assertNotEqual(response.json()['item']['status'], 'succeeded')
            self.assertEqual(self.recovery.store.load(), before)

    async def test_first_write_requires_explicit_qualified_activation(self):
        async with self.client() as client:
            before = self.recovery.store.load()
            body = self.body()
            body['request']['activate_state_1_3'] = False
            response = await client.post(self.url(), json=body)
            self.assertLess(response.status_code, 500, response.text)
            if response.status_code == 200:
                self.assertNotEqual(response.json()['item']['status'], 'succeeded')
            self.assertEqual(self.recovery.store.load(), before)

    async def test_primary_session_does_not_borrow_mfa_from_fixture_actor(self):
        async with self.client() as client:
            def demote(state):
                state.sessions = [s.model_copy(update={'assurance': AuthenticationAssurance.PRIMARY,
                    'step_up_until': None}) if s.id == self.credentials.session_id else s for s in state.sessions]
                return state
            self.identity.store.update(demote)
            before = self.recovery.store.load()
            response = await client.post(self.url(), json=self.body())
            self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(self.recovery.store.load(), before)

    async def test_rotation_after_admission_refuses_domain_mutation(self):
        async with self.client() as client:
            before = self.recovery.store.load()
            original = self.service.claim
            def rotate(*args, **kwargs):
                self.identity.refresh_session(self.credentials.session_id, self.credentials.refresh_token)
                return original(*args, **kwargs)
            with patch.object(self.service, 'claim', side_effect=rotate):
                response = await client.post(self.url(), json=self.body())
            self.assertLess(response.status_code, 500, response.text)
            if response.status_code == 200:
                self.assertNotEqual(response.json()['item']['status'], 'succeeded')
            self.assertEqual(self.recovery.store.load(), before)

    async def test_lost_result_observe_settles_same_receipt_without_engine_or_effect_replay(self):
        async with self.client() as client:
            with patch.object(self.operator.provider, '_result', side_effect=RuntimeError('fixture lost result')):
                response = await client.post(self.url(), json=self.body())
            self.assertEqual(response.status_code, 200, response.text)
            intent = response.json()['item']
            self.assertIn(intent['status'], {'uncertain', 'requires_reconciliation'})
            before = self.recovery.store.load().reconciliations[self.payload.event_id]
            evidence = self.artifacts.store.load()
            with patch.object(self.operator.provider.reconcile, 'reconcile', side_effect=AssertionError('engine replay')), \
                 patch.object(self.recovery, 'create_backup', side_effect=AssertionError('backup replay')):
                response = await client.post(self.url('/reconciliation-intents/' + intent['id'] + '/observe'), json={})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['item']['status'], 'succeeded', response.text)
            self.assertEqual(self.recovery.store.load().reconciliations[self.payload.event_id], before)
            self.assertEqual(self.artifacts.store.load().evidence, evidence.evidence)

    async def test_cancel_before_domain_commit_preserves_original_state(self):
        async with self.client() as client:
            before = self.recovery.store.load()
            with patch.object(self.operator.provider.reconcile, 'reconcile', side_effect=asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError):
                    await client.post(self.url(), json=self.body())
            self.assertEqual(self.recovery.store.load(), before)

    async def test_cookie_csrf_and_extra_authority_fields_are_not_accepted(self):
        async with self.client() as client:
            before = self.recovery.store.load()
            client.headers.pop(CSRF_HEADER)
            response = await client.post(self.url(), json=self.body())
            self.assertIn(response.status_code, {401, 403}, response.text)
            client.headers[CSRF_HEADER] = self.credentials.csrf_token
            body = self.body()
            body['actor'] = 'forged-admin'
            response = await client.post(self.url(), json=body)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(self.recovery.store.load(), before)

    async def test_read_only_session_revalidation_expiry_does_not_write_inside_transaction(self):
        credentials = self.identity.create_session(identity_id=self.actor.identity_id,
            scope=self.actor.tenant, assurance=AuthenticationAssurance.MFA)
        def expire(state):
            state.sessions = [s.model_copy(update={'idle_expires_at': 0})
                if s.id == credentials.session_id else s for s in state.sessions]
            return state
        self.identity.store.update(expire)
        before = self.identity.store.load()
        with self.assertRaises(AuthenticationError):
            self.identity.authenticate_session(credentials.session_token, touch=False, retire_expired=False)
        self.assertEqual(self.identity.store.load(), before)
