"""Disabled elapsed ceilings preserve real gateway and procedure boundaries."""
import asyncio
from dataclasses import replace
import unittest

from pydantic import ValidationError

from codex_web.model_gateway import ModelInvocationRequest, ModelMessage
from codex_web.services.executive_configuration import EXECUTIVE_INVOCATION_TIMEOUT
from codex_web.services.native_model_provider import NativeProcessOwnership
from codex_web.services.codex_reasoning_policy import NativeReasoningPolicyError
from codex_web.skill_procedures import OperationalProcedure
from tests import test_model_gateway as gateway
from tests import test_native_model_gateway as native
from tests import test_skill_procedures as procedures


class ExecutiveProcedureTimeoutValuesTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_gateway_accepts_disabled_values_and_joins_same_provider(self):
        fixture = gateway.ModelGatewayTests()
        await fixture.asyncSetUp()
        self.addAsyncCleanup(fixture.asyncTearDown)
        fixture._provider()
        fixture._model('m1')
        original = fixture.adapter.invoke
        for value in (0, -1):
            with self.subTest(value=value):
                entered, release = asyncio.Event(), asyncio.Event()
                async def held(*args, **kwargs):
                    entered.set()
                    await release.wait()
                    return await original(*args, **kwargs)
                fixture.adapter.invoke = held
                task = asyncio.create_task(fixture.service.invoke(
                    fixture._request(timeout_seconds=value), actor=fixture.actor))
                try:
                    await asyncio.wait_for(entered.wait(), 60)
                    progress = asyncio.Event()
                    asyncio.get_running_loop().call_soon(progress.set)
                    await asyncio.wait_for(progress.wait(), 60)
                    self.assertFalse(task.done())
                finally:
                    release.set()
                    result = await asyncio.wait_for(asyncio.shield(task), 60)
                self.assertEqual(result.text, 'reply:m1')
                self.assertEqual(result.invocation.status, 'succeeded')

    async def test_real_native_gateway_disabled_ceiling_keeps_current_authority(self):
        fixture = native.NativeGatewayTests()
        fixture.addCleanup = self.addCleanup
        await fixture.asyncSetUp()
        for value in (0, -1):
            request = fixture.request.model_copy(update={'timeout_seconds': value})
            context = replace(fixture.context, max_duration_seconds=value)
            result = await fixture.service.invoke(request, actor=fixture.actor, native_context=context)
            self.assertEqual(result.text, 'measured result')
            self.assertIsNone(fixture.calls[-1].process_owner.deadline_at)
        owner = NativeProcessOwnership(deadline_at=None)
        owner.validate_current()
        owner.cancelled.set()
        with self.assertRaises(NativeReasoningPolicyError):
            owner.validate_current()
        with self.assertRaises(NativeReasoningPolicyError):
            NativeProcessOwnership(deadline_at=0).validate_current()
        # A finite authorized reservation cannot be widened by an off request.
        self.assertFalse(fixture.context.accepts(
            fixture.request.model_copy(update={'timeout_seconds': 0}), fixture.actor))

    async def test_public_timeout_values_preserve_sentinels_reject_other_negative(self):
        for value in (0, -1, 1, 120):
            request = ModelInvocationRequest(model_class='strategic',
                messages=(ModelMessage(role='user', content='Question'),), timeout_seconds=value)
            self.assertEqual(request.model_dump()['timeout_seconds'], value)
            self.assertEqual(EXECUTIVE_INVOCATION_TIMEOUT.validate_value(value), value)
        with self.assertRaises(ValidationError):
            ModelInvocationRequest(model_class='strategic',
                messages=(ModelMessage(role='user', content='Question'),), timeout_seconds=-0.5)

    async def test_procedure_new_version_disables_caps_without_rewriting_old_definition(self):
        original = procedures.procedure()
        old = OperationalProcedure.model_validate(original)
        for value in (0, -1):
            candidate = procedures.procedure()
            candidate['schema_version'] = '1.5'
            candidate['max_duration_seconds'] = value
            for step in candidate['steps']:
                step['timeout_seconds'] = value
            new = OperationalProcedure.model_validate(candidate)
            self.assertEqual(new.max_duration_seconds, value)
            self.assertTrue(all(step.timeout_seconds == value for step in new.steps))
            candidate['schema_version'] = '1.0'
            with self.assertRaises(ValidationError):
                OperationalProcedure.model_validate(candidate)
        self.assertEqual(old.model_dump(mode='json'), OperationalProcedure.model_validate(original).model_dump(mode='json'))
        finite = procedures.procedure()
        finite['schema_version'] = '1.5'
        finite['steps'][0]['timeout_seconds'] = 0
        bounded_total = OperationalProcedure.model_validate(finite)
        self.assertEqual(bounded_total.steps[0].timeout_seconds, 0)
        self.assertEqual(bounded_total.max_duration_seconds, original['max_duration_seconds'])
