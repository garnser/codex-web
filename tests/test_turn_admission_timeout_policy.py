"""Actual TurnService ingress guard ownership with a controlled store-budget clock.

No application composition or elapsed wall-time wait: the real owned operation
and its propagated StorageOperationBudget judge a six-second logical read.
"""
import asyncio
import math
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.models import TurnCreate
from codex_web.storage.state_store import (
    StorageOperationBudget, check_operation_budget, storage_operation_budget,
)
from tests import test_immediate_guarded_turn_admission as fixture


class TurnAdmissionTimeoutPolicyTests(unittest.IsolatedAsyncioTestCase):
    def environment(self):
        env = fixture.ImmediateGuardedTurnAdmissionTests()
        env.setUp()
        self.addCleanup(env.doCleanups)
        # Exercise actual immediate start or explicit durable enqueue separately;
        # procedure/native work is not what this pre-admission boundary owns.
        env.runtime.queued_preflight = None
        env.runtime.procedure_admission = None
        return env

    async def start(self, env, guard, *, queued=False):
        return await env.service.start('thread', TurnCreate(
            project_id='project', message='one original', defer_start=queued),
            actor=env.actor, execution_id='same-execution', admission_guard=guard)

    async def test_disabled_policy_allows_actual_start_and_preenqueue_guard_after_five_seconds(self):
        for configured in ('0', '-1'):
            for queued in (False, True):
                with self.subTest(configured=configured, queued=queued):
                    env = self.environment()
                    clock = [100.]
                    observed = []
                    def guard():
                        budget = storage_operation_budget.get()
                        observed.append(budget)
                        clock[0] += 6.
                        check_operation_budget()
                    with patch.dict(os.environ, {'CODEX_WEB_LOCAL_PREPARATION_TIMEOUT_SECONDS': configured}), \
                            patch('codex_web.storage.state_store.time', SimpleNamespace(monotonic=lambda: clock[0])):
                        result = await self.start(env, guard, queued=queued)
                    self.assertEqual(len(observed), 2 if queued else 1)
                    self.assertTrue(all(math.isinf(budget.deadline) for budget in observed))
                    if queued:
                        self.assertTrue(result['queued'])
                        item, = env.queue.queue('thread')
                        self.assertEqual((item.execution_id, item.message), ('same-execution', 'one original'))
                        env.runtime.start_thread_turn_now.assert_not_awaited()
                    else:
                        self.assertEqual(result['turn']['id'], 'new-turn')
                        env.runtime.start_thread_turn_now.assert_awaited_once()
                        self.assertEqual(env.queue.queue('thread'), [])

    async def test_positive_and_default_caps_refuse_at_either_actual_guard_without_enqueue_or_start(self):
        for configured, expected in (('3', 3.), ('', 5.)):
            for second_guard in (False, True):
                with self.subTest(configured=configured, second_guard=second_guard):
                    env = self.environment()
                    clock = [100.]
                    observed = []
                    def guard():
                        budget = storage_operation_budget.get()
                        observed.append((budget, budget.remaining()))
                        if len(observed) == (2 if second_guard else 1):
                            clock[0] += 6.
                        check_operation_budget()
                    with patch.dict(os.environ, {'CODEX_WEB_LOCAL_PREPARATION_TIMEOUT_SECONDS': configured}), \
                            patch('codex_web.storage.state_store.time', SimpleNamespace(monotonic=lambda: clock[0])):
                        with self.assertRaises(TimeoutError):
                            await self.start(env, guard, queued=second_guard)
                    self.assertEqual(len(observed), 2 if second_guard else 1)
                    self.assertTrue(all(remaining == expected for _, remaining in observed))
                    self.assertEqual(env.queue.queue('thread'), [])
                    env.runtime.start_thread_turn_now.assert_not_awaited()

    async def test_disabled_local_cap_retains_existing_parent_budget(self):
        env = self.environment()
        clock = [100.]
        observed = []
        def guard():
            observed.append(storage_operation_budget.get())
            clock[0] += 6.
            check_operation_budget()
        with patch.dict(os.environ, {'CODEX_WEB_LOCAL_PREPARATION_TIMEOUT_SECONDS': '0'}), \
                patch('codex_web.storage.state_store.time', SimpleNamespace(monotonic=lambda: clock[0])):
            parent = StorageOperationBudget(4.)
            token = storage_operation_budget.set(parent)
            try:
                with self.assertRaises(TimeoutError):
                    await self.start(env, guard)
            finally:
                storage_operation_budget.reset(token)
        self.assertEqual(observed[0].deadline, parent.deadline)
        self.assertFalse(parent.cancelled.is_set())
        env.runtime.start_thread_turn_now.assert_not_awaited()
        self.assertEqual(env.queue.queue('thread'), [])

    async def test_cancelled_preenqueue_waiter_joins_same_guard_and_never_enqueues(self):
        env = self.environment()
        entered = asyncio.Event()
        release = threading.Event()
        finished = threading.Event()
        loop = asyncio.get_running_loop()
        calls = []
        def guard():
            calls.append(storage_operation_budget.get())
            if len(calls) == 2:
                loop.call_soon_threadsafe(entered.set)
                try:
                    if not release.wait(30):  # fixture deadlock guard only
                        raise AssertionError('test guard was not released')
                    check_operation_budget()
                finally:
                    finished.set()
        with patch.dict(os.environ, {'CODEX_WEB_LOCAL_PREPARATION_TIMEOUT_SECONDS': '-1'}):
            task = asyncio.create_task(self.start(env, guard, queued=True))
            try:
                await asyncio.wait_for(entered.wait(), 30)  # fixture deadlock guard
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertFalse(finished.is_set())
                self.assertEqual(env.queue.queue('thread'), [])
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(finished.is_set())
                self.assertTrue(calls[-1].cancelled.is_set())
                self.assertEqual(len(calls), 2)
                env.runtime.start_thread_turn_now.assert_not_awaited()
                self.assertEqual(env.queue.queue('thread'), [])
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except BaseException:
                        pass


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(TurnAdmissionTimeoutPolicyTests)
