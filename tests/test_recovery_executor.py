import asyncio
import contextvars
import threading
import unittest
from unittest.mock import patch
from anyio import CancelScope

from codex_web.services.recovery_executor import RecoveryExecutor, RecoveryExecutorBusy, RecoveryExecutorClosed


class RecoveryExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_level_cancel_joins_failed_worker_without_proxy_fanout_or_loop_errors(self):
        executor = RecoveryExecutor()
        self.addAsyncCleanup(executor.stop)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        scopes, cancellations, errors = [], [], []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        def worker():
            entered.set()
            if not release.wait(5):
                raise AssertionError('level cancellation worker gate not released')
            raise ValueError('terminal worker denial')
        async def observer():
            with CancelScope() as scope:
                scopes.append(scope)
                try:
                    await executor.run('level-cancel', worker)
                except asyncio.CancelledError:
                    cancellations.append(True)
                    raise
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        work = asyncio.create_task(observer())
        stopping = None
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            future = executor._futures['level-cancel']
            # The real future already has this observer's completion callback.
            # Level cancellation must not attach any additional proxies.
            with patch.object(future, 'add_done_callback', wraps=future.add_done_callback) as attached:
                scopes[0].cancel()
                heartbeats = 0
                for _ in range(3):
                    await asyncio.sleep(.01)
                    heartbeats += 1
                self.assertEqual(heartbeats, 3)
                self.assertEqual(attached.call_count, 0)
                self.assertFalse(future.done())
                self.assertFalse(work.done())
                self.assertEqual(executor.status()['inflight_work'], 1)
            stopping = asyncio.create_task(executor.stop())
            await asyncio.sleep(0)
            self.assertFalse(stopping.done())
        finally:
            release.set()
            try:
                await asyncio.gather(work, *([stopping] if stopping is not None else []))
                # Give scheduled completion/shield callbacks a chance to report
                # errors; the handler records, rather than suppresses, failures.
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            finally:
                loop.set_exception_handler(previous_handler)
        self.assertEqual(cancellations, [True])
        self.assertTrue(future.done())
        self.assertIsInstance(future.exception(), ValueError)
        self.assertEqual(str(future.exception()), 'terminal worker denial')
        self.assertEqual(executor.status()['waiting_or_running'], 0)
        self.assertEqual(errors, [])

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
