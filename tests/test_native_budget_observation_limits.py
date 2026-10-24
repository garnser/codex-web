from __future__ import annotations

import unittest
from codex_web.agent_profiles import AgentProfileBudgetDefaults, AgentProfileUpdate
from codex_web.agent_routing import AgentRoutingRequest
from codex_web.model_gateway import ModelInvocationRequest
from codex_web.services.agent_routing import AgentRoutingService
from tests import test_agent_profiles as profiles_fixture


class NullableProfileBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = profiles_fixture.AgentProfileTests()
        await self.fixture.asyncSetUp()
        self.addCleanup(self.fixture.temp.cleanup)

    async def test_profile_constraints_apply_to_null_request_without_type_error(self):
        f = self.fixture
        f._create(budgets=AgentProfileBudgetDefaults(max_input_tokens=12000,
            max_output_tokens=2048, max_cost_usd=1., max_model_calls=1))
        service = AgentRoutingService(None, None, profiles=f.profiles)
        request = AgentRoutingRequest(project_id='project-a', agent_profile_id='coder',
            model_request=ModelInvocationRequest(model_class='primary-coding', messages=(),
                max_input_tokens=None, max_output_tokens=None, max_cost_usd=None))
        effective, profile = service._apply_profile(request, f.member)
        self.assertEqual((effective.model_request.max_input_tokens,
                          effective.model_request.max_output_tokens,
                          effective.model_request.max_cost_usd), (12000,2048,1.))
        # Disabling Profile defaults is a separate authorized canonical revision,
        # never implicit authority granted by the invocation's null marker.
        updated = f.profiles.update('coder', AgentProfileUpdate(
            budgets=AgentProfileBudgetDefaults(max_model_calls=1),
            reason='Explicit operator removal of application token/cost defaults'), actor=f.admin)
        self.assertEqual(updated.revision, profile.revision + 1)
        effective, _ = service._apply_profile(request, f.member)
        self.assertIsNone(effective.model_request.max_input_tokens)
        self.assertIsNone(effective.model_request.max_output_tokens)
        self.assertIsNone(effective.model_request.max_cost_usd)
        self.assertEqual(updated.budgets.max_model_calls, 1)


class NativeTokenObservationLimits(unittest.TestCase):
    def context(self, **values):
        from codex_web.services.native_model_provider import NativeModelInvocationContext
        defaults=dict(organization_id='local',workspace_id='default',actor_id='actor',
            project_id='project',activation_id='activation',role_id='cto',
            execution_id='executive:activation:cto',max_input_tokens=None,max_output_tokens=None,
            model_context_window_tokens=12000,model_output_capacity_tokens=2048,validate_current=lambda:None)
        defaults.update(values)
        return NativeModelInvocationContext(**defaults)

    def test_application_null_is_not_a_reserved_output_input_ceiling(self):
        context=self.context()
        self.assertTrue(context.observed_tokens_allowed(11000,256))
        self.assertFalse(context.observed_tokens_allowed(11900,256))
        self.assertFalse(context.observed_tokens_allowed(1000,2049))

    def test_finite_application_limits_and_unknown_capacity_remain_fail_closed(self):
        context=self.context(max_input_tokens=1000,max_output_tokens=500)
        self.assertTrue(context.observed_tokens_allowed(1000,500))
        self.assertFalse(context.observed_tokens_allowed(1001,1))
        self.assertFalse(context.observed_tokens_allowed(1,501))
        self.assertFalse(self.context(model_context_window_tokens=None).observed_tokens_allowed(17,9))

    def test_partial_usage_rejects_known_joint_capacity_overflow(self):
        context = self.context(model_output_capacity_tokens=None)
        self.assertTrue(context.observed_tokens_allowed(11000, None))
        self.assertTrue(context.observed_tokens_allowed(None, 11000))
        self.assertFalse(context.observed_tokens_allowed(12001, None))
        self.assertFalse(context.observed_tokens_allowed(None, 12001))
