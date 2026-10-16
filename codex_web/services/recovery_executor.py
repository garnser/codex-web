"""Bounded ownership of scheduled and manual HTTP Recovery worker futures.

Durable firing admission belongs to RecoveryState, not this executor.
"""
import asyncio
import contextvars
import threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor


class RecoveryExecutorBusy(RuntimeError):
    pass


class RecoveryExecutorClosed(RuntimeError):
    pass


class RecoveryExecutor:
    def __init__(self):
        self._admission_lock = threading.RLock()
        self._quiescing = False
        self._exclusive_key = None
        self._owned_key = contextvars.ContextVar("recovery_owned_future", default=None)
        self._pool = None
        self._futures = {}
        self._closed = False
        self._terminal_observations = {}

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

    async def run(self, key, operation, *, exclusive=False):
        with self._admission_lock:
            if self._closed or self._quiescing:
                raise RecoveryExecutorClosed('recovery admission is closed or settling a quiescent snapshot')
            # Reserve exclusive work at submission, not inside the single
            # worker where it could wait behind the writer it must refuse.
            # The reservation also prevents new writers entering before CAS.
            if self._exclusive_key is not None and self._exclusive_key != key:
                raise RecoveryExecutorBusy('exclusive Recovery work is still owned')
            future = self._futures.get(key)
            if future is None:
                if exclusive and self._futures:
                    raise RecoveryExecutorBusy('other actual local Recovery work is not drained')
                if len(self._futures) >= 2:
                    raise RecoveryExecutorBusy('recovery executor is at capacity')
                if self._pool is None:
                    self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='recovery-scheduled')
                context = contextvars.copy_context()
                def owned():
                    token = self._owned_key.set(key)
                    try:
                        return operation()
                    finally:
                        self._owned_key.reset(token)
                future = self._pool.submit(context.run, owned)
                self._futures[key] = future
                if exclusive:
                    self._exclusive_key = key
        try:
            return await self._settle(future)
        finally:
            with self._admission_lock:
                self._retire(key, future)

    def _retire(self, key, future):
        if future.done() and self._futures.get(key) is future:
            # Failed serialization can retain a complete snapshot through
            # its traceback; release terminal futures promptly.
            error = future.exception()
            self._terminal_observations[key] = {"event_id":key,
                "error_class":type(error).__name__ if error is not None else None,
                "future_terminal":True,"effect_outcome":"not inferred"}
            while len(self._terminal_observations) > 2:
                del self._terminal_observations[next(iter(self._terminal_observations))]
            del self._futures[key]
            if self._exclusive_key == key:
                self._exclusive_key = None


    @contextmanager
    def quiescence(self):
        # A thread performing the canonical CAS blocks ALL local submissions,
        # including manual requests, without holding a lock across await/I/O.
        with self._admission_lock:
            own = self._owned_key.get()
            if self._quiescing or any(key != own for key in self._futures):
                raise RecoveryExecutorBusy('other actual local Recovery work is not drained')
            self._quiescing = True
        try:
            yield
        finally:
            with self._admission_lock:
                self._quiescing = False

    def close_admission(self):
        with self._admission_lock:
            self._closed = True

    async def stop(self):
        self.close_admission()
        cancelled = False
        error = None
        try:
            with self._admission_lock:
                futures = list(self._futures.values())
            for future in futures:
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
            with self._admission_lock:
                self._futures.clear()
                self._exclusive_key = None
        if cancelled:
            raise asyncio.CancelledError
        if error is not None:
            raise error

    def status(self):
        with self._admission_lock:
            return {'accepting': not self._closed, 'waiting_or_running': len(self._futures),
                'inflight_work': sum(not future.done() for future in self._futures.values()),
                'terminal_observations': list(self._terminal_observations.values()),
                'quiescence_guard': self._quiescing,
                'observation_scope': 'process-local manual+scheduled work; not proof of another owner or effect'}
