"""Common result checks on original canonical Codex evidence, never relabeled.

The separate canonical-bridge fixture proves the restricted 1.1 runtime path.
"""
import unittest
import inspect
from dataclasses import replace
from types import SimpleNamespace

from codex_web.services.native_model_provider import NativeModelInvocationContext
from codex_web.services.native_reasoning_bridge import CanonicalNativeReasoningBridge, NativeReasoningBinding
from tests import test_home_child_terminal_event as canonical


class OriginalCodexResultBoundary(CanonicalNativeReasoningBridge):
    # Retain the genuine original runtime identity. This tests common result
    # validation and never manufactures a restricted-runtime START receipt.
    runtime_id='codex'


class NativeOriginalResultTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Composition keeps all inherited fixture tests out of this module.
        self.fixture = canonical.HomeChildTerminalTests('test_owned_child_completion_publishes_canonical_event')
        self.addAsyncCleanup(self.cleanup_fixture)
        await self.fixture.asyncSetUp()
        self.active = await self.fixture.start_child()
        self.assignment = self.fixture.f.workers.store.assignment(self.active.assignment_id)
        actor = self.fixture.actor
        original = self.fixture.sessions.store.find_by_native_id(self.active.thread_id,
            organization_id=actor.organization_id, workspace_id=actor.workspace_id,
            provider_id='openai', runtime_id='codex')
        measured = self.fixture.final_usage(self.active)
        self.session = original
        self.usage = measured.model_copy(update={
            'agent_session_id': self.session.id, 'model_call_count':1})
        self.fixture.usage.upsert(self.usage)
        self.context = NativeModelInvocationContext(organization_id=actor.organization_id,
            workspace_id=actor.workspace_id, actor_id=actor.identity_id, project_id=self.active.project_id,
            activation_id='actual-fixture-reservation', role_id='cto',
            execution_id=self.fixture.execution.queued_preflight.store.get(
                self.active.queued_admission_attempt_id).execution_id,
            max_input_tokens=100, max_output_tokens=100, validate_current=lambda:None)
        self.bridge = OriginalCodexResultBoundary(binding=NativeReasoningBinding(
            provider_id='native', model_id='native-model', concrete_model='configured-model',
            agent_profile_id='configured-profile', agent_profile_revision=1,
            execution_profile_id='configured-scratch', worker_id=self.active.worker_id),
            threads=None, turns=None, sessions=self.fixture.sessions.store,
            usage=self.fixture.usage, preflight=self.fixture.execution.queued_preflight,
            execution=self.fixture.execution, hub=None, attest_session=lambda *args:None)
        self.arguments = dict(context=self.context, actor=actor, session=self.session,
            assignment=self.assignment, thread_id=self.active.thread_id,
            turn_id=self.active.turn_id,
            queued_id=self.active.queued_admission_attempt_id.removeprefix('preflight-queued-'),
            text='bounded measured response')

    async def cleanup_fixture(self):
        while self.fixture._cleanups:
            function, args, kwargs = self.fixture._cleanups.pop()
            result = function(*args, **kwargs)
            if inspect.isawaitable(result):
                await result

    async def test_original_start_and_measured_usage_return_unknown_usd(self):
        result = self.bridge._result(**self.arguments)
        self.assertEqual((result.usage.input_tokens,result.usage.output_tokens),(17,9))
        self.assertIsNone(result.usage.cost)
        self.assertEqual(result.provider_request_id, f'{self.session.id}/{self.active.turn_id}')
        self.assertEqual(len(self.fixture.starts()),1)

    async def test_missing_measurement_foreign_session_or_failed_terminal_refuse(self):
        for changes in ({'input_tokens':None}, {'agent_session_id':'foreign'},
                        {'terminal_outcome':'unknown'}, {'model_call_count':None}):
            self.fixture.usage.upsert(self.usage.model_copy(update=changes))
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.bridge._result(**self.arguments)
        self.assertEqual(len(self.fixture.starts()),1)

    async def test_changed_original_fence_or_reservation_refuses_without_new_start(self):
        with self.assertRaises(ValueError):
            self.bridge._result(**{**self.arguments, 'assignment':self.assignment.model_copy(
                update={'fence':self.assignment.fence+1})})
        def changed():
            raise ValueError('Current consulting reservation changed')
        with self.assertRaises(ValueError):
            self.bridge._result(**{**self.arguments, 'context':replace(self.context,validate_current=changed)})
        self.assertEqual(len(self.fixture.starts()),1)
