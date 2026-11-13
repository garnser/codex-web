"""#2102: closed native bootstrap diagnostics across the Executive HTTP boundary."""
import asyncio
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from codex_web.events import EventHub
from codex_web.model_providers import ModelProviderAdapterError, ModelProviderOutcomeUnknownError
from codex_web.services.model_gateway import ModelGatewayService, ModelProviderUnavailableError
from codex_web.services.native_reasoning_bridge import CanonicalNativeReasoningBridge, NativeReasoningBinding
from codex_web.services.threads import ThreadBootstrapPrestartRefusal, bootstrap_preparation_diagnostic
from tests.test_executive_context_broker_refusal import ExecutiveContextBrokerRefusalTests
from tests.test_fresh_bootstrap_authentication import FreshBootstrapAuthenticationTests


class ExecutiveNativePreparationRefusalTests(unittest.IsolatedAsyncioTestCase):
    # Reuse setup, not a TestCase superclass with inherited test methods.
    asyncSetUp = ExecutiveContextBrokerRefusalTests.asyncSetUp
    request = ExecutiveContextBrokerRefusalTests.request
    assert_audit = ExecutiveContextBrokerRefusalTests.assert_audit

    async def authentic_auth_refusal(self):
        fixture = FreshBootstrapAuthenticationTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        # A deliberate unavailable account, not a TTL/timing reproduction. The
        # real Thread/binding authentication guard must own this typed refusal.
        fixture.threads.authentication_preparation = None
        with self.assertRaises(ThreadBootstrapPrestartRefusal) as caught:
            await fixture.create()
        diagnostic = bootstrap_preparation_diagnostic(caught.exception)
        self.assertIsNotNone(diagnostic)
        self.assertEqual(diagnostic['bootstrap_blocker_code'], 'local_session_unavailable')
        self.assertEqual(diagnostic['bootstrap_http_status'], 503)
        self.assertEqual(fixture.manager.started, [])
        self.assertEqual(fixture.session.requests, [])
        return caught.exception

    async def bridge_error(self, refusal):
        create = AsyncMock(side_effect=refusal)
        events = []
        bridge = CanonicalNativeReasoningBridge(
            binding=NativeReasoningBinding(provider_id='openai', model_id='model',
                concrete_model='model', agent_profile_id='profile', agent_profile_revision=1,
                execution_profile_id='reasoning-only', worker_id='worker'),
            threads=NS(create=create, event_sink=events.append), turns=None, sessions=None,
            usage=None, preflight=None, execution=None, hub=EventHub(), attest_session=lambda: None)
        context = NS(validate_current=lambda: None, organization_id='local', workspace_id='default',
            project_id='project-a', activation_id='activation-original', role_id='cto',
            execution_id='executive:activation-original:cto', process_owner=None)
        with self.assertRaises(ModelProviderAdapterError) as caught:
            await bridge._invoke_owned(NS(id='openai'),
                NS(id='model', concrete_model='model', supports_tools=False), NS(),
                actor=self.actor, context=context, cancelled=asyncio.Event())
        create.assert_awaited_once()
        self.assertEqual(events[0]['type'], 'native_reasoning_preparation_failed')
        return caught.exception

    async def consult_error(self, error):
        status, _, created = await self.socket._request('POST', '/api/executive/activations',
            payload=self.request())
        self.assertEqual(status, 200, created)
        activation_id = created['item']['id']
        failures = []

        async def invoke(request, *, actor):
            # Control the external model boundary. Exercise the actual gateway
            # classifier and wrapping contract; no model/provider call occurs.
            failure = ModelGatewayService._provider_failure(error,
                provider=NS(id='openai', adapter_type='native'), model=NS(id='model', concrete_model='model'),
                request=request, attempt=1)
            failures.append(failure)
            raise ModelProviderUnavailableError(str(error), failure=failure) from error

        with patch.object(self.executive.gateway, 'invoke', side_effect=invoke) as gateway:
            status, _, response = await self.socket._request('POST',
                f'/api/executive/activations/{activation_id}/consult')
        gateway.assert_awaited_once()
        self.assert_audit(response, 'executive.activation.consult', status)
        activation = self.executive.store.get_scoped(activation_id,
            organization_id=self.actor.organization_id, workspace_id=self.actor.workspace_id)
        self.assertEqual(activation.status.value, 'failed')
        self.assertEqual(activation.consultations, ())
        return status, response, activation, failures[0]

    async def test_authentication_refusal_reaches_http_audit_and_canonical_failure(self):
        refusal = await self.authentic_auth_refusal()
        error = await self.bridge_error(refusal)
        status, response, activation, failure = await self.consult_error(error)
        self.assertEqual(status, 503, response)
        self.assertEqual(response['error']['code'], 'executive_native_preparation_unavailable')
        for value in ('bootstrap_reservation', 'local_session_unavailable'):
            self.assertIn(value, response['error']['message'])
            self.assertIn(value, activation.failure_reason)
        self.assertEqual(failure.source_native_code, 'NativeBootstrapPreparationRefused')
        self.assertEqual(failure.source_native_status, 'local_session_unavailable')
        self.assertNotIn('retryable', response['error'])
        self.assertNotIn('provider_start_called', json.dumps(response))

    async def assert_generic_bootstrap_failure(self, refusal):
        error = await self.bridge_error(refusal)
        self.assertIs(type(error), ModelProviderAdapterError)
        status, response, activation, _ = await self.consult_error(error)
        self.assertEqual(status, 500, response)
        self.assertEqual(response['error']['code'], 'control_plane_broker_denied')
        self.assertNotIn('PRIVATE', json.dumps(response))
        self.assertNotIn('PRIVATE', activation.failure_reason)

    async def test_unclassified_secret_bearing_exception_remains_generic(self):
        await self.assert_generic_bootstrap_failure(RuntimeError('PRIVATE account/path/token'))

    async def test_untrusted_bootstrap_diagnostic_remains_generic(self):
        await self.assert_generic_bootstrap_failure(ThreadBootstrapPrestartRefusal(
            status_code=503, detail={'code':'bootstrap_prestart_failed',
                'bootstrap_preparation_stage':'PRIVATE-stage', 'bootstrap_exception_type':'Exception',
                'provider_start_called':False, 'bootstrap_blocker_code':'PRIVATE-token'}))

    async def test_unclassified_controller_failure_does_not_become_service_unavailable(self):
        await self.assert_generic_bootstrap_failure(ThreadBootstrapPrestartRefusal(
            status_code=500, detail={'code':'bootstrap_prestart_failed',
                'bootstrap_preparation_stage':'bootstrap_reservation', 'bootstrap_exception_type':'RuntimeError',
                'provider_start_called':False, 'bootstrap_http_status':500}))

    async def test_unknown_model_outcome_is_not_adapted_as_preparation_refusal(self):
        status, response, _, _ = await self.consult_error(
            ModelProviderOutcomeUnknownError('Original inference remains uncertain'))
        self.assertEqual(status, 500, response)
        self.assertEqual(response['error']['code'], 'control_plane_broker_denied')


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(ExecutiveNativePreparationRefusalTests)
