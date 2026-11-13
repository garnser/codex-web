"""One same-Goal cycle across the native wire, accounting and owned completion.

The process/stdio peer is synthetic; scheduling, admission, START receipts,
terminal ownership, telemetry storage and subsequent dispatch remain real.
"""
import asyncio
import ast
import hashlib
import unittest
from pathlib import Path

from codex_web.events import EventHub
from codex_web.goals import GoalBudget
from codex_web.identity import TenantScope
from codex_web.runtime.codex import CodexRuntime
from codex_web.services.agent_runtime_telemetry import AgentRuntimeTelemetryService
from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter
from codex_web.services.goal_continuation_events import GoalContinuationEventService
from tests.test_project_goal_worker_admission import ProjectGoalWorkerAdmissionTests


class ProjectGoalContinuationCycleTests(ProjectGoalWorkerAdmissionTests):
    goal_budget = GoalBudget()

    async def asyncSetUp(self):
        await super().asyncSetUp()
        loop = asyncio.get_running_loop()
        self.loop_errors = []
        self.owned_tasks = set()
        previous_handler = loop.get_exception_handler()
        previous_factory = loop.get_task_factory()

        def capture_loop_error(_loop, context):
            self.loop_errors.append(context)

        def capture_owner(_loop, coro, **kwargs):
            task = (previous_factory(_loop, coro, **kwargs) if previous_factory is not None
                    else asyncio.Task(coro, loop=_loop, **kwargs))
            name = getattr(coro, '__qualname__', '')
            if name.startswith(('TurnExecutionService.', 'GoalContinuationEventService.',
                                '_subscribe_goal_continuation_events.')):
                # Retain completed owners too: the production registry removes
                # them in finally, before a later test observation can join.
                self.owned_tasks.add(task)
            return task

        loop.set_exception_handler(capture_loop_error)
        loop.set_task_factory(capture_owner)
        self.addCleanup(loop.set_exception_handler, previous_handler)
        self.addCleanup(loop.set_task_factory, previous_factory)
        # Restore the production scheduler overridden by the shared fixture.
        del self.execution.schedule_queue_drain
        self.execution._event_loop = loop
        self.host._release_stale_active_turn = self.recovery.release_stale_active_turn
        self.host._clear_thread_active = self.execution.clear_thread_active
        self.native.host.hub = EventHub()
        self.native.host._record_thread_activity = self.execution.record_thread_activity
        self.native.host._record_terminal_turn_result = self.execution.record_terminal_turn_result
        self.native.host._schedule_queue_drain = self.execution.schedule_queue_drain

        # Only the external native peer supplies these responses. In particular,
        # a second turn must not reuse the shared fixture's fixed native ID.
        self.native_turns = []

        async def external_reply(message, **options):
            await CodexRuntime._send(self.native, message, **options)
            if message['method'] == 'turn/start':
                native_id = f'cycle-native-turn-{len(self.native_turns) + 1}'
                self.native_turns.append(native_id)
                result = {'turn': {'id': native_id, 'threadId': self.thread}}
            else:
                result = {'thread': {'id': self.thread}}
            await self.native._dispatch_message({'id': message['id'], 'result': result})

        self.native._send = external_reply
        adapter = CodexAgentRuntimeAdapter(self.native)
        self.telemetry = AgentRuntimeTelemetryService(
            self.usage, self.sessions.store, self.sessions.registry)
        self.telemetry.subscribe(adapter)
        self.addCleanup(self.telemetry.close)

        # Use the actual application subscriber without importing application
        # startup or replicating its event-routing implementation in the test.
        source = Path(__file__).resolve().parents[1] / 'codex_web' / 'application.py'
        nodes = [node for node in ast.parse(source.read_text()).body
                 if isinstance(node, ast.FunctionDef)
                 and node.name == '_subscribe_goal_continuation_events']
        self.assertEqual(len(nodes), 1)
        context = {'asyncio': asyncio, 'TenantScope': TenantScope,
            'agent_session_store': self.sessions.store,
            'goal_continuation_event_service': GoalContinuationEventService(
                self.goal_bindings, self.sessions, self.goal_continuation)}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), context)
        unsubscribe = context['_subscribe_goal_continuation_events'](
            adapter, provider_id='openai', runtime_id='codex')
        self.addCleanup(unsubscribe)
        # Registered last so all owners settle before unsubscribing or tearing
        # down the private storage/session fixture, including on assertion failure.
        self.addAsyncCleanup(self.finish_owned_work)

    async def join_owned_work(self):
        # Join actual owners, not a sleep/latency assertion or a manual drain.
        # Any automatic second dispatch must schedule its own admission task.
        joined = set()
        failures = []
        while True:
            tasks = set(self.owned_tasks)
            tasks.update(self.execution.queue_drain_tasks.values())
            tasks.update(self.execution.assignment_completion_tasks.values())
            tasks.update(self.execution.thread_completion_tasks.values())
            tasks.update(task for task in asyncio.all_tasks()
                         if getattr(task.get_coro(), '__qualname__', '')
                         == 'GoalContinuationEventService.handle_event')
            tasks.discard(asyncio.current_task())
            tasks.difference_update(joined)
            if not tasks:
                self.assertEqual(failures, [], 'Owned continuation task failed')
                return
            # Observe every result before raising, so one failure cannot leave
            # another child running beyond private fixture cleanup.
            results = await asyncio.gather(*tasks, return_exceptions=True)
            joined.update(tasks)
            failures.extend(item for item in results if isinstance(item, BaseException))

    async def finish_owned_work(self):
        try:
            await self.join_owned_work()
        finally:
            self.assertEqual(self.loop_errors, [], 'Unexpected background loop exception')

    async def test_terminal_accounting_automatically_starts_next_same_goal_once(self):
        result = await self.goal_continuation.dispatch_once(
            self.goal_binding.id, scope=self.actor.tenant)
        self.assertEqual(result.outcome, 'queued', result.reason)
        await self.join_owned_work()
        self.assertEqual(self.native_turns, ['cycle-native-turn-1'], self.native_diagnostics())
        first = self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant)
        delivery = first.project_continuation.delivery
        self.assertEqual(delivery.phase, 'started')
        original_start = self.execution.queued_preflight.store.get(delivery.admission_attempt_id)
        self.assertIsNotNone(original_start.queued_start_receipt)
        original_start_json = original_start.model_dump(mode='json')

        # Feed provider notifications into the actual native runtime. No direct
        # usage upsert, terminal callback, resolve or subsequent dispatch call.
        await self.native._handle_message({'method': 'thread/tokenUsage/updated', 'params': {
            'threadId': self.thread, 'turnId': delivery.native_turn_id,
            'tokenUsage': {'last': {'inputTokens': 40, 'outputTokens': 5}}}})
        terminal = {'method': 'turn/completed', 'params': {
            'threadId': self.thread,
            'turn': {'id': delivery.native_turn_id, 'threadId': self.thread, 'status': 'completed'}}}
        await self.native._handle_message(terminal)
        await self.join_owned_work()

        self.assertEqual(self.native_turns, ['cycle-native-turn-1', 'cycle-native-turn-2'],
                         self.native_diagnostics())
        current = self.goal_bindings.get(first.id, scope=self.actor.tenant)
        self.assertEqual((current.goal_id, current.goal_revision, current.work_item_refs),
                         (first.goal_id, first.goal_revision, first.work_item_refs))
        self.assertEqual(current.project_continuation.delivery.phase, 'started')
        self.assertNotEqual(current.project_continuation.delivery.id, delivery.id)
        self.assertNotEqual(current.project_continuation.delivery.admission_attempt_id,
                            delivery.admission_attempt_id)
        second_start = self.execution.queued_preflight.store.get(
            current.project_continuation.delivery.admission_attempt_id)
        self.assertIsNotNone(second_start.queued_start_receipt)
        self.assertEqual(second_start.queued_start_receipt.provider_native_turn_id,
                         'cycle-native-turn-2')
        self.assertEqual(current.project_continuation.reserved_calls, 2)
        self.assertEqual(current.project_continuation.model_calls, 1)
        self.assertEqual(current.project_continuation.input_tokens, 40)
        self.assertEqual(current.project_continuation.output_tokens, 5)
        usage_id = 'runtime-usage-' + hashlib.sha256(
            f'{first.agent_session_id}\n{delivery.native_turn_id}'.encode()).hexdigest()[:32]
        usage = self.usage.get(usage_id)
        self.assertIsNotNone(usage)
        self.assertEqual((usage.agent_session_id, usage.assignment_id,
                          usage.provider_native_turn_id, usage.terminal_outcome),
                         (first.agent_session_id, delivery.assignment_id,
                          delivery.native_turn_id, 'succeeded'))
        self.assertIsNotNone(usage.completed_at)
        self.assertEqual(self.execution.queued_preflight.store.get(
            delivery.admission_attempt_id).model_dump(mode='json'), original_start_json)
        self.assertEqual(sum(event.reason == 'Original Project Goal terminal usage settled'
                             for event in self.goal_bindings.store.load().events), 1)

        # Redelivery of the old terminal event must not consume/release turn 2.
        before_duplicate = current.model_dump(mode='json')
        await self.native._handle_message(terminal)
        await self.join_owned_work()
        self.assertEqual(len(self.starts()), 2)
        self.assertEqual(self.goal_bindings.get(first.id, scope=self.actor.tenant).model_dump(
            mode='json'), before_duplicate)
        self.assertEqual(self.usage.get(usage_id), usage)


def load_tests(loader, standard_tests, pattern):
    # Shared fixture classes include unrelated tests; collect only this new case.
    return unittest.TestSuite((ProjectGoalContinuationCycleTests(
        'test_terminal_accounting_automatically_starts_next_same_goal_once'),))
