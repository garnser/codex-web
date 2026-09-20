from __future__ import annotations

import os
import time
from collections import deque
from collections.abc import Callable

from fastapi import HTTPException

from codex_web.models import QueuedTurn


TurnQueueLoader = Callable[[], dict[str, list[QueuedTurn]]]
TurnQueueGetter = Callable[[str], list[QueuedTurn]]


class TurnQueuePolicy:
    """Own queue visibility, depth limits, and steering-rate policy."""

    def __init__(
        self,
        load_queues: TurnQueueLoader,
        get_queue: TurnQueueGetter | None = None,
    ) -> None:
        self.load_queues = load_queues
        self.get_queue = get_queue
        self.steer_times: dict[str, deque[float]] = {}

    def queue(self, thread_id: str | None) -> list[QueuedTurn]:
        if not thread_id:
            return []
        if self.get_queue is not None:
            return self.get_queue(thread_id)
        return self.load_queues().get(thread_id, [])

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
        recent = self.steer_times.setdefault(thread_id, deque())
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


def install_turn_queue_policy(
    app,
    host,
    *,
    load_queues: TurnQueueLoader | None = None,
    get_queue: TurnQueueGetter | None = None,
) -> TurnQueuePolicy:
    existing = getattr(app.state, "turn_queue_policy", None)
    load_queues = load_queues or host._load_turn_queues
    get_queue = get_queue or getattr(host, "_thread_queue_record", None)
    if (
        isinstance(existing, TurnQueuePolicy)
        and existing.load_queues == load_queues
        and existing.get_queue == get_queue
    ):
        policy = existing
    else:
        policy = TurnQueuePolicy(load_queues, get_queue=get_queue)
        app.state.turn_queue_policy = policy

    # Compatibility aliases for direct import-server callers. Internal services
    # receive the policy object explicitly.
    host._thread_queue = policy.queue
    host._thread_queue_depth = policy.depth
    host._max_thread_queue_depth = policy.max_depth
    host._steer_window_seconds = policy.steer_window_seconds
    host._max_steers_per_window = policy.max_steers_per_window
    host._record_thread_steer = policy.record_steer
    return policy
