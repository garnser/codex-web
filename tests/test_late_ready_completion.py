"""Existing status events follow real native terminals; private canonical state only."""
import ast
import asyncio
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from codex_web.action_intents import ActionIntentStatus, ActionIntentLease
from codex_web.services.event_journal import EventJournal
from codex_web.storage.state_store import StorageOperationBudget, storage_operation_budget
from codex_web.services.task_source_action_provider import TASK_SOURCE_CREATE_ACTION_ID
from tests import test_post_turn_readiness as fixture


class LateReadyCompletionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.PostTurnReadinessTests.asyncSetUp
    asyncTearDown = fixture.PostTurnReadinessTests.asyncTearDown
    _assignment = staticmethod(fixture.PostTurnReadinessTests._assignment)
    _publish_worker_authority = fixture.PostTurnReadinessTests._publish_worker_authority
    _publish_profile = fixture.PostTurnReadinessTests._publish_profile
    _request = fixture.PostTurnReadinessTests._request
    change_assignment = fixture.PostTurnReadinessTests.change_assignment
    submit = fixture.PostTurnReadinessTests.submit
    respond = fixture.PostTurnReadinessTests.respond
    prepare_target = fixture.PostTurnReadinessTests.prepare_target
    build = fixture.PostTurnReadinessTests.build
    prepare = fixture.PostTurnReadinessTests.prepare
    new_callback = fixture.PostTurnReadinessTests.new_callback
    terminal_runtime = fixture.PostTurnReadinessTests.terminal_runtime

    def configured_runtime(self):
        runtime, active = self.terminal_runtime()
        journal = EventJournal(Path(self.temp.name) / 'real-terminal-events.jsonl')
        def append(event):
            recorded = journal.append(event)
            self.events.append(recorded)
            return recorded
        runtime.host._append_bot_event = append
        self.callback.append_event = append
        runtime.readiness_terminal_events = journal.recent
        runtime.readiness_intent_lookup = self.runtime.intents.store.get
        # Execute the actual application composition hook without importing the
        # application or touching its default/shared state and provider threads.
        path = Path(__file__).resolve().parents[1] / 'codex_web/application.py'
        tree = ast.parse(path.read_text())
        hook = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
            and node.name == '_notify_automation_action_intent')
        namespace = {'turn_execution_service': runtime, 'asyncio': asyncio,
                     'TASK_SOURCE_CREATE_ACTION_ID': TASK_SOURCE_CREATE_ACTION_ID}
        exec(compile(ast.Module(body=[hook], type_ignores=[]), str(path), 'exec'), namespace)
        self.runtime.intents.status_notifier = namespace[hook.name]
        return runtime, active, journal

    def mark_inflight(self):
        def update(state):
            state.intents = [row.model_copy(update={'status': ActionIntentStatus.EXECUTING,
                'lease': ActionIntentLease(owner=self.worker.id, acquired_at=time.time(),
                                          expires_at=time.time() + 120)})
                if row.id == self.intent_id else row for row in state.intents]
            return state
        self.runtime.intents.store.update(update, intent_ids=(self.intent_id,))

    async def terminal_before_result(self):
        await self.prepare()
        self.before = self.runtime.intents.history(self.intent_id, self.admin)
        self.mark_inflight()
        runtime, active, journal = self.configured_runtime()
        runtime._schedule_assignment_completion(active, succeeded=True,
            message={'method': 'turn/completed', 'params': {'turn': {'id': active.turn_id}}})
        await asyncio.gather(*runtime.assignment_completion_tasks.values())
        self.assertFalse(runtime.thread_completion_tasks)
        self.assertEqual(self.runtime.intents.store.get(self.intent_id).status, ActionIntentStatus.EXECUTING)
        self.original_calls = len(self.calls)
        return runtime, active, journal

    async def late_result(self, runtime):
        # A real canonical status transition from a storage thread uses the same
        # production notifier, not a model turn or direct recovery invocation.
        await asyncio.to_thread(self.runtime.intents._ready_reconciliation,
            self.intent_id, 'Private fixture late verification refusal')
        await self.join_followups(runtime)

    async def join_followups(self, runtime):
        for _ in range(6):  # fixture event-loop flush, never production polling
            await asyncio.sleep(0)
            tasks = tuple(runtime.terminal_recovery_tasks.values())
            if tasks:
                await asyncio.gather(*tasks)
        self.assertFalse(runtime.terminal_recovery_tasks)
        self.assertFalse(runtime.thread_completion_tasks)

    def assert_originals(self):
        history = self.runtime.intents.history(self.intent_id, self.admin)
        self.assertEqual(history['receipts'], self.before['receipts'])
        self.assertEqual(history['intent']['attempt'], self.before['intent']['attempt'])
        self.assertEqual(sum(method == 'PUT' for method, _ in self.calls), 1)
        self.assertEqual(self.workers.store.assignment(self.assignment.id).procedure_discoveries[:64], self.originals)
        return history

    async def test_late_unresolved_status_settles_from_actual_terminal_without_another_turn(self):
        runtime, _, _ = await self.terminal_before_result()
        await self.late_result(runtime)
        history = self.assert_originals()
        self.assertEqual(history['intent']['status'], 'succeeded')
        self.assertTrue(history['verifications'][-1]['verified'])
        self.assertTrue(all(method == 'GET' for method, _ in self.calls[self.original_calls:]))
        self.assertEqual(sum(event['type'] == 'post_turn_readiness_started' for event in self.events), 1)

    async def test_expired_original_request_budget_does_not_replace_finite_callback_budget(self):
        runtime, _, _ = await self.terminal_before_result()
        notify = self.runtime.intents.status_notifier
        def expired_request_notification(intent):
            expired = StorageOperationBudget(1)
            expired.cancelled.set()
            token = storage_operation_budget.set(expired)
            try:
                notify(intent)
            finally:
                storage_operation_budget.reset(token)
        self.runtime.intents.status_notifier = expired_request_notification
        await self.late_result(runtime)
        self.assertEqual(self.assert_originals()['intent']['status'], 'succeeded')

    async def test_changed_fence_refuses_late_notification_without_provider_access(self):
        runtime, _, _ = await self.terminal_before_result()
        self.change_assignment(fence=self.assignment.fence + 1)
        await self.late_result(runtime)
        self.assertEqual(len(self.calls), self.original_calls)
        self.assertEqual(self.assert_originals()['intent']['status'], 'requires_reconciliation')

    async def test_current_pause_still_refuses_late_settlement(self):
        runtime, _, _ = await self.terminal_before_result()
        self.controller.pause(actor_id=self.admin.identity_id)
        await self.late_result(runtime)
        self.assertEqual(len(self.calls), self.original_calls)
        self.assertEqual(self.assert_originals()['intent']['status'], 'requires_reconciliation')

    async def test_changed_definition_refuses_late_settlement(self):
        runtime, _, _ = await self.terminal_before_result()
        self._publish_profile(self.profile_payload, '1.1', project='project-a')
        await self.late_result(runtime)
        self.assertEqual(len(self.calls), self.original_calls)
        self.assertEqual(self.assert_originals()['intent']['status'], 'requires_reconciliation')

    async def test_missing_terminal_observation_does_not_authorize_recovery(self):
        runtime, _, _ = await self.terminal_before_result()
        runtime.readiness_terminal_events = lambda **kwargs: []
        await self.late_result(runtime)
        self.assertEqual(len(self.calls), self.original_calls)
        self.assertEqual(self.assert_originals()['intent']['status'], 'requires_reconciliation')

    async def test_terminal_observation_before_attempt_cannot_authorize_recovery(self):
        runtime, _, journal = await self.terminal_before_result()
        events = journal.recent(limit=200)
        started = self.runtime.intents.store.get(self.intent_id).execution_started_at
        runtime.readiness_terminal_events = lambda **kwargs: [
            {**event, 'created_at': started - 1} for event in events]
        await self.late_result(runtime)
        self.assertEqual(len(self.calls), self.original_calls)
        self.assertEqual(self.assert_originals()['intent']['status'], 'requires_reconciliation')

    async def test_status_during_checkpoint_follows_owned_completion_without_rewriting_failure(self):
        await self.prepare()
        self.before = self.runtime.intents.history(self.intent_id, self.admin)
        self.mark_inflight()
        runtime, active, journal = self.configured_runtime()
        manager, _ = runtime._session_for_assignment(active.assignment_id)
        entered, release = asyncio.Event(), asyncio.Event()
        async def checkpoint(_assignment):
            entered.set()
            await release.wait()
            raise RuntimeError('private checkpoint failure')
        manager.checkpoint = AsyncMock(side_effect=checkpoint)
        runtime._schedule_assignment_completion(active, succeeded=True,
            message={'method': 'turn/completed', 'params': {'turn': {'id': active.turn_id}}})
        await entered.wait()
        self.original_calls = len(self.calls)
        await asyncio.to_thread(self.runtime.intents._ready_reconciliation,
            self.intent_id, 'Private late result during owned checkpoint')
        await asyncio.sleep(0)
        self.assertTrue(runtime.thread_is_active(active.thread_id))
        self.assertEqual(len(self.calls), self.original_calls)
        completion = tuple(runtime.assignment_completion_tasks.values())
        release.set()
        await asyncio.gather(*completion)
        await self.join_followups(runtime)
        self.assertEqual(self.assert_originals()['intent']['status'], 'succeeded')
        event = next(row for row in journal.recent(limit=200)
                     if row['type'] == 'thread_bootstrap_turn_completed')
        self.assertFalse(event['succeeded'])
        self.assertEqual(event['repository_checkpoint_error'], 'private checkpoint failure')
        manager.checkpoint.assert_awaited_once()
        self.assertEqual(sum(row['type'] == 'post_turn_readiness_started'
                             for row in self.events), 1)

    async def test_busy_native_input_is_not_interrupted_by_status_notification(self):
        runtime, active, _ = await self.terminal_before_result()
        runtime._active_turn = lambda thread: active
        await self.late_result(runtime)
        self.assertEqual(len(self.calls), self.original_calls)
        self.assertEqual(self.assert_originals()['intent']['status'], 'requires_reconciliation')

    async def test_duplicate_and_reopened_notifications_preserve_failed_verification_once(self):
        runtime, _, _ = await self.terminal_before_result()
        self.pr['sha'] = 'c' * 40
        await self.late_result(runtime)
        history = self.assert_originals()
        self.assertFalse(history['verifications'][-1]['verified'])
        calls = len(self.calls)
        self.callback = self.new_callback()
        reopened, _, _ = self.configured_runtime()
        # A restarted runtime has acquired its owner loop, but uses ONLY the
        # previously recorded genuine terminal and canonical recovery marker.
        reopened._event_loop = asyncio.get_running_loop()
        await self.late_result(reopened)
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.runtime.intents.history(self.intent_id, self.admin)['verifications'],
                         history['verifications'])

    async def test_new_native_turn_winning_start_lock_prevents_old_terminal_settlement(self):
        runtime, active, _ = await self.terminal_before_result()
        lock = runtime._turn_start_locks.setdefault(active.thread_id, asyncio.Lock())
        await lock.acquire()
        read = threading.Event()
        lookup = runtime.readiness_intent_lookup
        def observed_lookup(intent_id):
            value = lookup(intent_id)
            read.set()
            return value
        runtime.readiness_intent_lookup = observed_lookup
        try:
            await asyncio.to_thread(self.runtime.intents._ready_reconciliation,
                self.intent_id, 'Private fixture result while new native input owns lock')
            self.assertTrue(await asyncio.to_thread(read.wait, 2))
            runtime._active_turn = lambda _thread: active.model_copy(
                update={'turn_id': 'genuine-new-native-turn'})
        finally:
            lock.release()
        await self.join_followups(runtime)
        self.assertEqual(len(self.calls), self.original_calls)
        self.assertEqual(self.assert_originals()['intent']['status'], 'requires_reconciliation')

    async def test_changed_attempt_while_waiting_start_lock_cannot_reuse_terminal(self):
        runtime, active, _ = await self.terminal_before_result()
        lock = runtime._turn_start_locks.setdefault(active.thread_id, asyncio.Lock())
        await lock.acquire()
        read = threading.Event()
        lookup = runtime.readiness_intent_lookup
        def observed_lookup(intent_id):
            value = lookup(intent_id)
            read.set()
            return value
        runtime.readiness_intent_lookup = observed_lookup
        try:
            await asyncio.to_thread(self.runtime.intents._ready_reconciliation,
                self.intent_id, 'Private fixture pending behind owned start lock')
            self.assertTrue(await asyncio.to_thread(read.wait, 2))
            def advance(state):
                state.intents = [row.model_copy(update={'attempt': row.attempt + 1})
                    if row.id == self.intent_id else row for row in state.intents]
                return state
            self.runtime.intents.store.update(advance, intent_ids=(self.intent_id,))
        finally:
            lock.release()
        await self.join_followups(runtime)
        self.assertEqual(len(self.calls), self.original_calls)
        history = self.runtime.intents.history(self.intent_id, self.admin)
        self.assertEqual(history['receipts'], self.before['receipts'])
        self.assertEqual(history['intent']['status'], 'requires_reconciliation')

    async def test_late_callback_keeps_completion_barrier_until_actual_settlement(self):
        runtime, active, _ = await self.terminal_before_result()
        entered, release = asyncio.Event(), asyncio.Event()
        original = runtime.post_turn_readiness
        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        runtime.post_turn_readiness = held
        await asyncio.to_thread(self.runtime.intents._ready_reconciliation,
            self.intent_id, 'Private fixture late verification refusal')
        await asyncio.wait_for(entered.wait(), 3)
        self.assertTrue(runtime.thread_is_active(active.thread_id))
        self.assertTrue(runtime._turn_start_locks[active.thread_id].locked())
        # Two independently delivered canonical unresolved-status notifications
        # coalesce behind this actual owned followup; neither repeats effects.
        for _ in range(2):
            await asyncio.to_thread(self.runtime.intents._ready_reconciliation,
                self.intent_id, 'Private duplicate while owned callback is active')
        release.set()
        await self.join_followups(runtime)
        self.assertEqual(self.assert_originals()['intent']['status'], 'succeeded')
        self.assertEqual(sum(row['type'] == 'post_turn_readiness_started'
                             for row in self.events), 1)
