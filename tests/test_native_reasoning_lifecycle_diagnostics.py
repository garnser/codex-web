"""Deadline observations cannot authorize retry or replace original cleanup."""
import asyncio
import math
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from codex_web.events import EventHub
from codex_web.services.native_model_provider import NativeProcessOwnership
from codex_web.services.native_reasoning_bridge import CanonicalNativeReasoningBridge, NativeReasoningBinding


class NativeLifecycleDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, phase, *, sink_failure=None, cleanup_failure=None):
        entered, release, stopping, joined = (asyncio.Event() for _ in range(4))
        events, calls = [], []
        owner = NativeProcessOwnership()
        session = NS(id='session-original', assignment_id='assignment-original',
            provider_id='openai', runtime_id='codex-reasoning', provider_native_session_id='thread-original',
            project_id='project', resource_ids=(), worker_id='worker', model='model')
        assignment = NS(id=session.assignment_id)
        context = NS(organization_id='org', workspace_id='workspace', project_id='project',
            activation_id='activation', role_id='cpo', execution_id='executive:activation:cpo',
            process_owner=owner, validate_current=lambda: None)
        execution = NS(thread_completion_tasks={})
        test = self
        class Bridge(CanonicalNativeReasoningBridge):
            async def _wait_original_terminal(self, terminal, refused, cancelled, **kwargs):
                # Feed the actual bounded text observer so completion cases
                # reach attribution instead of failing on an absent answer.
                await self.hub.publish({'type': 'codex.event', 'message': {
                    'method': 'item/completed', 'params': {'threadId': 'thread-original',
                    'item': {'type': 'agentMessage', 'phase': 'final_answer', 'text': 'fixture answer'}}}})
                await asyncio.sleep(0)
                if phase == 'terminal_observation':
                    entered.set()
                    return await super()._wait_original_terminal(terminal, refused, cancelled, **kwargs)
                if phase == 'usage_observation':
                    asyncio.get_running_loop().call_soon(entered.set)
                else:
                    self._usage_waiters[session.id][1].set_result('turn-original')
                return 'turn-original'
            def _result(self, **kwargs):
                calls.append('result')
                test.assertEqual((kwargs['thread_id'], kwargs['turn_id'], kwargs['queued_id']),
                    ('thread-original', 'turn-original', 'queue-original'))
                return 'original-result'
        async def create(**kwargs):
            calls.append('create')
            if phase == 'thread_bootstrap':
                entered.set()
                await release.wait()
            return {'thread': {'id': 'thread-original'}, 'agentSessionId': session.id}
        async def start(*args, **kwargs):
            calls.append('input_requested')
            if phase == 'queued_admission':
                entered.set()
                await release.wait()
            return {'queuedId': 'queue-original'}
        async def complete(*args, **kwargs):
            calls.append('complete')
            if phase == 'assignment_completion':
                entered.set()
                await release.wait()
        async def stop():
            calls.append('stop')
            stopping.set()
            if phase == 'cleanup':
                entered.set()
            await release.wait()
            joined.set()
            if cleanup_failure is not None:
                raise cleanup_failure
        owner.attach(assignment.id, stop)
        def sink(event):
            if event['type'] == 'native_reasoning_lifecycle_interrupted':
                events.append(event)
                if sink_failure is not None:
                    raise sink_failure
        manager = NS(complete=complete)
        execution._session_for_assignment = lambda _: (manager, NS(
            runtime=NS(reasoning_refusal=False), validate_current=lambda: assignment))
        bridge = Bridge(binding=NativeReasoningBinding(provider_id='native', model_id='model',
            concrete_model='model', agent_profile_id='profile', agent_profile_revision=1,
            execution_profile_id='reasoning', worker_id='worker'),
            threads=NS(create=create, event_sink=sink), turns=NS(start=start),
            sessions=NS(get=lambda *a, **k: session), usage=None, preflight=None,
            execution=execution, hub=EventHub(), attest_session=lambda *a: None)
        completion = None
        if phase == 'completion_observation':
            completion = asyncio.get_running_loop().create_future()
            class CompletionLookup(dict):
                def get(self, key, default=None):
                    # Signal when the actual completion owner is selected,
                    # after the bridge has left measured-usage observation.
                    asyncio.get_running_loop().call_soon(entered.set)
                    return super().get(key, default)
            execution.thread_completion_tasks = CompletionLookup({'thread-original': completion})
        return NS(bridge=bridge, entered=entered, release=release, stopping=stopping,
            joined=joined, events=events, calls=calls, context=context,
            completion=completion,
            provider=NS(id='native'), model=NS(id='model', concrete_model='model', supports_tools=False),
            request=NS(messages=(), reasoning_effort=None))

    async def interrupted(self, phase, *, deadline=False, sink_failure=None, cleanup_failure=None):
        f = self.fixture(phase, sink_failure=sink_failure, cleanup_failure=cleanup_failure)
        timeout = asyncio.timeout(None)
        async def original():
            async with timeout:
                return await f.bridge.invoke(f.provider, f.model, f.request, actor=NS(), context=f.context)
        task = asyncio.create_task(original())
        interruption_requested = False
        try:
            await asyncio.wait_for(f.entered.wait(), 2)
            if deadline:
                timeout.reschedule(asyncio.get_running_loop().time() - 1)
            else:
                task.cancel()
            interruption_requested = True
            # Snapshot must occur at the interrupted wait, before cleanup can
            # replace that phase. The original owner remains positively joined.
            for _ in range(30):
                if f.events:
                    break
                await asyncio.sleep(0)
                if task.done():
                    break
            self.assertEqual(len(f.events), 1)
            event = f.events[0]
            self.assertEqual((event['type'], event['native_lifecycle_phase'], event['interruption']),
                ('native_reasoning_lifecycle_interrupted', phase, 'cancelled'))
            self.assertEqual(event['execution_id'], f.context.execution_id)
            self.assertTrue(all(math.isfinite(event[k]) and 0 <= event[k] <= 86400
                for k in ('elapsed_seconds', 'phase_elapsed_seconds')))
            self.assertLessEqual(event['phase_elapsed_seconds'], event['elapsed_seconds'])
            self.assertEqual(event['native_input_requested'], phase not in {'thread_bootstrap'})
            self.assertEqual(event['native_queue_submitted'], phase not in {'thread_bootstrap','queued_admission'})
            self.assertNotIn('native_input_admitted', event)
            self.assertNotIn('native_start_confirmed', event)
            if phase != 'thread_bootstrap':
                self.assertEqual(event['thread_id'], 'thread-original')
                self.assertEqual(event['session_id'], 'session-original')
                self.assertEqual(event['assignment_id'], 'assignment-original')
            if phase not in {'thread_bootstrap','queued_admission'}:
                self.assertEqual(event['queued_id'], 'queue-original')
            self.assertFalse(task.done())
        finally:
            # An entry assertion can fail before the intended interruption.
            # Cancel that original only once; never recancel its owned cleanup.
            if not interruption_requested and not task.done():
                task.cancel()
            f.release.set()
            if f.completion is not None and not f.completion.done():
                f.completion.set_result(None)
            outcome = (await asyncio.gather(task, return_exceptions=True))[0]
        self.assertIsInstance(outcome, TimeoutError if deadline else asyncio.CancelledError)
        self.assertTrue(f.joined.is_set())
        self.assertEqual(f.calls.count('create'), 1)
        self.assertEqual(f.calls.count('stop'), 1)
        self.assertLessEqual(f.calls.count('input_requested'), 1)
        self.assertEqual(f.bridge._usage_waiters, {})
        self.assertEqual(f.bridge._admission_waiters, {})
        self.assertNotIn('private', repr(f.events))
        return f

    async def test_deadline_snapshot_at_each_actual_bridge_wait(self):
        for phase in ('thread_bootstrap','queued_admission','terminal_observation',
                      'usage_observation','completion_observation','assignment_completion','cleanup'):
            with self.subTest(phase=phase):
                await self.interrupted(phase, deadline=True)

    async def test_cancel_preserves_original_phase_and_positive_join(self):
        await self.interrupted('terminal_observation')

    async def test_failed_diagnostic_sink_and_cleanup_cannot_replace_cancellation(self):
        await self.interrupted('usage_observation', sink_failure=RuntimeError('private sink'),
                               cleanup_failure=RuntimeError('private cleanup'))

    async def test_sink_cancellation_cannot_interrupt_cleanup_join(self):
        await self.interrupted('queued_admission', sink_failure=asyncio.CancelledError())

    async def test_original_initial_timeout_is_preserved_with_closed_metadata(self):
        f = self.fixture('initial_current_guard')
        original = TimeoutError('private initial error')
        def guard():raise original
        f.context.validate_current = guard
        with self.assertRaises(TimeoutError) as caught:
            await f.bridge.invoke(f.provider, f.model, f.request, actor=NS(), context=f.context)
        self.assertIs(caught.exception, original)
        event, = f.events
        self.assertEqual((event['native_lifecycle_phase'], event['interruption']),
                         ('initial_current_guard','timeout'))
        self.assertEqual(f.calls, [])
        self.assertNotIn('private', repr(event))

    async def test_success_emits_no_interruption_and_keeps_single_completion(self):
        f = self.fixture('success');f.release.set()
        result = await f.bridge.invoke(f.provider, f.model, f.request, actor=NS(), context=f.context)
        self.assertEqual(result, 'original-result')
        self.assertEqual(f.events, [])
        self.assertEqual(f.calls, ['create','input_requested','result','complete','stop'])
        self.assertTrue(f.joined.is_set())

    async def test_ordinary_admitted_failures_keep_phase_category_unknown_and_join(self):
        from codex_web.model_providers import ModelProviderOutcomeUnknownError
        for phase in ('terminal_observation', 'result_attribution', 'assignment_completion'):
            with self.subTest(phase=phase):
                f = self.fixture('success'); f.release.set()
                original = ValueError('private provider response')
                if phase == 'terminal_observation':
                    async def terminal(*args, **kwargs): raise original
                    f.bridge._wait_original_terminal = terminal
                elif phase == 'result_attribution':
                    def result(**kwargs): raise original
                    f.bridge._result = result
                else:
                    manager, _ = f.bridge.execution._session_for_assignment('assignment-original')
                    manager.complete = AsyncMock(side_effect=original)
                with self.assertRaises(ModelProviderOutcomeUnknownError) as caught:
                    await f.bridge.invoke(f.provider, f.model, f.request, actor=NS(), context=f.context)
                self.assertIs(caught.exception.__cause__, original)
                self.assertEqual(len(f.events), 1, 'ordinary admitted failure lost lifecycle evidence')
                event, = f.events
                self.assertEqual((event['native_lifecycle_phase'], event['interruption'],
                                  event['native_exception_type']), (phase, 'failed', 'ValueError'))
                self.assertNotIn('private', repr(event))
                self.assertTrue(f.joined.is_set())
                self.assertEqual(f.calls.count('stop'), 1)
                self.assertEqual(f.bridge._usage_waiters, {})
                self.assertEqual(f.bridge._admission_waiters, {})

    async def test_ordinary_failure_sink_cannot_replace_original_or_join(self):
        from codex_web.model_providers import ModelProviderOutcomeUnknownError
        f = self.fixture('success', sink_failure=asyncio.CancelledError()); f.release.set()
        original = TypeError('private malformed result')
        def result(**kwargs): raise original
        f.bridge._result = result
        with self.assertRaises(ModelProviderOutcomeUnknownError) as caught:
            await f.bridge.invoke(f.provider, f.model, f.request, actor=NS(), context=f.context)
        self.assertIs(caught.exception.__cause__, original)
        self.assertTrue(f.joined.is_set()); self.assertEqual(f.calls.count('stop'), 1)
        self.assertEqual(len(f.events), 1)

    async def test_ordinary_cleanup_failure_retains_original_exception(self):
        original = LookupError('private cleanup failure')
        f = self.fixture('success', cleanup_failure=original); f.release.set()
        with self.assertRaises(LookupError) as caught:
            await f.bridge.invoke(f.provider, f.model, f.request, actor=NS(), context=f.context)
        self.assertIs(caught.exception, original)
        event, = f.events
        self.assertEqual((event['native_lifecycle_phase'], event['interruption'],
                          event['native_exception_type']), ('cleanup', 'failed', 'LookupError'))
        self.assertTrue(f.joined.is_set()); self.assertNotIn('private', repr(event))

    def test_ordinary_failure_unknown_exception_name_is_closed(self):
        f = self.fixture('unused')
        malicious = type('private_credential_exception', (Exception,), {})('private payload')
        checkpoint = dict(started_at=0., phase_started_at=0., phase='result_attribution')
        f.bridge._lifecycle_interrupted(f.context, checkpoint, 'failed', malicious)
        event, = f.events
        self.assertEqual(event['native_exception_type'], 'Exception')
        self.assertNotIn('private', repr(event))

    async def test_entry_timeout_preserves_failure_and_joins_pending_original(self):
        original_fixture = self.fixture
        original_wait_for = asyncio.wait_for
        captured = []
        failure = TimeoutError('original fixture entry timeout')
        def fixture(*args, **kwargs):
            f = original_fixture(*args, **kwargs)
            captured.append((f, f.entered))
            f.entered = asyncio.Event()  # The helper's expected entry never arrives.
            return f
        async def entry_timeout(awaitable, timeout):
            try:
                await original_wait_for(captured[0][1].wait(), timeout)
            finally:
                awaitable.close()
            raise failure
        with patch.object(self, 'fixture', side_effect=fixture), patch(
                'asyncio.wait_for', side_effect=entry_timeout):
            with self.assertRaises(TimeoutError) as caught:
                await self.interrupted('usage_observation')
        self.assertIs(caught.exception, failure)
        f, _ = captured[0]
        self.assertTrue(f.joined.is_set()); self.assertEqual(f.calls.count('stop'), 1)
        self.assertEqual(f.bridge._usage_waiters, {})
        self.assertEqual(f.bridge._admission_waiters, {})

    def test_diagnostic_rejects_open_phase_and_sensitive_identifiers(self):
        f = self.fixture('unused')
        checkpoint = dict(started_at=0., phase_started_at=0., phase='untrusted phase private text')
        f.bridge._lifecycle_interrupted(f.context, checkpoint, 'cancelled')
        self.assertEqual(f.events, [])
        checkpoint = dict(started_at=0., phase_started_at=0., phase='cleanup',
                          thread_id='private\ncredential', turn_id='x'*201)
        f.bridge._lifecycle_interrupted(f.context, checkpoint, 'cancelled')
        event, = f.events
        self.assertNotIn('thread_id', event);self.assertNotIn('turn_id', event)
        self.assertNotIn('private', repr(event))
