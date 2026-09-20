from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any


class GitLabSyncHealth:
    """Own mutable GitLab work-item synchronization health state."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        on_change: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.clock = clock
        self.on_change = on_change
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._last_error: str | None = None
        self._last_error_at = 0.0
        self._last_success_at = 0.0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "consecutive_failures": self._consecutive_failures,
                "last_error": self._last_error,
                "last_error_at": self._last_error_at,
                "last_success_at": self._last_success_at,
            }

    def _notify(self) -> dict[str, Any]:
        snapshot = self.snapshot()
        if self.on_change is not None:
            self.on_change(dict(snapshot))
        return snapshot

    def record_failure(self, error: str) -> dict[str, Any]:
        now = float(self.clock())
        with self._lock:
            self._consecutive_failures += 1
            self._last_error = error
            self._last_error_at = now
        return self._notify()

    def record_success(self) -> dict[str, Any]:
        now = float(self.clock())
        with self._lock:
            self._consecutive_failures = 0
            self._last_error = None
            self._last_success_at = now
        return self._notify()
