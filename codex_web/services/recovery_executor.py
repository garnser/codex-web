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
        self._pool = None
        self._futures = {}
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
                if future.done():
                    break
                raise
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
        if key in self._futures:
            return await self._settle(self._futures[key])
        # Own a bounded submitted future for BOTH accepted jobs, including the
        # queued job. An asyncio waiter cancelled before acquiring a serial
        # lock would otherwise silently discard an already committed event.
        if len(self._futures) >= 2:
            raise RecoveryExecutorBusy('scheduled recovery executor is at capacity')
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='recovery-scheduled')
        context = contextvars.copy_context()
        future = self._pool.submit(context.run, operation)
        self._futures[key] = future
        try:
            return await self._settle(future)
        finally:
            if future.done() and self._futures.get(key) is future:
                # Failed serialization can retain a complete snapshot through
                # its traceback; release terminal futures promptly.
                del self._futures[key]

    def close_admission(self):
        self._closed = True

    async def stop(self):
        self.close_admission()
        cancelled = False
        error = None
        try:
            for future in list(self._futures.values()):
                try:
                    await self._settle(future)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    # The operation's caller owns its recorded failure. A
                    # completed failure must not abort application shutdown.
                    pass
                except BaseException as exc:
                    error = exc
        finally:
            if self._pool is not None:
                self._pool.shutdown(wait=True, cancel_futures=False)
                self._pool = None
            self._futures.clear()
        if cancelled:
            raise asyncio.CancelledError
        if error is not None:
            raise error

    def status(self):
        return {'accepting': not self._closed, 'waiting_or_running': len(self._futures),
                'inflight_work': sum(not future.done() for future in self._futures.values())}
