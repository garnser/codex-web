"""Real canonical heartbeat contention must settle before its observer closes."""
import asyncio
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from codex_web.services import agent_process_session
from codex_web.storage import state_store
from codex_web.storage.postgres_state import PostgresStateStore
from codex_web.storage.state_store import (
    StorageLockUnavailable, StorageOperationBudget, operation_lock, storage_operation_budget,
)
from tests import test_runtime_supervision_custody as fixture


class ObservedLock:
    """Forward the real repository mutex; expose acquisition, never grant it."""
    def __init__(self, lock):
        self.lock = lock
        self.entered = threading.Event()
        self.budgets = []

    def acquire(self, *args, **kwargs):
        budget = storage_operation_budget.get()
        if budget is not None and not self.entered.is_set():
            self.budgets.append(budget)
            self.entered.set()
        return self.lock.acquire(*args, **kwargs)

    def release(self):
        return self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


class NativeRenewalDeadlineOwnershipTests(unittest.IsolatedAsyncioTestCase):
    _create_assignment = fixture.SupervisionCustodyTests._create_assignment
    _session = fixture.SupervisionCustodyTests._session
    asyncSetUp = fixture.SupervisionCustodyTests.asyncSetUp
    asyncTearDown = fixture.SupervisionCustodyTests.asyncTearDown
    session = fixture.SupervisionCustodyTests.session

    async def test_contended_heartbeat_settles_without_retiring_current_claim(self):
        session, _ = await self.session()
        before = self.worker_service.store.assignment(session.assignment_id)
        original_lock = self.worker_service.store._lock
        lock = ObservedLock(original_lock)
        session.last_heartbeat_monotonic = None
        heartbeat = self.worker_service.heartbeat
        renewal = self.worker_service.renew
        self.worker_service.heartbeat = Mock(wraps=heartbeat)
        self.worker_service.renew = Mock(wraps=renewal)
        self.worker_service.store._lock = lock
        original_lock.acquire()
        try:
            # This is the real current-authority -> heartbeat -> canonical
            # store.update_worker path. Only another owner holds its mutex.
            try:
                current = await session._supervise('renewal', session._heartbeat_and_renew,
                    before, seconds=1.0)
            except TimeoutError:
                current = None
            self.assertTrue(lock.entered.is_set(), 'heartbeat never reached canonical mutex')
            self.assertIsNotNone(current,
                'known unstarted heartbeat contention must settle and revalidate the same live claim')
            self.assertEqual(current, before)
            self.assertEqual(self.worker_service.store.assignment(before.id), before)
            self.assertEqual(self.worker_service.heartbeat.call_count, 1)
            self.worker_service.renew.assert_not_called()
            self.assertEqual(session._supervision_phase, 'renewal')
            self.assertFalse(session._executor_work, 'settled refusal must not leave executor work')
        finally:
            original_lock.release()
            self.worker_service.store._lock = original_lock
            pending = list(session._executor_work)
            if pending:
                await asyncio.wait_for(asyncio.gather(
                    *(asyncio.wrap_future(f) for f in pending), return_exceptions=True), 2)
        # Retention itself did not renew or replay. Only this next ordinary
        # owner invocation may heartbeat and actually renew the original lease.
        current = await session._supervise('renewal', session._heartbeat_and_renew,
            before, seconds=1.0)
        self.assertEqual(current.id, before.id)
        self.assertEqual(current.fence, before.fence)
        self.assertEqual(current.lease.lease_token, before.lease.lease_token)
        self.assertIsNotNone(current.lease.renewed_at)
        self.assertEqual(self.worker_service.heartbeat.call_count, 2)
        self.assertEqual(self.worker_service.renew.call_count, 1)

    async def test_renewal_work_budget_precedes_unchanged_observer_bound(self):
        session, _ = await self.session()
        work_bounds, observer_bounds = [], []
        def budget(seconds):
            work_bounds.append(seconds)
            return StorageOperationBudget(seconds)
        async def observe(awaitable, seconds):
            observer_bounds.append(seconds)
            return await asyncio.wait_for(awaitable, seconds)
        # Capture arguments while retaining the real budget, clock, executor
        # and asyncio waiter. No assertion depends on runner speed.
        controlled_asyncio = SimpleNamespace(**{**vars(asyncio), 'wait_for': observe})
        with patch.object(agent_process_session, 'StorageOperationBudget', budget), \
                patch.object(agent_process_session, 'asyncio', controlled_asyncio):
            for phase, seconds in (('renewal', 10.0), ('renewal', 1.0),
                    ('validation', 1.0), ('renewal_authority', 1.0)):
                await session._supervise(phase, session._validate_current_authority,
                    seconds=seconds)
        self.assertEqual(work_bounds, [9.0, .75, 1.0, 1.0])
        self.assertEqual(observer_bounds, [10.0, 1.0, 1.0, 1.0])


class LocalLockDeadlineClassificationTests(unittest.TestCase):
    def test_only_unacquired_expiry_is_a_settled_refusal(self):
        lock = threading.Lock()
        lock.acquire()
        token = storage_operation_budget.set(StorageOperationBudget(.025))
        entered = []
        try:
            with self.assertRaises(StorageLockUnavailable):
                with operation_lock(lock):
                    entered.append(True)
            self.assertEqual(entered, [])
            self.assertTrue(lock.locked(), 'the waiting owner cannot release another owner mutex')
        finally:
            storage_operation_budget.reset(token)
            lock.release()

    def test_acquired_body_and_explicit_cancellation_keep_original_error(self):
        lock = threading.Lock()
        for error in (TimeoutError('updater deadline'), RuntimeError('updater failure')):
            with self.subTest(kind=type(error).__name__):
                token = storage_operation_budget.set(StorageOperationBudget(1))
                try:
                    with self.assertRaises(type(error)) as caught:
                        with operation_lock(lock):
                            raise error
                    self.assertIs(caught.exception, error)
                    self.assertFalse(lock.locked())
                finally:
                    storage_operation_budget.reset(token)
        budget = StorageOperationBudget(1)
        budget.cancelled.set()
        token = storage_operation_budget.set(budget)
        try:
            with self.assertRaises(TimeoutError) as caught:
                with operation_lock(lock):
                    self.fail('cancelled owner entered')
            self.assertNotIsInstance(caught.exception, StorageLockUnavailable)
        finally:
            storage_operation_budget.reset(token)

    def test_expiry_after_acquisition_is_not_a_known_unstarted_mutation(self):
        lock = threading.Lock()
        budget = StorageOperationBudget(1)
        token = storage_operation_budget.set(budget)
        try:
            with self.assertRaises(TimeoutError) as caught:
                with operation_lock(lock):
                    budget.deadline = time.monotonic() - 1
            self.assertNotIsInstance(caught.exception, StorageLockUnavailable)
            self.assertFalse(lock.locked())
        finally:
            storage_operation_budget.reset(token)


class CancelledStatement(Exception):
    sqlstate = '57014'


class PostgresAcquisitionDeadlineTests(unittest.TestCase):
    def test_each_advisory_lock_uses_current_remaining_transaction_budget(self):
        clock = SimpleNamespace(now=0.0)
        configured, acquisitions = [], []
        def execute(statement, parameters):
            if "set_config('statement_timeout'" in statement:
                configured.append(int(parameters[0]))
            elif 'pg_advisory_xact_lock' in statement:
                acquisitions.append((parameters[0], configured[-1]))
                if len(acquisitions) == 1:
                    clock.now += 7.0
            else:
                self.fail('unexpected acquisition statement')
        cursor = Mock(execute=execute)
        with patch.object(state_store, 'time', SimpleNamespace(monotonic=lambda: clock.now)):
            token = storage_operation_budget.set(StorageOperationBudget(10))
            try:
                PostgresStateStore._lock_many(cursor, ('second', 'first'))
            finally:
                storage_operation_budget.reset(token)
        self.assertEqual(configured, [10000, 3000])
        self.assertEqual(acquisitions, [('first', 10000), ('second', 3000)])

    def test_expired_budget_never_starts_another_acquisition(self):
        cursor = Mock()
        budget = StorageOperationBudget(1)
        budget.deadline = time.monotonic() - 1
        token = storage_operation_budget.set(budget)
        try:
            with self.assertRaises(TimeoutError) as caught:
                PostgresStateStore._lock(cursor, 'synthetic')
            self.assertNotIsInstance(caught.exception, StorageLockUnavailable)
            cursor.execute.assert_not_called()
        finally:
            storage_operation_budget.reset(token)

    def test_timeout_configuration_cancellation_is_not_an_acquisition_refusal(self):
        original = CancelledStatement('synthetic set_config cancellation')
        cursor = Mock()
        cursor.execute.side_effect = original
        token = storage_operation_budget.set(StorageOperationBudget(1))
        try:
            with self.assertRaises(CancelledStatement) as caught:
                PostgresStateStore._lock(cursor, 'synthetic')
            self.assertIs(caught.exception, original)
            self.assertEqual(cursor.execute.call_count, 1)
            self.assertIn("set_config('statement_timeout'", cursor.execute.call_args.args[0])
        finally:
            storage_operation_budget.reset(token)

    def test_only_actual_budgeted_acquisition_cancellation_is_known_unstarted(self):
        original = CancelledStatement('synthetic advisory acquisition cancellation')
        statements = []
        def execute(statement, parameters):
            statements.append(statement)
            if 'pg_advisory_xact_lock' in statement:
                raise original
        cursor = Mock(execute=execute)
        token = storage_operation_budget.set(StorageOperationBudget(1))
        try:
            with self.assertRaises(StorageLockUnavailable) as caught:
                PostgresStateStore._lock(cursor, 'synthetic')
            self.assertIs(caught.exception.__cause__, original)
            self.assertEqual(len(statements), 2)
        finally:
            storage_operation_budget.reset(token)

    def test_unbudgeted_lock_keeps_existing_single_statement_and_error(self):
        original = CancelledStatement('synthetic unbudgeted cancellation')
        cursor = Mock()
        cursor.execute.side_effect = original
        with self.assertRaises(CancelledStatement) as caught:
            PostgresStateStore._lock(cursor, 'synthetic')
        self.assertIs(caught.exception, original)
        self.assertEqual(cursor.execute.call_count, 1)
        self.assertIn('pg_advisory_xact_lock', cursor.execute.call_args.args[0])

