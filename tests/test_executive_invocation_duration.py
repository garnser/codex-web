"""Private canonical configuration/consultation fixtures; no live provider or app."""
from dataclasses import replace
import unittest
import threading
from unittest.mock import patch

from codex_web.configuration import ConfigurationDraftCreate, ConfigurationPublishRequest
from codex_web.executive_roles import ExecutiveActivationCreate, ExecutiveReasoningBudget
from codex_web.model_gateway import ModelInvocationRequest
from codex_web.model_providers import ModelProviderOutcomeUnknownError
from codex_web.services.configuration import ConfigurationService
from codex_web.services.executive_configuration import (
    EXECUTIVE_INVOCATION_TIMEOUT, install_executive_configuration,
)
from codex_web.services.model_gateway import ModelRoutingError, ModelProviderUnavailableError
from codex_web.storage.configuration_registry import ConfigurationRegistryStore
from tests import test_executive_management as executive_fixture
from tests import test_native_model_gateway as native_fixture


class ExecutiveInvocationDurationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = executive_fixture.ExecutiveManagementTests(
            'test_deterministic_selection_invokes_only_relevant_roles_and_preserves_disagreement')
        await self.f.asyncSetUp()
        self.addAsyncCleanup(self.f.asyncTearDown)
        self.config = ConfigurationService(ConfigurationRegistryStore(self.f.store.store))
        self.spec = install_executive_configuration(self.config)
        self.resolutions = []
        self.resolver_threads = []
        self.event_loop_thread = threading.get_ident()

        def resolve():
            self.resolver_threads.append(threading.get_ident())
            value = self.config.resolve(self.spec.key)
            self.resolutions.append(value)
            return value

        self.f.service.invocation_duration_resolver = resolve

    def publish(self, value):
        draft = self.config.create_draft(ConfigurationDraftCreate(
            key=self.spec.key, scope_type='deployment', value=value,
            actor=self.f.actor.identity_id, reason='Private finite invocation duration',
        ))
        return self.config.publish(draft.id, ConfigurationPublishRequest(
            actor=self.f.actor.identity_id, reason='Private finite invocation duration',
        ))

    def activation(self, roles=('cto',), *, native=False):
        item = self.f.service.create(ExecutiveActivationCreate(
            subject='Architecture and cost migration', request='Assess bounded migration readiness',
            requested_role_ids=roles, max_roles=len(roles),
            budget=ExecutiveReasoningBudget(max_model_calls=3 if len(roles)>1 else 1),
        ), actor=self.f.actor)
        if native:
            # Same canonical metadata binding used by test_native_executive_context;
            # no process, workspace, capability or live authority is fabricated.
            def bind(state, current):
                updated = current.model_copy(update={'project_id': 'project'})
                state.activations = [updated if row.id == updated.id else row for row in state.activations]
                return state, updated
            item = self.f.store.update(item.id, bind)
            self.f.service.native_invocation_authorizer = lambda *a, **k: lambda: None
        return item

    async def test_default_keeps_120_and_one_call_and_global_gateway_default(self):
        item = self.activation()
        result = await self.f.service.consult(item.id, actor=self.f.actor)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(len(self.resolutions), 1)
        self.assertNotEqual(self.resolver_threads, [self.event_loop_thread])
        self.assertEqual([r.timeout_seconds for r in self.f.gateway.requests], [120])
        self.assertFalse(self.f.gateway.requests[0].allow_fallback)
        self.assertEqual(ModelInvocationRequest.model_fields['timeout_seconds'].default, 120)

    async def test_300_snapshot_reaches_roles_synthesis_and_native_context_then_next_call_refreshes(self):
        published = self.publish(300)
        item = self.activation(('cto', 'cfo'), native=True)
        original = self.f.gateway.invoke
        contexts = []

        async def invoke(request, *, actor, native_context):
            contexts.append(native_context)
            self.assertEqual(request.timeout_seconds, 300)
            self.assertEqual(native_context.max_duration_seconds, 300)
            self.assertTrue(native_context.accepts(request, actor))
            native_context.validate_current()
            if len(contexts) == 1:
                self.publish(600)  # An active consultation keeps its original snapshot.
            return await original(request, actor=actor)

        self.f.gateway.invoke = invoke
        with patch('codex_web.services.executive_management.log_event', create=True) as log:
            result = await self.f.service.consult(item.id, actor=self.f.actor)
        self.assertEqual(result.status, 'escalated')
        self.assertEqual(len(self.resolutions), 1)
        self.assertEqual(len(contexts), 3)
        self.assertEqual([r.timeout_seconds for r in self.f.gateway.requests], [300]*3)
        self.assertTrue(all(not r.allow_fallback for r in self.f.gateway.requests))
        self.assertEqual({c.role_id for c in contexts}, {'cto', 'cfo', 'synthesis'})
        fields = log.call_args.kwargs
        self.assertEqual((fields['timeout_seconds'], fields['configuration_record_id'],
                          fields['configuration_revision']), (300, published.id, published.revision))
        self.f.gateway.invoke = original
        next_item = self.activation()
        await self.f.service.consult(next_item.id, actor=self.f.actor)
        self.assertEqual(len(self.resolutions), 2)
        self.assertEqual(self.f.gateway.requests[-1].timeout_seconds, 600)
        self.assertEqual(self.f.store.get(item.id).budget, item.budget)

    def test_canonical_spec_bounds_scope_and_admin_metadata(self):
        self.assertTrue(self.spec.hot_reloadable)
        self.assertFalse(self.spec.startup_only)
        self.assertFalse(self.spec.grants_authority)
        for invalid in (0, -1, 601, True, 300.5, '300', None):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                self.publish(invalid)
        with self.assertRaises(ValueError):
            self.config.create_draft(ConfigurationDraftCreate(
                key=self.spec.key, scope_type='project', scope_id='project', value=300,
                actor=self.f.actor.identity_id,
            ))
        self.assertEqual(self.spec.validate_value(1), 1)
        self.assertEqual(self.spec.validate_value(600), 600)

    async def test_invalid_resolved_value_refuses_before_reservation_and_any_call(self):
        item = self.activation()
        resolved = self.config.resolve(self.spec.key)
        self.f.service.invocation_duration_resolver = lambda: resolved.model_copy(update={'value': 601})
        with self.assertRaises(ValueError):
            await self.f.service.consult(item.id, actor=self.f.actor)
        self.assertEqual(self.f.store.get(item.id).status, 'planned')
        self.assertEqual(self.f.gateway.requests, [])

    async def test_unconfigured_service_and_logging_failure_preserve_default(self):
        self.f.service.invocation_duration_resolver = None
        item = self.activation()
        with patch('codex_web.services.executive_management.log_event', side_effect=RuntimeError('sink')):
            result = await self.f.service.consult(item.id, actor=self.f.actor)
        self.assertEqual(result.status, 'completed')
        self.assertEqual([r.timeout_seconds for r in self.f.gateway.requests], [120])


class NativeDurationOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = native_fixture.NativeGatewayTests(
            'test_registered_entitlement_requires_internal_context_and_keeps_usd_unknown')
        await self.f.asyncSetUp()
        self.addCleanup(self.f.doCleanups)

    async def test_300_requires_matching_context_and_keeps_unknown_single_attempt(self):
        f = self.f
        request = f.request.model_copy(update={'timeout_seconds': 300})
        with self.assertRaises(ModelRoutingError):
            f.service.route(request, actor=f.actor, native_context=f.context)
        self.assertEqual(f.calls, [])
        context = replace(f.context, max_duration_seconds=300)
        f.unknown = True
        with self.assertRaises(ModelProviderUnavailableError) as retained:
            await f.service.invoke(request, actor=f.actor, native_context=context)
        self.assertIsInstance(retained.exception.__cause__, ModelProviderOutcomeUnknownError)
        self.assertEqual(len(f.calls), 1)
        record = f.service.store.load().invocations[-1]
        self.assertEqual(record.status, 'uncertain')
        self.assertEqual([a.outcome for a in record.attempts], ['unknown'])
        self.assertEqual(f.calls[0].max_duration_seconds, 300)
        self.assertIsNotNone(f.calls[0].process_owner.deadline_at)
