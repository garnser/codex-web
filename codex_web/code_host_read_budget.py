"""Ephemeral trusted read execution budget; never request data or authority."""
import asyncio
import inspect
import math
import time


class ReadExecutionBudget:
    """One reader's cumulative HTTP time under its owner's absolute deadline.

    Execute and verification construct separate readers, each with 30 seconds
    of HTTP time, while retaining the same trusted parent deadline. Canonical
    before/after guards run outside request() but inside that parent deadline.
    """
    NETWORK_SECONDS = 30.0

    def __init__(self, deadline_at: float, *, clock=None, monotonic=None):
        if isinstance(deadline_at, bool) or not math.isfinite(deadline_at):
            raise ValueError('read execution deadline must be finite')
        self.deadline_at = float(deadline_at)
        self.clock = time.time if clock is None else clock
        self.monotonic = time.monotonic if monotonic is None else monotonic
        self.network_seconds_used = 0.0
        self._network_exhausted = False

    def remaining_seconds(self):
        now = float(self.clock())
        remaining = self.deadline_at - now
        if (not math.isfinite(now) or not math.isfinite(remaining) or remaining <= 0
                or self._network_exhausted or self.network_seconds_used >= self.NETWORK_SECONDS):
            raise TimeoutError('read execution budget exhausted')
        return remaining

    def require_current(self):
        self.remaining_seconds()

    async def request(self, awaitable):
        """Charge only the bounded actual client await, including failed I/O."""
        try:
            seconds = min(self.remaining_seconds(), self.NETWORK_SECONDS - self.network_seconds_used)
            started = float(self.monotonic())
            if not math.isfinite(started):
                raise TimeoutError('read execution clock unavailable')
        except BaseException:
            # Readers supply an unstarted client coroutine. Refusal must not
            # launch it or leave an unawaited coroutine retaining credentials.
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise
        try:
            async with asyncio.timeout(seconds):
                result = await awaitable
        except TimeoutError:
            self._network_exhausted = True
            raise
        finally:
            elapsed = float(self.monotonic()) - started
            if not math.isfinite(elapsed) or elapsed < 0:
                self._network_exhausted = True
            else:
                self.network_seconds_used += elapsed
                if self.network_seconds_used >= self.NETWORK_SECONDS:
                    self._network_exhausted = True
        self.require_current()
        return result
