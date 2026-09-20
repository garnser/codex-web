from __future__ import annotations

import os
import time
from typing import Any


class WatchdogDispatchPolicy:
    """Own watchdog dispatch cooldown and throttling decisions."""

    def __init__(
        self,
        host: Any | None = None,
        *,
        timestamps: dict[str, float] | None = None,
    ) -> None:
        if timestamps is None and host is not None:
            timestamps = getattr(host, "WATCHDOG_DISPATCH_TIMES", None)
        self.timestamps = timestamps if timestamps is not None else {}

    @staticmethod
    def cooldown_seconds() -> float:
        try:
            seconds = float(os.environ.get("CODEX_WEB_WATCHDOG_DISPATCH_COOLDOWN_SECONDS") or "120")
        except ValueError:
            return 120.0
        return max(5.0, seconds)

    def allowed(self, key: str, *, now: float | None = None) -> bool:
        # Preserve the historical truthiness behavior: now=0 falls back to the
        # current clock rather than being treated as an explicit timestamp.
        timestamp = now or time.time()
        last = self.timestamps.get(key)
        if last is None:
            return True
        return (timestamp - last) >= self.cooldown_seconds()

    def record(self, key: str, *, now: float | None = None) -> None:
        # Keep the timestamp map on the compatibility host until process-global
        # runtime state is migrated as a separate, explicit change.
        self.timestamps[key] = now or time.time()


def install_watchdog_dispatch_policy(
    app: Any,
    host: Any,
    *,
    timestamps: dict[str, float] | None = None,
) -> WatchdogDispatchPolicy:
    existing = getattr(app.state, "watchdog_dispatch_policy", None)
    if isinstance(existing, WatchdogDispatchPolicy):
        policy = existing
    else:
        policy = WatchdogDispatchPolicy(
            host,
            timestamps=timestamps,
        )
        app.state.watchdog_dispatch_policy = policy

    host.WATCHDOG_DISPATCH_TIMES = policy.timestamps
    host._watchdog_dispatch_cooldown_seconds = policy.cooldown_seconds
    host._watchdog_dispatch_allowed = policy.allowed
    host._record_watchdog_dispatch = policy.record
    return policy
