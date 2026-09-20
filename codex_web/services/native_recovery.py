from __future__ import annotations

import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from codex_web.services.keyed_tasks import KeyedTaskCoordinator
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
        try:
            concurrency = int(
                os.environ.get(
                    "CODEX_WEB_NATIVE_RECOVERY_CONCURRENCY"
                )
                or "4"
            )
        except ValueError:
            concurrency = 4
        try:
            timeout_seconds = float(
                os.environ.get(
                    "CODEX_WEB_NATIVE_RECOVERY_CYCLE_TIMEOUT_SECONDS"
                )
                or "120"
            )
        except ValueError:
            timeout_seconds = 120.0
        self.coordinator = KeyedTaskCoordinator(
            name="native-recovery",
            max_concurrency=max(1, min(concurrency, 16)),
            timeout_seconds=max(1.0, min(timeout_seconds, 900.0)),
            event_sink=append_event,
        )
        # Compatibility/inspection seam: callers historically counted tasks.
        self.tasks = self.coordinator._tasks

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
        submitted = False
        for index, cycle in enumerate(self.cycles):
            name = getattr(cycle, "__name__", cycle.__class__.__name__)
            key = f"cycle:{index}:{name}"
            submitted = (
                self.coordinator.submit(
                    key,
                    cycle,
                    metadata={
                        "reason": reason,
                        "cycle": name,
                    },
                )
                or submitted
            )
        return submitted

    def status(self) -> dict[str, Any]:
        return {
            "lastScheduledAt": self.last_scheduled_at or None,
            "shuttingDown": self.shutting_down,
            **self.coordinator.status(),
        }

    async def stop(self) -> None:
        self.shutting_down = True
        await self.coordinator.stop()
