from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

from codex_web.model_gateway import (ModelProviderUpsert, ModelDefinitionUpsert,
    ModelInvocationRequest, ModelMessage, ModelAccessSource, ModelUsageSemantics,
    ModelProviderResult, ModelProviderUsage, PromptTemplateUpsert)
from codex_web.model_providers import ModelProviderOutcomeUnknownError
from codex_web.services.model_gateway import ModelGatewayService, ModelRoutingError, ModelProviderUnavailableError
from codex_web.services.native_model_provider import (NativeCodexModelProviderAdapter,
    NativeModelInvocationContext, current_native_model_invocation)
from codex_web.services.codex_reasoning_policy import (DISABLED_FEATURES, REASONING_CATALOG,
    NativeReasoningPolicyError, ReasoningOnlyCodexRuntime, restricted_model_catalog,
    verify_reasoning_config)
from codex_web.services.identity import IdentityService
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.model_gateway import ModelGatewayStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class NativeGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        store = SQLiteStateStore(Path(self.directory.name) / 'state.sqlite')
        identity = IdentityService(IdentityStateStore(store))
        identity.bootstrap_local()
        self.actor = identity.local_trusted_actor()
        self.service = ModelGatewayService(ModelGatewayStore(store))
        self.service.upsert_template(PromptTemplateUpsert(template_id="generic.system",
            version="1.0", content="{{ instructions }}"), actor=self.actor)
        self.calls = []
        test = self
        class Bridge:
            def supports(self, provider, model, context):
                return model.concrete_model == 'actual-qualified-row'
            async def invoke(self, provider, model, request, *, actor, context):
                test.calls.append(context)
                test.assertIs(current_native_model_invocation(), context)
                context.validate_current()
                if test.unknown:
                    raise ModelProviderOutcomeUnknownError('Original START retained')
                return ModelProviderResult(text='measured result',
                    usage=ModelProviderUsage(input_tokens=20, output_tokens=5))
        self.unknown = False
        self.service.register_adapter(NativeCodexModelProviderAdapter(Bridge()))
        self.service.upsert_provider(ModelProviderUpsert(id='native', display_name='Native',
            adapter_type='codex-native-reasoning', runtime_provider='codex',
            credential_required=False, access_source=ModelAccessSource.CHATGPT_SUBSCRIPTION,
            usage_semantics=ModelUsageSemantics.ENTITLEMENT), actor=self.actor)
        self.service.upsert_model(ModelDefinitionUpsert(id='native-row', provider_id='native',
            concrete_model='actual-qualified-row', model_classes=('strategic',),
            capabilities=('text', 'reasoning'), max_output_tokens=2000), actor=self.actor)
        self.request = ModelInvocationRequest(model_class='strategic',
            messages=(ModelMessage(role='user', content='Bounded advisory question'),),
            max_input_tokens=1000, max_output_tokens=1000, max_cost_usd=1.,
            allow_fallback=False, purpose='executive-role:cto', execution_id='executive:a:cto')
        self.context = NativeModelInvocationContext(organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id, actor_id=self.actor.identity_id,
            project_id='project', activation_id='a', role_id='cto', execution_id='executive:a:cto',
            max_input_tokens=1000, max_output_tokens=1000, validate_current=lambda: None)

    async def test_registered_entitlement_requires_internal_context_and_keeps_usd_unknown(self):
        with self.assertRaises(ModelRoutingError):
            self.service.route(self.request, actor=self.actor)
        result = await self.service.invoke(self.request, actor=self.actor, native_context=self.context)
        self.assertEqual(result.text, 'measured result')
        self.assertEqual(result.invocation.attempts[0].input_tokens, 20)
        self.assertEqual(result.invocation.attempts[0].output_tokens, 5)
        self.assertIsNone(result.invocation.attempts[0].actual_cost_usd)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(NativeReasoningPolicyError):
            current_native_model_invocation()

    async def test_foreign_context_and_larger_budget_cannot_admit_unpriced_route(self):
        for context in (replace(self.context, actor_id='foreign'),
                        replace(self.context, execution_id='other'),
                        replace(self.context, max_output_tokens=999)):
            with self.subTest(context=context), self.assertRaises(ModelRoutingError):
                self.service.route(self.request, actor=self.actor, native_context=context)
        self.assertEqual(self.calls, [])

    async def test_unknown_original_never_falls_back_or_reports_zero_cost_success(self):
        self.unknown = True
        with self.assertRaises(ModelProviderUnavailableError):
            await self.service.invoke(self.request, actor=self.actor, native_context=self.context)
        records = self.service.store.load().invocations
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, 'uncertain')
        self.assertEqual([a.outcome for a in records[0].attempts], ['unknown'])
        self.assertEqual(len(self.calls), 1)

    async def test_current_reservation_revocation_refuses_before_bridge_result(self):
        def revoked():
            raise NativeReasoningPolicyError('Reservation changed')
        with self.assertRaises(ModelProviderUnavailableError):
            await self.service.invoke(self.request, actor=self.actor,
                native_context=replace(self.context, validate_current=revoked))
        self.assertNotEqual(self.service.store.load().invocations[-1].status, 'succeeded')

    async def test_mixed_priced_and_entitled_summary_keeps_usd_unknown(self):
        class Priced:
            adapter_type = 'priced-test'
            async def invoke(self, *args, **kwargs):
                return ModelProviderResult(text='priced', usage=ModelProviderUsage(
                    input_tokens=10, output_tokens=2, cost=.25, currency='USD'))
        self.service.register_adapter(Priced())
        self.service.upsert_provider(ModelProviderUpsert(id='priced', display_name='Priced',
            adapter_type='priced-test', credential_required=False), actor=self.actor)
        self.service.upsert_model(ModelDefinitionUpsert(id='priced-row', provider_id='priced',
            concrete_model='priced', model_classes=('strategic',), capabilities=('text','reasoning'),
            input_price_per_million_usd=1., output_price_per_million_usd=1.), actor=self.actor)
        request = self.request.model_copy(update={'pinned_model_id':'priced-row',
            'goal_id':'goal', 'decision_id':'decision'})
        await self.service.invoke(request, actor=self.actor)
        self.assertEqual(self.service.goal_usage('goal',actor=self.actor).cost_usd,.25)
        request = request.model_copy(update={'pinned_model_id':'native-row'})
        await self.service.invoke(request, actor=self.actor, native_context=self.context)
        for summary in (self.service.goal_usage('goal',actor=self.actor),
                        self.service.decision_usage('decision',actor=self.actor)):
            self.assertEqual((summary.calls,summary.input_tokens,summary.output_tokens),(2,30,7))
            self.assertIsNone(summary.cost_usd)
            self.assertIsNone(summary.model_dump(mode='json')['cost_usd'])



class NativePolicyTests(unittest.IsolatedAsyncioTestCase):
    def config(self):
        return {'features': dict.fromkeys(DISABLED_FEATURES, False), 'mcp_servers': {},
            'web_search': 'disabled', 'model_catalog_json': str(REASONING_CATALOG),
            'forced_login_method': 'chatgpt', 'project_doc_max_bytes': 0, 'model_provider':'openai'}

    def test_effective_mcp_inherited_entry_cannot_be_hidden_by_empty_override(self):
        config = self.config()
        verify_reasoning_config(config)
        config['mcp_servers'] = {'inherited': {'command': '/usr/bin/false'}}
        with self.assertRaises(NativeReasoningPolicyError):
            verify_reasoning_config(config)
        config['mcp_servers']['inherited']['enabled'] = False
        verify_reasoning_config(config)

    def test_actual_catalog_row_reduced_without_mutating_original_or_inventing_model(self):
        original = {'models': [{'slug': 'actual', 'shell_type': 'unified_exec',
            'apply_patch_tool_type': 'freeform', 'experimental_supported_tools': ['clock']}]}
        restricted = restricted_model_catalog(original, 'actual')
        self.assertEqual(restricted['models'][0]['shell_type'], 'disabled')
        self.assertEqual(restricted['models'][0]['experimental_supported_tools'], [])
        self.assertEqual(original['models'][0]['experimental_supported_tools'], ['clock'])
        with self.assertRaises(NativeReasoningPolicyError):
            restricted_model_catalog(original, 'invented')

    async def test_server_tool_and_auth_refresh_requests_are_refused_without_delegate(self):
        runtime = object.__new__(ReasoningOnlyCodexRuntime)
        runtime._send = AsyncMock()
        for method, expected in [('item/commandExecution/requestApproval', 'native_reasoning_server_request_refused'),
                                 ('account/chatgptAuthTokens/refresh', 'native_auth_refresh_owner_required')]:
            await runtime._handle_message({'id': 9, 'method': method, 'params': {}})
            self.assertEqual(runtime.reasoning_refusal, expected)
            self.assertIn('error', runtime._send.call_args.args[0])
