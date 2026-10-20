from __future__ import annotations

import asyncio
import io
import json
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from codex_web.runtime.codex import CodexRuntime
from tests.test_codex_runtime import _Host


class Process:
    def __init__(self, writer=None, *, ignore_terminate=False):
        self.pid = 12345
        self.stdin = writer or io.StringIO()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.exit = threading.Event()
        self.returncode = None
        self.ignore_terminate = ignore_terminate
        self.terminate_count = self.kill_count = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_count += 1
        if not self.ignore_terminate:
            self.returncode = -15
            self.exit.set()

    def kill(self):
        self.kill_count += 1
        self.returncode = -9
        self.exit.set()

    def wait(self):
        if not self.exit.wait(5):
            raise RuntimeError('fixture process exit was never signalled')
        return self.returncode


class GuardedNativeWriterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = CodexRuntime(_Host(), restart_on_timeout=False)
        self.process = Process()
        self.runtime.proc = self.process
        self.runtime.ready.set()

    async def entered(self, event):
        self.assertTrue(await asyncio.to_thread(event.wait, 1), 'worker operation did not enter')

    async def test_guard_and_write_share_one_worker_while_loop_remains_available(self):
        entered, release = threading.Event(), threading.Event()
        threads = []
        writer = Mock(wraps=self.process.stdin)
        original_write = writer.write
        def write(value):
            threads.append(threading.get_ident())
            return original_write(value)
        writer.write = write
        self.process.stdin = writer
        def guard():
            threads.append(threading.get_ident())
            entered.set()
            if not release.wait(3):
                raise RuntimeError('fixture guard never released')
        task = asyncio.create_task(self.runtime._send({'id': 1}, start_guard=guard))
        try:
            await self.entered(entered)
            self.assertFalse(task.done())
            observed = asyncio.Event()
            asyncio.get_running_loop().call_soon(observed.set)
            await asyncio.wait_for(observed.wait(), .5)
        finally:
            release.set()
        await task
        self.assertEqual(len(threads), 2)
        self.assertEqual(threads[0], threads[1])
        self.assertNotEqual(threads[0], threading.get_ident())
        self.assertEqual(self.process.stdin.getvalue(), '{"id": 1}\n')

    async def test_repeated_cancel_keeps_writer_lock_until_exactly_one_write_settles(self):
        entered, release = threading.Event(), threading.Event()
        def guard():
            entered.set()
            release.wait(3)
        task = asyncio.create_task(self.runtime._send({'id': 1}, start_guard=guard))
        try:
            await self.entered(entered)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertTrue(self.runtime.write_lock.locked())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.runtime.write_lock.locked())
        self.assertEqual(self.process.stdin.getvalue().count('\n'), 1)

    async def test_stop_during_guard_never_writes_retired_or_replacement_stdin(self):
        entered, release = threading.Event(), threading.Event()
        def guard():
            entered.set()
            release.wait(3)
        task = asyncio.create_task(self.runtime._send({'id': 1}, start_guard=guard))
        await self.entered(entered)
        stop = asyncio.create_task(self.runtime.stop())
        try:
            await asyncio.sleep(.01)
            self.assertIsNone(self.runtime.proc)
            self.assertFalse(stop.done(), 'stop must join actual guard work')
            replacement = Process()
            # Publication uses the same lock in production start(). Deliberate
            # adversarial substitution additionally proves captured stdin safety.
            self.runtime.proc = replacement
        finally:
            release.set()
        with self.assertRaisesRegex(RuntimeError, 'generation changed'):
            await task
        await stop
        self.assertEqual(self.process.stdin.getvalue(), '')
        self.assertEqual(replacement.stdin.getvalue(), '')
        self.assertEqual(self.process.terminate_count, 1)

    async def test_blocked_pipe_cannot_prevent_termination_and_kill_deadline(self):
        entered = threading.Event()
        process = Process(ignore_terminate=True)
        class Pipe:
            def write(self, payload):
                entered.set()
                if not process.exit.wait(3):
                    raise RuntimeError('kill was blocked behind writer ownership')
                raise BrokenPipeError('controlled terminated pipe')
            def flush(self):
                raise AssertionError('failed write must not flush')
        process.stdin = Pipe()
        self.runtime.proc = process
        task = asyncio.create_task(self.runtime._send({'id': 1}))
        await self.entered(entered)
        real_wait_for = asyncio.wait_for
        async def short_process_wait(awaitable, *, timeout):
            return await real_wait_for(awaitable, timeout=.03 if timeout == 5 else timeout)
        with patch('codex_web.runtime.codex.asyncio.wait_for', side_effect=short_process_wait):
            await self.runtime.stop()
        with self.assertRaises(BrokenPipeError):
            await task
        self.assertEqual(process.terminate_count, 1)
        self.assertEqual(process.kill_count, 1)
        self.assertFalse(self.runtime.write_lock.locked())

    async def test_final_authority_refusal_precedes_actual_write(self):
        def refuse():
            raise PermissionError('current scope revoked')
        with self.assertRaises(PermissionError):
            await self.runtime._send({'id': 1}, start_guard=refuse)
        self.assertEqual(self.process.stdin.getvalue(), '')

    async def test_initialization_and_stop_do_not_deadlock_lifecycle_and_writer(self):
        self.runtime.proc = None
        initializing = asyncio.Event()
        def popen(*args, **kwargs):
            return self.process
        self.runtime._popen = popen
        self.runtime._read_loop = AsyncMock()
        self.runtime._stderr_loop = AsyncMock()
        original_request = self.runtime.request
        async def request(method, params=None, **kwargs):
            if method == 'initialize':
                initializing.set()
            return await original_request(method, params, **kwargs)
        self.runtime.request = request
        self.runtime.ensure_started = AsyncMock()
        start = asyncio.create_task(self.runtime.start())
        await asyncio.wait_for(initializing.wait(), .5)
        stop = asyncio.create_task(self.runtime.stop())
        results = await asyncio.wait_for(asyncio.gather(start, stop, return_exceptions=True), 2)
        self.assertIsInstance(results[0], RuntimeError)
        self.assertIsNone(results[1])
        self.assertIsNone(self.runtime.proc)
        self.assertEqual(self.process.terminate_count, 1)

    async def test_actual_positive_capture_survives_cancellation_while_flush_is_settling(self):
        from tests.test_queued_native_start import QueuedNativeStartTests
        fixture = QueuedNativeStartTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        runtime = fixture.runtime
        runtime._send = CodexRuntime._send.__get__(runtime)
        entered, release = threading.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        written = []
        generation = runtime.generation
        class EchoPipe:
            def write(self, value):
                written.append(value)
                message = json.loads(value)
                async def reply():
                    await runtime._dispatch_message({'id': message['id'],
                        'result': {'turn': {'id': 'real-fixture-response', 'threadId': 'thread'}}},
                        generation=generation)
                loop.call_soon_threadsafe(lambda: asyncio.create_task(reply()))
                return len(value)
            def flush(self):
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('fixture flush not released')
        runtime.proc = Process(EchoPipe())
        request = asyncio.create_task(runtime.request('turn/start', {'threadId': 'thread'}, start_capture=fixture.capture))
        try:
            await self.entered(entered)
            for _ in range(100):
                saved = await asyncio.to_thread(fixture.saved)
                if saved.status == 'started':
                    break
                await asyncio.sleep(.005)
            self.assertEqual(saved.status, 'started')
            request.cancel()
            await asyncio.sleep(0)
            request.cancel()
            self.assertFalse(request.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertEqual(fixture.saved().queued_start_receipt.provider_native_turn_id, 'real-fixture-response')
        self.assertEqual(len(written), 1)
        self.assertFalse(runtime.write_lock.locked())

    async def test_partial_write_exception_remains_unknown_and_is_never_retried(self):
        from tests.test_queued_native_start import QueuedNativeStartTests
        fixture = QueuedNativeStartTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        runtime = fixture.runtime
        runtime._send = CodexRuntime._send.__get__(runtime)
        written = []
        class PartialPipe:
            def write(self, value):
                written.append(value[:10])
                raise BrokenPipeError('controlled partial write')
            def flush(self):
                raise AssertionError('partial write cannot flush')
        runtime.proc = Process(PartialPipe())
        with self.assertRaises(BrokenPipeError):
            await runtime.request('turn/start', {'threadId': 'thread'}, start_capture=fixture.capture)
        retained = fixture.saved()
        self.assertEqual(retained.status, 'reconciliation_required')
        self.assertIsNotNone(retained.queued_start_correlation)
        self.assertIsNone(retained.queued_start_receipt)
        self.assertEqual(len(written), 1)
