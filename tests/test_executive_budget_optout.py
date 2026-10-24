from __future__ import annotations

from dataclasses import replace
import unittest

from codex_web.executive_roles import ExecutiveActivationCreate, ExecutiveReasoningBudget
from codex_web.input_plugins import (InputEnvelope, InputPluginPipeline, InputPluginRegistration,
                                     InputPhase, NormalizeWhitespaceInputPlugin)
from codex_web.input_plugin_transports import ExternalInputProjection
from codex_web.model_gateway import ModelDefinitionUpsert, TenantModelPolicyUpdate
from codex_web.services.model_gateway import ModelRoutingError
from codex_web.model_providers import ModelProviderAdapterError
from tests import test_executive_management as executive_fixture
from tests import test_native_model_gateway as native_fixture


class ExecutiveBudgetOptOutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = executive_fixture.ExecutiveManagementTests(
            'test_deterministic_selection_invokes_only_relevant_roles_and_preserves_disagreement')
        await self.fixture.asyncSetUp()
        self.addCleanup(self.fixture.temp.cleanup)

    async def test_explicit_null_survives_creation_storage_and_one_call(self):
        f = self.fixture
        budget = ExecutiveReasoningBudget(max_input_tokens=None, max_output_tokens=None,
                                         max_cost_usd=None, max_model_calls=1)
        activation = f.service.create(ExecutiveActivationCreate(subject='Architecture qualification',
            request='Assess one architecture question', requested_role_ids=('cto',), budget=budget),
            actor=f.actor)
        self.assertEqual(f.store.get(activation.id).budget, budget)
        completed = await f.service.consult(activation.id, actor=f.actor)
        self.assertEqual(completed.status, 'completed')
        self.assertEqual(len(f.gateway.requests), 1)
        request = f.gateway.requests[0]
        self.assertIsNone(request.max_input_tokens)
        self.assertIsNone(request.max_output_tokens)
        self.assertIsNone(request.max_cost_usd)
        self.assertFalse(request.allow_fallback)
        self.assertEqual(f.store.get(activation.id).budget, budget)

    async def test_envelope_migration_does_not_rewrite_historical_budgets(self):
        f = self.fixture
        activation = f.service.create(ExecutiveActivationCreate(subject='Historical architecture',
            request='Assess one architecture question', requested_role_ids=('cto',)), actor=f.actor)
        old = f.store.load().model_dump(mode='json')
        old['schema_version'] = '1.0'
        migrated = f.store._decode(old).model_dump(mode='json')
        self.assertEqual(migrated['schema_version'], '1.1')
        self.assertEqual(migrated['activations'], old['activations'])
        self.assertEqual(migrated['revisions'], old['revisions'])
        self.assertEqual(activation.budget, ExecutiveReasoningBudget())

    async def test_input_projections_preserve_explicit_null(self):
        envelope = InputEnvelope(request_id='r', organization_id='local', workspace_id='default',
            actor_id='actor', model_class='strategic', purpose='test', max_output_tokens=None)
        self.assertIsNone(InputEnvelope.model_validate(envelope.model_dump()).max_output_tokens)
        self.assertIsNone(ExternalInputProjection(purpose='test', model_class='strategic',
                                                 max_output_tokens=None).max_output_tokens)


class NativeBudgetOptOutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await native_fixture.NativeGatewayTests.asyncSetUp(self)

    async def test_real_route_to_native_invoke_resolves_actual_capacity_once(self):
        request = self.request.model_copy(update={'max_input_tokens': None,
            'max_output_tokens': None, 'max_cost_usd': None})
        context = replace(self.context, max_input_tokens=None, max_output_tokens=None)
        self.service.input_pipeline = InputPluginPipeline([
            InputPluginRegistration(plugin=NormalizeWhitespaceInputPlugin(), phase=InputPhase.NORMALIZE)])
        adapter = self.service.adapters['codex-native-reasoning']
        original = adapter.bridge.invoke
        captured = []
        async def invoke(provider, model, effective, *, actor, context):
            self.assertTrue(context.accepts(effective, actor))
            self.assertIsNone(effective.max_output_tokens)
            self.assertIsNone(effective.max_input_tokens)
            self.assertIsNone(context.max_input_tokens)
            self.assertIsNone(context.max_output_tokens)
            self.assertEqual(context.model_context_window_tokens, model.context_window_tokens)
            self.assertEqual(context.model_output_capacity_tokens, model.max_output_tokens)
            captured.append(effective)
            return await original(provider, model, effective, actor=actor, context=context)
        adapter.bridge.invoke = invoke
        result = await self.service.invoke(request, actor=self.actor, native_context=context)
        self.assertEqual(result.text, 'measured result')
        self.assertEqual(len(captured), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(request.max_output_tokens)  # Original intent is immutable.
        self.assertIsNone(context.max_output_tokens)
        self.assertIsNone(result.invocation.attempts[0].actual_cost_usd)
        self.assertEqual(len(result.invocation.input_plugin_provenance), 1)
        # A caller cannot substitute a finite request under the original null
        # marker at the actual adapter boundary after seeing a route.
        provider = next(row for row in self.service.store.load().providers if row.id == 'native')
        model = next(row for row in self.service.store.load().models if row.id == 'native-row')
        with self.assertRaises(ModelProviderAdapterError):
            await adapter.invoke_scoped(provider, model,
                request.model_copy(update={'max_output_tokens': model.max_output_tokens + 1}),
                actor=self.actor, context=context, credential=None)
        self.assertEqual(len(self.calls), 1)

    async def test_null_markers_never_authorize_mismatch_or_foreign_context(self):
        null_request = self.request.model_copy(update={'max_input_tokens': None,
                                                      'max_output_tokens': None})
        null_context = replace(self.context, max_input_tokens=None, max_output_tokens=None)
        cases = [(null_request, self.context), (self.request, null_context),
                 (null_request, replace(null_context, activation_id='other')),
                 (null_request, replace(null_context, workspace_id='foreign')),
                 (null_request, replace(null_context, actor_id='foreign')),
                 (self.request.model_copy(update={'max_input_tokens': 1001}), self.context)]
        for request, context in cases:
            with self.subTest(request=request.max_input_tokens, context=context.activation_id):
                with self.assertRaises(ModelRoutingError):
                    self.service.route(request, actor=self.actor, native_context=context)
        self.assertEqual(self.calls, [])

    async def test_null_output_uses_remaining_context_and_rejects_oversize_input(self):
        self.service.upsert_model(ModelDefinitionUpsert(id='native-row', provider_id='native',
            concrete_model='actual-qualified-row', model_classes=('strategic',),
            capabilities=('text', 'reasoning'), max_output_tokens=2000,
            context_window_tokens=1024), actor=self.actor)
        request = self.request.model_copy(update={'max_input_tokens': None, 'max_output_tokens': None})
        context = replace(self.context, max_input_tokens=None, max_output_tokens=None)
        route = self.service.route(request, actor=self.actor, native_context=context)
        self.assertGreater(route.candidates[0].max_output_tokens, 0)
        self.assertLess(route.candidates[0].max_output_tokens, 1024)
        large = request.model_copy(update={'system_prompt': 'x' * 20000})
        with self.assertRaises(ModelRoutingError):
            self.service.route(large, actor=self.actor, native_context=context)
        self.assertEqual(self.calls, [])

    async def test_disabled_application_cost_keeps_real_policy_cost_ceiling(self):
        from codex_web.model_gateway import ModelProviderUpsert
        class Priced:
            adapter_type = 'priced'
        self.service.register_adapter(Priced())
        self.service.upsert_provider(ModelProviderUpsert(id='priced', display_name='Priced',
            adapter_type='priced', credential_required=False), actor=self.actor)
        self.service.upsert_model(ModelDefinitionUpsert(id='priced-row', provider_id='priced',
            concrete_model='priced', model_classes=('strategic',), capabilities=('text','reasoning'),
            input_price_per_million_usd=1000., output_price_per_million_usd=1000.), actor=self.actor)
        self.service.set_policy(TenantModelPolicyUpdate(max_invocation_cost_usd=.000001), actor=self.actor)
        with self.assertRaises(ModelRoutingError):
            self.service.route(self.request.model_copy(update={'pinned_model_id':'priced-row',
                'max_input_tokens':None, 'max_output_tokens':None, 'max_cost_usd':None}), actor=self.actor)
        self.assertEqual(self.calls, [])

    async def test_disabled_application_cost_does_not_disable_policy(self):
        self.service.set_policy(TenantModelPolicyUpdate(allowed_provider_ids=('other',)), actor=self.actor)
        request = self.request.model_copy(update={'max_input_tokens': None,
            'max_output_tokens': None, 'max_cost_usd': None})
        with self.assertRaises(ModelRoutingError):
            self.service.route(request, actor=self.actor,
                native_context=replace(self.context, max_input_tokens=None, max_output_tokens=None))
        self.assertEqual(self.calls, [])
