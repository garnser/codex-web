from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from codex_web.services.keyed_background_tasks import KeyedTaskCoordinator
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
    """Own bounded, coalesced native recovery scheduling."""

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
        self.last_reason: str | None = None
        self.shutting_down = False
        cycle_concurrency = max(1, len(cycles))
        self.coordinator = KeyedTaskCoordinator(
            max_concurrency=cycle_concurrency,
            per_scope_concurrency=cycle_concurrency,
        )

    @property
    def tasks(self) -> set[asyncio.Task[None]]:
        return {
            task
            for task in self.coordinator._tasks.values()
            if not task.done()
        }

    def set_shutting_down(self, value: bool) -> None:
        self.shutting_down = value

    def status(self) -> dict[str, Any]:
        return {
            "lastScheduledAt": self.last_scheduled_at or None,
            "lastReason": self.last_reason,
            "shuttingDown": self.shutting_down,
            **self.coordinator.status(),
        }

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
        self.last_reason = reason
        self.append_event(
            {"type": "native_recovery_scheduled", "reason": reason}
        )
        timeout_getter = getattr(
            self.policy,
            "native_recovery_cycle_timeout",
            None,
        )
        timeout = (
            float(timeout_getter())
            if callable(timeout_getter)
            else 300.0
        )
        for index, cycle in enumerate(self.cycles):
            key = f"native-recovery:{index}"
            created = self.coordinator.schedule(
                key,
                cycle,
                revision=f"{now:.6f}:{reason}",
                scope="native-recovery",
                timeout_seconds=timeout,
            )
            if not created:
                self.append_event(
                    {
                        "type": "native_recovery_coalesced",
                        "reason": reason,
                        "cycle": index,
                    }
                )
        return True

    async def stop(self) -> None:
        self.shutting_down = True
        await self.coordinator.stop()
