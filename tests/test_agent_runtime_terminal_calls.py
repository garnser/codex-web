"""Existing observed terminal inference must match new and retained records."""
import unittest
from codex_web.agent_runtime import AgentRuntimeEvent
from tests.test_agent_runtime_telemetry import AgentRuntimeTelemetryTests

class ObservedTerminalCallTests(AgentRuntimeTelemetryTests):
    def test_real_start_then_measured_terminal_sets_same_observed_call_count(self):
        start=self.service.observe_event('anthropic','claude-code',AgentRuntimeEvent(event_type='turn/started',
            provider_native_session_id='native-session-1',provider_native_turn_id='terminal-calls'))
        self.assertIsNone(start.model_call_count)
        terminal=self.service.observe_event('anthropic','claude-code',AgentRuntimeEvent(event_type='turn/completed',
            provider_native_session_id='native-session-1',provider_native_turn_id='terminal-calls',
            payload={'usage':{'input_tokens':17,'output_tokens':9}}))
        self.assertEqual(terminal.id,start.id)
        self.assertEqual(terminal.model_call_count,1)
    def test_prior_measurement_then_terminal_without_usage_counts_once(self):
        measured=self.service.observe_event('anthropic','claude-code',AgentRuntimeEvent(event_type='thread/tokenUsage/updated',
            provider_native_session_id='native-session-1',provider_native_turn_id='prior-usage',
            payload={'params':{'tokenUsage':{'last':{'inputTokens':17,'outputTokens':9}}}}))
        self.assertIsNone(measured.model_call_count)
        terminal=self.service.observe_event('anthropic','claude-code',AgentRuntimeEvent(event_type='turn/completed',
            provider_native_session_id='native-session-1',provider_native_turn_id='prior-usage'))
        self.assertEqual(terminal.model_call_count,1)
        same=self.service.observe_event('anthropic','claude-code',AgentRuntimeEvent(event_type='turn/completed',
            provider_native_session_id='native-session-1',provider_native_turn_id='prior-usage'))
        self.assertEqual(same,terminal)

    def test_bare_terminal_and_start_never_infer_measured_calls(self):
        for event_type in ('turn/started','turn/completed'):
            row=self.service.observe_event('anthropic','claude-code',AgentRuntimeEvent(event_type=event_type,
                provider_native_session_id='native-session-1',provider_native_turn_id='no-measurement'))
            self.assertIsNone(row.model_call_count)
        self.assertIsNotNone(row.runtime_duration_seconds) # elapsed time is not actual usage

    def test_existing_non_none_call_count_and_custody_are_preserved(self):
        start=self.service.observe_event('anthropic','claude-code',AgentRuntimeEvent(event_type='turn/started',
            provider_native_session_id='native-session-1',provider_native_turn_id='preserved'))
        self.usage.upsert(start.model_copy(update={'model_call_count':3}))
        row=self.service.observe_event('anthropic','claude-code',AgentRuntimeEvent(event_type='turn/completed',
            provider_native_session_id='native-session-1',provider_native_turn_id='preserved',
            payload={'usage':{'input_tokens':19,'output_tokens':11}}))
        self.assertEqual(row.model_call_count,3)
        self.assertEqual((row.assignment_id,row.execution_id,row.agent_session_id),
            (start.assignment_id,start.execution_id,start.agent_session_id))

    def test_known_terminal_before_late_actual_usage_settles_same_record(self):
        terminal=self.service.observe_event('anthropic','claude-code',AgentRuntimeEvent(event_type='turn/completed',
            provider_native_session_id='native-session-1',provider_native_turn_id='late-usage'))
        self.assertIsNone(terminal.model_call_count)
        measured=self.service.observe_event('anthropic','claude-code',AgentRuntimeEvent(event_type='thread/tokenUsage/updated',
            provider_native_session_id='native-session-1',provider_native_turn_id='late-usage',
            payload={'params':{'tokenUsage':{'last':{'inputTokens':17,'outputTokens':9}}}}))
        self.assertEqual(measured.id,terminal.id)
        self.assertEqual(measured.model_call_count,1)

    def test_duplicate_genuine_terminal_repairs_retained_none_once(self):
        event=AgentRuntimeEvent(event_type='turn/completed',provider_native_session_id='native-session-1',
            provider_native_turn_id='legacy-terminal',payload={'usage':{'input_tokens':17,'output_tokens':9}})
        measured=self.service.observe_event('anthropic','claude-code',event)
        legacy=measured.model_copy(update={'model_call_count':None})
        self.usage.upsert(legacy) # Actual legacy source persisted this exact state.
        fixed=self.service.observe_event('anthropic','claude-code',event)
        self.assertEqual(fixed,legacy.model_copy(update={'model_call_count':1}))
        self.assertEqual(self.service.observe_event('anthropic','claude-code',event),fixed)
        self.assertEqual(fixed.event_fingerprints,legacy.event_fingerprints)

    def test_duplicate_repair_preserves_concurrently_newer_canonical_measurement(self):
        from unittest.mock import patch
        event=AgentRuntimeEvent(event_type='turn/completed',provider_native_session_id='native-session-1',
            provider_native_turn_id='concurrent-terminal',payload={'usage':{'input_tokens':17,'output_tokens':9}})
        row=self.service.observe_event('anthropic','claude-code',event).model_copy(update={'model_call_count':None})
        self.usage.upsert(row)
        reconcile=self.usage.reconcile_observed_call_count
        newer=row.model_copy(update={'input_tokens':23,'model_call_count':3,'provider_request_ids':('later-actual-request',)})
        def concurrent(*args,**kwargs):
            self.usage.upsert(newer)
            return reconcile(*args,**kwargs)
        with patch.object(self.usage,'reconcile_observed_call_count',side_effect=concurrent):
            result=self.service.observe_event('anthropic','claude-code',event)
        self.assertEqual(result,newer)

    def test_duplicate_counter_repair_notifies_exactly_the_atomic_winner(self):
        event=AgentRuntimeEvent(event_type='turn/completed',provider_native_session_id='native-session-1',
            provider_native_turn_id='notify-repair',payload={'usage':{'input_tokens':17,'output_tokens':9}})
        row=self.service.observe_event('anthropic','claude-code',event)
        self.usage.upsert(row.model_copy(update={'model_call_count':None}))
        notified=[]
        self.service.observation_notifier=notified.append
        fixed=self.service.observe_event('anthropic','claude-code',event)
        self.assertEqual(notified,[fixed])
        self.service.observe_event('anthropic','claude-code',event)
        self.assertEqual(notified,[fixed])

    def test_unchanged_and_concurrent_duplicate_repairs_do_not_notify(self):
        from unittest.mock import patch
        bare=AgentRuntimeEvent(event_type='turn/completed',provider_native_session_id='native-session-1',
            provider_native_turn_id='notify-bare')
        self.service.observe_event('anthropic','claude-code',bare)
        notified=[]
        self.service.observation_notifier=notified.append
        self.service.observe_event('anthropic','claude-code',bare)
        self.assertEqual(notified,[])
        event=AgentRuntimeEvent(event_type='turn/completed',provider_native_session_id='native-session-1',
            provider_native_turn_id='notify-concurrent',payload={'usage':{'input_tokens':17,'output_tokens':9}})
        row=self.service.observe_event('anthropic','claude-code',event)
        self.usage.upsert(row.model_copy(update={'model_call_count':None}))
        notified.clear()
        reconcile=self.usage.reconcile_observed_call_count
        def concurrent(*args,**kwargs):
            self.usage.upsert(row.model_copy(update={'model_call_count':3}))
            return reconcile(*args,**kwargs)
        with patch.object(self.usage,'reconcile_observed_call_count',side_effect=concurrent):
            self.assertEqual(self.service.observe_event('anthropic','claude-code',event).model_call_count,3)
        self.assertEqual(notified,[])


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(ObservedTerminalCallTests(name) for name in ObservedTerminalCallTests.__dict__ if name.startswith('test_'))
