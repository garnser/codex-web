"""Actual bound continuation owns blocking validation without stopping the loop."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

from codex_web.models import Project
from tests import test_turn_execution as fixtures


class NativeStartValidationOffloopTests(unittest.IsolatedAsyncioTestCase):
    def prepare(self):
        host, binding, sessions, service = fixtures.TurnExecutionStartTests._service(
            self, bootstrap_thread_id="t1")
        session = sessions.session
        # Match the production session's existing serial executor and work set;
        # this is private fixture custody, not another production executor.
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="private-validation")
        session._supervision_executor = executor
        session._executor_work = set()
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        entered = asyncio.Event()
        release = threading.Event()
        order = []
        original_validate = session.validate_current
        first = [True]

        def validate():
            if first[0]:
                first[0] = False
                loop.call_soon_threadsafe(entered.set)
                # Fail OLD at the actual synchronous boundary before blocking,
                # so its event loop cannot hang waiting on the test's gate.
                self.assertNotEqual(threading.get_ident(), loop_thread,
                    "Bound native validation ran blocking work on the event loop")
                release.wait()
                order.append("validation_returned")
            return original_validate()

        session.validate_current = validate
        original_request = session.request

        async def request(method, params=None):
            if method == "turn/start":
                order.append("provider_start")
            return await original_request(method, params)

        session.request = request
        return host, binding, sessions, service, executor, entered, release, order

    @staticmethod
    async def start(service):
        return await service.start_thread_turn_now("t1",
            project=Project(id="p1", name="Project", path="/workspace/project"),
            message="Continue existing bound work", sandbox="workspace-write",
            approval_policy="on-request", source="web")

    async def test_actual_continuation_validation_allows_unrelated_watchdog_progress(self):
        host, binding, sessions, service, executor, entered, release, order = self.prepare()

        async def unrelated_watchdog():
            await entered.wait()
            order.append("unrelated_watchdog_tick")
            release.set()

        watchdog = asyncio.create_task(unrelated_watchdog())
        task = asyncio.create_task(self.start(service))
        try:
            await entered.wait()
            result = await task
            await watchdog
            self.assertIn("unrelated_watchdog_tick", order)
            self.assertLess(order.index("unrelated_watchdog_tick"), order.index("validation_returned"))
            self.assertLess(order.index("validation_returned"), order.index("provider_start"))
            self.assertEqual(order.count("provider_start"), 1)
            self.assertTrue(result)
            self.assertFalse(binding.calls, "Existing continuation must not prepare another assignment")
            self.assertEqual(sessions.started, [])
            self.assertEqual(sessions.stopped, [])
            self.assertEqual(sessions.session._executor_work, set())
            self.assertTrue(service.thread_is_active("t1"))
        finally:
            release.set()
            await asyncio.gather(task, watchdog, return_exceptions=True)
            executor.shutdown(wait=True)

    async def test_cancelled_continuation_joins_validation_without_late_start(self):
        _, binding, sessions, service, executor, entered, release, order = self.prepare()
        task = asyncio.create_task(self.start(service))
        try:
            await entered.wait()
            # OLD has already failed the boundary assertion here. Retrieve its
            # real error instead of obscuring it with a cancellation assertion.
            if task.done():
                await task
            self.assertEqual(len(sessions.session._executor_work), 1)
            self.assertTrue(service._turn_start_locks["t1"].locked())
            task.cancel()
            # Drain cancellation callbacks while actual worker completion is
            # forbidden by the gate; this asserts ownership, not a time SLA.
            for _ in range(20):
                await asyncio.sleep(0)
            order.append("unrelated_loop_progress_during_cancellation")
            self.assertFalse(task.done(), "Cancellation abandoned the validation worker")
            self.assertEqual(len(sessions.session._executor_work), 1)
            self.assertTrue(service._turn_start_locks["t1"].locked())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIn("validation_returned", order)
            self.assertFalse(any(method == "turn/start" for method, _ in sessions.session.requests))
            self.assertFalse(binding.calls)
            self.assertEqual(sessions.started, [])
            self.assertEqual(sessions.stopped, [])
            self.assertEqual(sessions.session._executor_work, set())
            self.assertFalse(service.thread_is_active("t1"))
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            executor.shutdown(wait=True)
