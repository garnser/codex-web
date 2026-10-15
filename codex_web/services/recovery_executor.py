"""Bounded in-memory ownership of scheduled recovery's actual worker future.

Durable firing admission belongs to RecoveryState, not this executor.
"""
import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor


class RecoveryExecutorBusy(RuntimeError):
    pass


class RecoveryExecutorClosed(RuntimeError):
    pass


class RecoveryExecutor:
    def __init__(self):
        self._serial = asyncio.Lock()
        self._pool = None
        self._future = None
        self._key = None
        self._waiting = 0
        self._closed = False

    @staticmethod
    async def _settle(future):
        cancelled = False
        while not future.done():
            try:
                await asyncio.shield(asyncio.wrap_future(future))
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        # Retrieve a terminal exception even when the observing task was
        # cancelled. Neither a cancelled wrapper nor a lost observer stops work.
        error = future.exception()
        if cancelled:
            raise asyncio.CancelledError
        if error is not None:
            raise error
        return future.result()

    async def run(self, key, operation):
        if self._closed:
            raise RecoveryExecutorClosed('scheduled recovery executor is closing')
        if self._key == key and self._future is not None and not self._future.done():
            return await self._settle(self._future)
        # One running operation and one waiting operation; no unbounded pool
        # submission queue. The two canonical recovery schedules can serialize.
        if self._waiting >= 2:
            raise RecoveryExecutorBusy('scheduled recovery executor is at capacity')
        self._waiting += 1
        try:
            async with self._serial:
                if self._closed:
                    raise RecoveryExecutorClosed('scheduled recovery executor is closing')
                if self._pool is None:
                    self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='recovery-scheduled')
                context = contextvars.copy_context()
                self._key = key
                self._future = self._pool.submit(context.run, operation)
                try:
                    return await self._settle(self._future)
                finally:
                    self._key = None
        finally:
            self._waiting -= 1

    def close_admission(self):
        self._closed = True

    async def stop(self):
        self.close_admission()
        try:
            if self._future is not None:
                try:
                    await self._settle(self._future)
                except Exception:
                    # The operation's caller owns its recorded failure. A
                    # completed failure must not abort application shutdown.
                    pass
        finally:
            if self._pool is not None:
                self._pool.shutdown(wait=True, cancel_futures=False)
                self._pool = None

    def status(self):
        return {'accepting': not self._closed, 'waiting_or_running': self._waiting,
                'inflight_work': int(self._future is not None and not self._future.done())}
