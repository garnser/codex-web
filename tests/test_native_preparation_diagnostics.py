"""Closed pre-input diagnostics preserve originals, never confer retry authority."""
import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from codex_web.events import EventHub
from codex_web.model_providers import ModelProviderAdapterError, ModelProviderOutcomeUnknownError
from codex_web.services.native_model_provider import NativeModelInvocationContext
from codex_web.services.native_reasoning_bridge import CanonicalNativeReasoningBridge, NativeReasoningBinding
from tests import test_thread_bootstrap_routing as bootstrap


class NativePreparationDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def bridge(self, *, threads=None, guard=lambda: None, sessions=None):
        self.events = []
        self.stopped = []
        self.turns = NS(start=AsyncMock(side_effect=RuntimeError('private turn error')))
        self.owner = NS(bind_native_session=lambda _: None,
                        stop=AsyncMock(side_effect=lambda: self.stopped.append('original')))
        self.context = NativeModelInvocationContext(organization_id='org', workspace_id='workspace',
            actor_id='actor', project_id='project', activation_id='activation', role_id='cpo',
            execution_id='executive:activation:cpo', max_input_tokens=100, max_output_tokens=100,
            validate_current=guard, process_owner=self.owner)
        if threads is None:
            threads = NS(create=AsyncMock(return_value={'thread': {'id': 'thread'}, 'agentSessionId': 'session'}))
        threads.event_sink = self.events.append
        session = NS(provider_id='openai', runtime_id='codex-reasoning', provider_native_session_id='thread',
                     project_id='project', resource_ids=(), worker_id='worker', model='model',
                     assignment_id='assignment', id='session')
        manager = NS()
        serving = NS(runtime=NS(), validate_current=lambda: NS(id='assignment'))
        self.subject = CanonicalNativeReasoningBridge(binding=NativeReasoningBinding(provider_id='native',
            model_id='model', concrete_model='model', agent_profile_id='profile', agent_profile_revision=2,
            execution_profile_id='reasoning', worker_id='worker'), threads=threads, turns=self.turns,
            sessions=sessions or NS(get=lambda *a, **kw: session), usage=None, preflight=None,
            execution=NS(_session_for_assignment=lambda _: (manager, serving)), hub=EventHub(),
            attest_session=lambda *a: None)
        self.provider = NS(id='native')
        self.model = NS(id='model', concrete_model='model', supports_tools=False)
        self.request = NS(messages=(), reasoning_effort=None)
        return self.subject

    async def invoke(self):
        return await self.subject._invoke_owned(self.provider, self.model, self.request,
            actor=NS(), context=self.context, cancelled=asyncio.Event())

    async def test_real_thread_refusal_retains_stage_without_exception_prose(self):
        fixture = bootstrap.ThreadBootstrapCreateTests()
        _, planner, manager, _, thread_service = fixture._service()
        def fail(**kwargs):
            raise TypeError('private credential-shaped detail must never escape')
        planner.prepare_bootstrap = fail
        # Native diagnostics cross the real ThreadService refusal boundary. Its
        # separate canonical bridge fixture covers Profile/routing/START policy.
        async def create(**kwargs):
            return await thread_service.create(project_id='p1', bootstrap_token=kwargs['bootstrap_token'])
        self.bridge(threads=NS(create=create))
        with self.assertRaisesRegex(ModelProviderAdapterError, '^Native reasoning preparation refused$'):
            await self.invoke()
        event, = self.events
        self.assertEqual(event['execution_id'], 'executive:activation:cpo')
        self.assertEqual(event['native_preparation_stage'], 'thread_bootstrap')
        self.assertEqual(event['bootstrap_preparation_stage'], 'bootstrap_reservation')
        self.assertEqual(event['bootstrap_exception_type'], 'TypeError')
        self.assertIs(event['provider_start_called'], False)
        self.assertNotIn('private', repr(event))
        self.assertEqual(manager.started, [])
        self.turns.start.assert_not_awaited()
        self.assertEqual(self.stopped, ['original'])

    async def test_first_guard_keeps_original_exception_and_reports_closed_class(self):
        def refused():
            raise TimeoutError('private policy data')
        self.bridge(guard=refused)
        with self.assertRaisesRegex(TimeoutError, 'private policy data'):
            await self.invoke()
        event, = [row for row in self.events if row['type'] == 'native_reasoning_preparation_failed']
        self.assertEqual((event['native_preparation_stage'], event['native_exception_type']),
                         ('initial_current_guard', 'TimeoutError'))
        self.assertNotIn('provider_start_called', event)
        self.assertNotIn('private', repr(event))
        self.subject.threads.create.assert_not_awaited()
        self.turns.start.assert_not_awaited()

    async def test_after_thread_creation_failure_does_not_claim_provider_never_started(self):
        class PrivateCredentialError(Exception):
            pass
        def unavailable(*args, **kwargs):
            raise PrivateCredentialError('private session data')
        self.bridge(sessions=NS(get=unavailable))
        with self.assertRaises(ModelProviderAdapterError):
            await self.invoke()
        event, = self.events
        self.assertEqual(event['native_preparation_stage'], 'session_lookup')
        self.assertEqual(event['native_exception_type'], 'Exception')
        self.assertNotIn('private', repr(event).lower())
        self.assertNotIn('provider_start_called', event)
        self.assertIs(event['native_input_admitted'], False)
        self.assertEqual(self.stopped, ['original'])
        self.turns.start.assert_not_awaited()

    async def test_post_input_failure_remains_unknown_without_preparation_evidence(self):
        self.bridge()
        with self.assertRaises(ModelProviderOutcomeUnknownError) as caught:
            await self.invoke()
        self.assertIs(caught.exception.__cause__, self.turns.start.side_effect)
        self.assertEqual([e for e in self.events
                          if e['type'] == 'native_reasoning_preparation_failed'], [])
        event, = self.events
        self.assertEqual((event['type'], event['native_lifecycle_phase'],
                          event['interruption'], event['native_exception_type']),
                         ('native_reasoning_lifecycle_interrupted', 'queued_admission',
                          'failed', 'RuntimeError'))
        self.assertTrue(event['native_input_requested'])
        self.assertFalse(event['native_queue_submitted'])
        self.assertNotIn('native_input_admitted', event)
        self.assertNotIn('native_start_confirmed', event)
        self.assertNotIn('private', repr(event))
        self.turns.start.assert_awaited_once()
        self.assertEqual(self.stopped, ['original'])

    async def test_diagnostic_sink_failure_cannot_replace_original_or_skip_cleanup(self):
        def unavailable(*args, **kwargs):
            raise LookupError('private session data')
        self.bridge(sessions=NS(get=unavailable))
        def broken(event):
            raise RuntimeError('private journal data')
        self.subject.threads.event_sink = broken
        with self.assertRaisesRegex(ModelProviderAdapterError, '^Native reasoning preparation refused$') as caught:
            await self.invoke()
        self.assertIsInstance(caught.exception.__cause__, LookupError)
        self.assertEqual(self.stopped, ['original'])

    async def test_missing_diagnostic_context_cannot_replace_original_or_skip_cleanup(self):
        def unavailable(*args, **kwargs):
            raise LookupError('original session failure')
        self.bridge(sessions=NS(get=unavailable))
        self.context = NS(organization_id='org', workspace_id='workspace', project_id='project',
            execution_id='executive:activation:cpo', validate_current=lambda: None, process_owner=self.owner)
        with self.assertRaisesRegex(ModelProviderAdapterError, '^Native reasoning preparation refused$') as caught:
            await self.invoke()
        self.assertIsInstance(caught.exception.__cause__, LookupError)
        self.assertEqual(self.events, [])
        self.assertEqual(self.stopped, ['original'])
