import asyncio
import contextvars
import threading
import unittest

from codex_web.services.recovery_executor import RecoveryExecutor, RecoveryExecutorBusy, RecoveryExecutorClosed


class RecoveryExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_exclusive_submission_refuses_before_waiting_for_active_writer(self):
        executor = RecoveryExecutor()
        entered, release, rejected_ran = (threading.Event() for _ in range(3))
        self.addAsyncCleanup(executor.stop)
        self.addCleanup(release.set)
        def writer():
            entered.set()
            if not release.wait(5):
                raise AssertionError('writer gate not released')
        work = asyncio.create_task(executor.run('manual', writer))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            with self.assertRaises(RecoveryExecutorBusy):
                await executor.run('reconcile', rejected_ran.set, exclusive=True)
            self.assertFalse(work.done())
            self.assertFalse(rejected_ran.is_set())
            self.assertEqual(executor.status()['waiting_or_running'], 1)
        finally:
            release.set()
            await work
        self.assertEqual(await executor.run('reconcile', lambda: 'admitted', exclusive=True), 'admitted')

    async def test_exclusive_reservation_survives_cancel_and_same_key_observer_until_join(self):
        executor = RecoveryExecutor()
        entered, release = threading.Event(), threading.Event()
        self.addAsyncCleanup(executor.stop)
        self.addCleanup(release.set)
        calls = []
        def writer():
            calls.append(True)
            entered.set()
            if not release.wait(5):
                raise AssertionError('exclusive gate not released')
        first = asyncio.create_task(executor.run('reconcile', writer, exclusive=True))
        second = None
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            second = asyncio.create_task(executor.run('reconcile', writer, exclusive=True))
            await asyncio.sleep(0)
            first.cancel()
            await asyncio.sleep(0)
            self.assertFalse(first.done())
            with self.assertRaises(RecoveryExecutorBusy):
                await executor.run('manual', lambda: self.fail('late writer started'))
            with self.assertRaises(RecoveryExecutorBusy):
                await executor.run('other-reconcile', writer, exclusive=True)
            self.assertEqual(executor.status()['inflight_work'], 1)
        finally:
            release.set()
            results = await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        self.assertIsInstance(results[0], asyncio.CancelledError)
        self.assertEqual(calls, [True])
        self.assertEqual(executor.status()['waiting_or_running'], 0)
        self.assertEqual(await executor.run('manual', lambda: 'joined'), 'joined')

    async def test_failed_exclusive_future_releases_reservation_without_closing_admission(self):
        executor = RecoveryExecutor()
        self.addAsyncCleanup(executor.stop)
        def fail():
            raise ValueError('exclusive failure')
        with self.assertRaisesRegex(ValueError, 'exclusive failure'):
            await executor.run('reconcile', fail, exclusive=True)
        self.assertEqual(executor.status()['waiting_or_running'], 0)
        self.assertEqual(await executor.run('manual', lambda: 'still open'), 'still open')

    async def test_cancelled_duplicate_observers_drain_one_actual_future(self):
        executor = RecoveryExecutor()
        entered, release, exited = (threading.Event() for _ in range(3))
        self.addAsyncCleanup(executor.stop)
        self.addCleanup(release.set)
        calls = []
        def work():
            calls.append(True)
            entered.set()
            try:
                release.wait(5)
            finally:
                exited.set()
        first = asyncio.create_task(executor.run('firing-a', work))
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        second = asyncio.create_task(executor.run('firing-a', work))
        await asyncio.sleep(0)
        first.cancel()
        second.cancel()
        await asyncio.sleep(0)
        self.assertFalse(first.done())
        self.assertFalse(second.done())
        self.assertEqual(executor.status()['inflight_work'], 1)
        release.set()
        for task in (first, second):
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(calls), 1)
        self.assertTrue(exited.is_set())
        await executor.stop()

    async def test_bounded_waiter_and_shutdown_keep_real_work_owned(self):
        executor = RecoveryExecutor()
        entered, release = threading.Event(), threading.Event()
        self.addAsyncCleanup(executor.stop)
        self.addCleanup(release.set)
        def work():
            entered.set()
            release.wait(5)
        first = asyncio.create_task(executor.run('a', work))
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        queued_ran = threading.Event()
        second = asyncio.create_task(executor.run('b', queued_ran.set))
        await asyncio.sleep(0)
        with self.assertRaises(RecoveryExecutorBusy):
            await executor.run('c', lambda: None)
        stopping = asyncio.create_task(executor.stop())
        await asyncio.sleep(0)
        second.cancel()
        stopping.cancel()
        stopping.cancel()
        await asyncio.sleep(0)
        self.assertFalse(stopping.done())
        release.set()
        await first
        with self.assertRaises(asyncio.CancelledError):
            await second
        with self.assertRaises(asyncio.CancelledError):
            await stopping
        self.assertEqual(executor.status()['inflight_work'], 0)
        self.assertTrue(queued_ran.is_set(), 'already accepted work was discarded')
        with self.assertRaises(RecoveryExecutorClosed):
            await executor.run('after-close', lambda: None)

    async def test_actor_context_and_failed_work_do_not_leak_pool_custody(self):
        executor = RecoveryExecutor()
        self.addAsyncCleanup(executor.stop)
        actor = contextvars.ContextVar('recovery-test-actor')
        token = actor.set('scoped-service')
        try:
            self.assertEqual(await executor.run('a', actor.get), 'scoped-service')
            def fail():
                raise ValueError('controlled worker failure')
            with self.assertRaisesRegex(ValueError, 'controlled worker failure'):
                await executor.run('b', fail)
            self.assertEqual(await executor.run('c', lambda: 3), 3)
        finally:
            actor.reset(token)
        with self.assertRaises(LookupError):
            await executor.run('no-actor', actor.get)
        await executor.stop()
