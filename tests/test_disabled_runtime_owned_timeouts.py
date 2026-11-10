"""Disabled elapsed ceilings retain actual storage ownership and cancellation."""
import asyncio
import math
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from codex_web.services.post_turn_readiness import PostTurnReadinessService
from codex_web.services.runtime_policy import RuntimePolicy
from codex_web.storage.home_intent_query import recovery_query_budget
from codex_web.storage.postgres_state import PostgresStateStore
from codex_web.storage.state_store import (
    StorageOperationBudget, check_operation_budget, operation_lock,
    storage_operation_budget,
)


class DisabledRuntimePolicyTests(unittest.TestCase):
    def test_existing_and_local_timeout_knobs_accept_disabled_sentinels(self):
        policy = RuntimePolicy(Path('/unused'))
        knobs = [
            ('CODEX_WEB_CONTINUITY_DISPATCH_TIMEOUT_SECONDS', policy.continuity_dispatch_timeout, 120),
            ('CODEX_WEB_NATIVE_RECOVERY_CYCLE_TIMEOUT_SECONDS', policy.native_recovery_cycle_timeout, 300),
            ('CODEX_WEB_LOCAL_PREPARATION_TIMEOUT_SECONDS', lambda: policy.local_preparation_timeout(17), 17),
            ('CODEX_WEB_NATIVE_RPC_TIMEOUT_SECONDS', lambda: policy.native_rpc_timeout(19), 19),
            ('CODEX_WEB_WORKER_WALL_TIMEOUT_SECONDS', lambda: policy.worker_wall_timeout(23), 23),
            ('CODEX_WEB_PROCEDURE_OPERATION_TIMEOUT_SECONDS', lambda: policy.procedure_operation_timeout(29), 29),
            ('CODEX_WEB_ACTION_TIMEOUT_SECONDS', lambda: policy.action_timeout(31), 31),
        ]
        for name, getter, default in knobs:
            for raw, expected in [('0', 0), ('-1', 0), ('7.25', 7.25),
                                  ('', default), ('invalid', default), ('nan', default), ('-2', default)]:
                with self.subTest(name=name, raw=raw), patch.dict(os.environ, {name: raw}, clear=True):
                    self.assertEqual(getter(), expected)

    def test_disabled_budget_has_no_elapsed_cutoff_but_remains_cancellable(self):
        for duration in (None, 0, -1):
            with self.subTest(duration=duration):
                budget = StorageOperationBudget(duration)
                token = storage_operation_budget.set(budget)
                try:
                    with patch('codex_web.storage.state_store.time.monotonic', return_value=10**20):
                        self.assertTrue(math.isinf(budget.remaining()))
                        with operation_lock(threading.Lock()):
                            check_operation_budget()
                        self.assertEqual(PostgresStateStore._statement_timeout_ms(budget), '0')
                    budget.cancelled.set()
                    with self.assertRaises(TimeoutError):
                        check_operation_budget()
                    with self.assertRaises(TimeoutError):
                        PostgresStateStore._statement_timeout_ms(budget)
                finally:
                    storage_operation_budget.reset(token)

    def test_positive_budget_still_expires_and_postgres_gets_finite_milliseconds(self):
        with patch('codex_web.storage.state_store.time.monotonic', return_value=100):
            budget = StorageOperationBudget(9)
        with patch('codex_web.storage.state_store.time.monotonic', return_value=103):
            self.assertEqual(budget.remaining(), 6)
            self.assertEqual(PostgresStateStore._statement_timeout_ms(budget), '6000')
        with patch('codex_web.storage.state_store.time.monotonic', return_value=110):
            with self.assertRaises(TimeoutError):
                budget.remaining()

    def test_local_query_off_keeps_enabled_parent_and_cancellation(self):
        for parent in (StorageOperationBudget(90), StorageOperationBudget(0)):
            token = storage_operation_budget.set(parent)
            try:
                with patch.dict(os.environ, {'CODEX_WEB_LOCAL_PREPARATION_TIMEOUT_SECONDS': '-1'}):
                    with recovery_query_budget():
                        child = storage_operation_budget.get()
                        self.assertIsNot(child.cancelled, parent.cancelled)
                        if math.isfinite(parent.deadline):
                            self.assertLessEqual(child.remaining(), 90)
                        else:
                            self.assertTrue(math.isinf(child.remaining()))
                        parent.cancelled.set()
                        with self.assertRaises(TimeoutError):
                            check_operation_budget()
                self.assertIs(storage_operation_budget.get(), parent)
            finally:
                storage_operation_budget.reset(token)


class DisabledOwnedOperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_owner_waits_for_real_completion_without_timer(self):
        real_wait = asyncio.wait
        for duration in (None, 0, -1):
            entered, release = asyncio.Event(), asyncio.Event()
            seen = []
            async def operation():
                seen.append(storage_operation_budget.get())
                entered.set()
                await release.wait()
                check_operation_budget()
                return 'actual-result'
            with patch('codex_web.services.post_turn_readiness.asyncio.wait', wraps=real_wait) as wait:
                task = asyncio.create_task(PostTurnReadinessService._run_owned(
                    operation, seconds=duration, name='disabled-owned-fixture'))
                try:
                    await entered.wait()
                    self.assertTrue(math.isinf(seen[0].deadline))
                    self.assertIsNone(wait.call_args.kwargs['timeout'])
                    self.assertFalse(task.done())
                    release.set()
                    self.assertEqual(await task, 'actual-result')
                finally:
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_disabled_child_cannot_remove_positive_parent_ceiling(self):
        parent = StorageOperationBudget(60)
        token = storage_operation_budget.set(parent)
        try:
            async def operation():
                child = storage_operation_budget.get()
                self.assertLessEqual(child.deadline, parent.deadline)
                self.assertTrue(math.isfinite(child.deadline))
                self.assertIsNot(child.cancelled, parent.cancelled)
                return 'bounded-by-parent'
            self.assertEqual(await PostTurnReadinessService._run_owned(
                operation, seconds=0, name='parent-bounded-fixture'), 'bounded-by-parent')
        finally:
            storage_operation_budget.reset(token)

    async def test_child_guard_expiry_keeps_parent_live_for_real_bootstrap_cleanup(self):
        from tests.thread_bootstrap_composition_harness import build_real_thread_create
        from codex_web.services.threads import ThreadBootstrapPrestartRefusal
        fixture = build_real_thread_create(self)
        parent = StorageOperationBudget(120)
        children = []
        token = storage_operation_budget.set(parent)
        def guard():
            children.append(storage_operation_budget.get())
            if len(children) == 2:
                # Deterministically expire only this positive local guard;
                # no response-time or machine-speed assertion is involved.
                children[-1].deadline = 0
                check_operation_budget()
        try:
            with self.assertRaises(ThreadBootstrapPrestartRefusal):
                await fixture.thread_service.create(project_id=fixture.project.id,
                    repository_resource_id=fixture.repository.id,
                    bootstrap_token='e' * 32, admission_guard=guard)
            self.assertEqual(len(children), 2)
            self.assertTrue(children[-1].cancelled.is_set())
            self.assertFalse(parent.cancelled.is_set())
            parent.remaining()
            assignments = [row for row in fixture.workers.store.load().assignments
                if row.execution_id == 'thread-bootstrap-' + 'e' * 32]
            self.assertEqual(len(assignments), 1)
            retired = assignments[0]
            self.assertEqual(str(retired.status), 'cancelled')
            self.assertEqual(retired.fence, 1)
            self.assertIsNone(retired.lease)
            self.assertEqual(retired.failure.details['phase'], 'before_worker_start')
            self.assertFalse(retired.failure.details['worker_start_called'])
            self.assertFalse(retired.failure.details['provider_start_called'])
            self.assertEqual(fixture.started_assignments, [])
            self.assertEqual(fixture.process_session.requests, [])
        finally:
            storage_operation_budget.reset(token)

    async def test_parent_cancellation_reaches_child_signal_and_refuses_sqlite_write(self):
        import tempfile
        from codex_web.storage.sqlite_state import SQLiteStateStore
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / 'state.sqlite3')
            store.update('original', lambda _: {'value': 'retained'}, default={})
            parent = StorageOperationBudget(0)
            child = StorageOperationBudget(-1, parent=parent)
            token = storage_operation_budget.set(child)
            try:
                self.assertIsNot(child.cancelled, parent.cancelled)
                parent.cancelled.set()
                self.assertTrue(child.cancelled.is_set())
                with self.assertRaises(TimeoutError):
                    store.update('original', lambda _: {'value': 'forbidden'}, default={})
            finally:
                storage_operation_budget.reset(token)
            self.assertEqual(store.get('original'), {'value': 'retained'})

    async def test_cancellation_joins_real_thread_and_refuses_its_later_write(self):
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release, finished = threading.Event(), threading.Event()
        budgets, writes = [], []
        def writer():
            budgets.append(storage_operation_budget.get())
            loop.call_soon_threadsafe(entered.set)
            try:
                release.wait()
                check_operation_budget()
                writes.append('forbidden-late-write')
            finally:
                finished.set()
        async def operation():
            return await asyncio.to_thread(writer)
        task = asyncio.create_task(PostTurnReadinessService._run_owned(
            operation, seconds=-1, name='cancelled-disabled-writer'))
        try:
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)  # Deliver cancellation; no wall-clock timing claim.
            self.assertTrue(budgets[0].cancelled.is_set())
            self.assertFalse(task.done())
            self.assertFalse(finished.is_set())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(finished.is_set())
            self.assertEqual(writes, [])
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
