from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

from codex_web.services.keyed_background_tasks import KeyedTaskCoordinator
from codex_web.services.runtime_policy import RuntimePolicy
from codex_web.storage.state_store import storage_operation_budget


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
        usage_cycle: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.policy = policy
        self.cycles = cycles
        if usage_cycle is not None and usage_cycle not in cycles:
            raise ValueError("Runtime usage recovery must be an existing recovery cycle")
        self.usage_cycle = usage_cycle
        self.append_event = append_event
        self.last_scheduled_at = 0.0
        self.last_reason: str | None = None
        self.shutting_down = False
        self._notification_lock = threading.Lock()
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._usage_wake: asyncio.Handle | None = None
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
        # RuntimeSupervisor starts/stops this owner on the ASGI loop. Worker
        # listeners may submit a wake, never choose or create an event loop.
        with self._notification_lock:
            self.shutting_down = value
            if value:
                self._owner_loop = None
                if self._usage_wake is not None:
                    self._usage_wake.cancel()
                    self._usage_wake = None
            else:
                self._owner_loop = asyncio.get_running_loop()

    def notify_runtime_usage(self) -> bool:
        """Coalesce persisted-usage notifications from any EventHub worker."""
        context = contextvars.copy_context()
        # This event owns a new bounded recovery cycle, not the publisher's
        # already-settled storage operation; retain all other request context.
        context.run(storage_operation_budget.set, None)
        with self._notification_lock:
            loop = self._owner_loop
            if self.shutting_down or loop is None or loop.is_closed() or self.usage_cycle is None:
                return False
            if self._usage_wake is not None:
                return False

            def wake() -> None:
                with self._notification_lock:
                    if self.shutting_down or self._owner_loop is not loop:
                        return
                    self._usage_wake = None
                if self.usage_cycle is not None:
                    self._schedule(reason="home-runtime-usage-observed", required=True,
                        indexes=tuple(index for index, cycle in enumerate(self.cycles)
                            if cycle == self.usage_cycle))

            try:
                self._usage_wake = loop.call_soon_threadsafe(wake, context=context)
            except RuntimeError:
                # A loop closing concurrently cannot accept recovery work.
                return False
            return True

    def status(self) -> dict[str, Any]:
        return {
            "lastScheduledAt": self.last_scheduled_at or None,
            "lastReason": self.last_reason,
            "shuttingDown": self.shutting_down,
            **self.coordinator.status(),
        }

    def schedule(self, *, reason: str = "manual") -> bool:
        return self._schedule(reason=reason, indexes=tuple(range(len(self.cycles))))

    def _schedule(self, *, reason: str, indexes: tuple[int, ...], required: bool = False) -> bool:
        if not self.policy.autonomy_enabled() or self.shutting_down:
            return False
        now = time.time()
        if (
            not required and now - self.last_scheduled_at
            < self.policy.native_recovery_cooldown()
        ):
            return False
        # Home-only terminal usage must not postpone unrelated ordinary cycles.
        if not required:
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
        for index in indexes:
            cycle = self.cycles[index]
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
        self.set_shutting_down(True)
        await self.coordinator.stop()
