"""Fixture cleanup must join its own handlers without cancelling other tasks."""
import asyncio
from types import SimpleNamespace
import unittest

from tests.project_coordination_fixture_lifecycle import stop_owned_broker


class CoordinationFixtureLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_joins_captured_handlers_and_leaves_unrelated_task(self):
        release = asyncio.Event()
        unrelated_release = asyncio.Event()
        completed = []
        async def handle():
            await release.wait()
            await asyncio.sleep(0)
            completed.append(True)
        handler = asyncio.create_task(handle())
        unrelated = asyncio.create_task(unrelated_release.wait())
        async def stop():
            release.set()
        try:
            await stop_owned_broker(SimpleNamespace(_handler_tasks={handler}, stop=stop))
            self.assertEqual(completed, [True])
            self.assertTrue(handler.done())
            self.assertFalse(unrelated.done())
        finally:
            unrelated_release.set()
            await unrelated

    async def test_cleanup_exhaustion_refuses_without_cancelling_handler(self):
        release = asyncio.Event()
        handler = asyncio.create_task(release.wait())
        async def stop():
            return None
        try:
            with self.assertRaisesRegex(AssertionError, 'did not settle'):
                await stop_owned_broker(SimpleNamespace(_handler_tasks={handler}, stop=stop), timeout=0)
            self.assertFalse(handler.done())
            self.assertFalse(handler.cancelled())
        finally:
            release.set()
            await handler

    async def test_cleanup_surfaces_owned_handler_failure_and_cancellation(self):
        async def fail(error):
            raise error
        async def stop():
            return None
        for error in (RuntimeError('fixture failure'), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                handler = asyncio.create_task(fail(error))
                with self.assertRaisesRegex(AssertionError, type(error).__name__):
                    await stop_owned_broker(SimpleNamespace(_handler_tasks={handler}, stop=stop))
                self.assertTrue(handler.done())
