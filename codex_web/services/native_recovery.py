from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from codex_web.services.runtime_policy import RuntimePolicy


class DeferredRecoveryScheduler:
    """Explicitly bridge composition order without a legacy service locator."""

    def __init__(self) -> None:
        self._delegate: NativeRecoveryService | None = None

    def bind(self, delegate: "NativeRecoveryService") -> None:
        self._delegate = delegate

    def schedule(self, *, reason: str = "manual") -> bool:
        if self._delegate is None:
            return False
        return self._delegate.schedule(reason=reason)


class NativeRecoveryService:
    """Own idempotent native recovery scheduling and cooldown state."""

    def __init__(
        self,
        *,
        policy: RuntimePolicy,
        cycles: tuple[Callable[[], Awaitable[None]], ...],
        append_event: Callable[[dict[str, Any]], None],
    ) -> None:
        self.policy = policy
        self.cycles = cycles
        self.append_event = append_event
        self.last_scheduled_at = 0.0
        self.shutting_down = False
        self.tasks: set[asyncio.Task[None]] = set()

    def set_shutting_down(self, value: bool) -> None:
        self.shutting_down = value

    def schedule(self, *, reason: str = "manual") -> bool:
        if not self.policy.autonomy_enabled() or self.shutting_down:
            return False
        now = time.time()
        if (
            now - self.last_scheduled_at
            < self.policy.native_recovery_cooldown()
        ):
            return False
        self.last_scheduled_at = now
        self.append_event(
            {"type": "native_recovery_scheduled", "reason": reason}
        )
        for cycle in self.cycles:
            task = asyncio.create_task(cycle())
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return True

    async def stop(self) -> None:
        self.shutting_down = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
