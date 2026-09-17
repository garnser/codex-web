from __future__ import annotations

import os
import time
from collections import deque
from typing import Any

from fastapi import HTTPException

from codex_web.models import QueuedTurn


class TurnQueuePolicy:
    """Own queue visibility, depth limits, and steering-rate policy."""

    def __init__(self, host: Any) -> None:
        self.host = host

    def queue(self, thread_id: str | None) -> list[QueuedTurn]:
        if not thread_id:
            return []
        return self.host._load_turn_queues().get(thread_id, [])

    def depth(self, thread_id: str | None) -> int:
        return len(self.queue(thread_id))

    @staticmethod
    def max_depth() -> int:
        try:
            value = int(os.environ.get("CODEX_WEB_MAX_THREAD_QUEUE_DEPTH") or "12")
        except ValueError:
            return 12
        return max(1, min(value, 100))

    @staticmethod
    def steer_window_seconds() -> float:
        try:
            value = float(os.environ.get("CODEX_WEB_STEER_WINDOW_SECONDS") or "60")
        except ValueError:
            return 60.0
        return max(1.0, value)

    @staticmethod
    def max_steers_per_window() -> int:
        try:
            value = int(os.environ.get("CODEX_WEB_MAX_STEERS_PER_WINDOW") or "4")
        except ValueError:
            return 4
        return max(1, min(value, 50))

    def record_steer(self, thread_id: str, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        window = self.steer_window_seconds()
        recent = self.host.THREAD_STEER_TIMES.setdefault(thread_id, deque())
        while recent and timestamp - recent[0] >= window:
            recent.popleft()
        if len(recent) >= self.max_steers_per_window():
            retry_after = max(1, int(window - (timestamp - recent[0])))
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "thread_steer_rate_limited",
                    "threadId": thread_id,
                    "retryAfterSeconds": retry_after,
                },
            )
        recent.append(timestamp)


def install_turn_queue_policy(app: Any, host: Any) -> TurnQueuePolicy:
    existing = getattr(app.state, "turn_queue_policy", None)
    if isinstance(existing, TurnQueuePolicy) and existing.host is host:
        policy = existing
    else:
        policy = TurnQueuePolicy(host)
        app.state.turn_queue_policy = policy

    host._thread_queue = policy.queue
    host._thread_queue_depth = policy.depth
    host._max_thread_queue_depth = policy.max_depth
    host._steer_window_seconds = policy.steer_window_seconds
    host._max_steers_per_window = policy.max_steers_per_window
    host._record_thread_steer = policy.record_steer
    return policy
