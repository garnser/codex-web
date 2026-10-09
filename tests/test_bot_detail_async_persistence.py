from __future__ import annotations

import asyncio
import contextvars
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from codex_web.services.bot_delivery import BotDeliveryService
from codex_web.services.bot_details import BotDetailService


_scope = contextvars.ContextVar('detail_test_scope', default=None)


class DetailPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_write_preserves_scope_and_consumer_order_without_blocking_loop(self):
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        observed = []
        sequence = []

        class Repository:
            def get(self, thread):
                return []

            def put(self, thread, items):
                observed.append((threading.get_ident(), _scope.get(), thread, items))
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(2):
                    raise TimeoutError('test storage barrier')
                sequence.append('persisted')

        service = BotDeliveryService(
            details=BotDetailService(detail_repository=Repository()),
            presentation=SimpleNamespace(format_detail_item=lambda item: {'title': 'command', 'text': 'output'}),
        )
        token = _scope.set('workspace-a')
        async def consume():
            await service.record_outbound({'method': 'item/completed', 'params': {'threadId': 't', 'item': {'type': 'commandExecution'}}})
            sequence.append('next-notification')
        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            heartbeat = asyncio.Event()
            loop.call_soon(heartbeat.set)
            await asyncio.wait_for(heartbeat.wait(), 0.5)
            self.assertFalse(task.done())
            self.assertEqual(sequence, [])
            self.assertNotEqual(observed[0][0], threading.get_ident())
            self.assertEqual(observed[0][1:3], ('workspace-a', 't'))
            self.assertEqual(observed[0][3][0].text, 'output')
        finally:
            release.set()
            _scope.reset(token)
            await task
        self.assertEqual(sequence, ['persisted', 'next-notification'])

    async def test_blocked_detail_read_waits_before_delivery_and_preserves_scope(self):
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        observed = []
        def latest(thread):
            observed.append((threading.get_ident(), _scope.get(), thread))
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(2):
                raise TimeoutError('test read barrier')
            return None
        service = BotDeliveryService(details=SimpleNamespace(latest=latest))
        service.send_outbound = AsyncMock(return_value={'sent': True})
        token = _scope.set('workspace-b')
        binding = SimpleNamespace(thread_id='t')
        task = asyncio.create_task(service.send_details(binding))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            self.assertFalse(task.done())
            service.send_outbound.assert_not_awaited()
            self.assertNotEqual(observed[0][0], threading.get_ident())
            self.assertEqual(observed[0][1:], ('workspace-b', 't'))
        finally:
            release.set()
            _scope.reset(token)
            result = await task
        self.assertEqual(result, {'sent': True})
        service.send_outbound.assert_awaited_once_with(binding, 'No command or file details are available for this thread yet.', reply_in_thread=True)

    async def test_write_failure_propagates_without_processing_next_notification(self):
        failure = RuntimeError('durable store unavailable')
        def record(*args):
            raise failure
        service = BotDeliveryService(details=SimpleNamespace(record=record), presentation=SimpleNamespace(format_detail_item=lambda item: {'title': 'file', 'text': 'diff'}))
        with self.assertRaises(RuntimeError) as raised:
            await service.record_outbound({'method': 'item/completed', 'params': {'threadId': 't', 'item': {'type': 'fileChange'}}})
        self.assertIs(raised.exception, failure)

    async def test_detail_read_failure_prevents_outbound_delivery(self):
        failure = RuntimeError('detail read unavailable')
        def latest(thread):
            raise failure
        service = BotDeliveryService(details=SimpleNamespace(latest=latest))
        service.send_outbound = AsyncMock()
        with self.assertRaises(RuntimeError) as raised:
            await service.send_details(SimpleNamespace(thread_id='t'))
        self.assertIs(raised.exception, failure)
        service.send_outbound.assert_not_awaited()

    async def test_cancelled_wait_does_not_process_next_notification_or_retry_write(self):
        entered = asyncio.Event()
        finished = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        calls = []
        sequence = []
        def record(*args):
            calls.append(args)
            loop.call_soon_threadsafe(entered.set)
            release.wait(2)
            loop.call_soon_threadsafe(finished.set)
        service = BotDeliveryService(details=SimpleNamespace(record=record), presentation=SimpleNamespace(format_detail_item=lambda item: {'title': 'file', 'text': 'diff'}))
        async def consume():
            await service.record_outbound({'method': 'item/completed', 'params': {'threadId': 't', 'item': {'type': 'fileChange'}}})
            sequence.append('next')
        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            await asyncio.wait_for(finished.wait(), 3)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sequence, [])


if __name__ == '__main__':
    unittest.main()
