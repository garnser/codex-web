from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(slots=True)
class _Submission:
    operation: Callable[[], Awaitable[Any]]
    group: str | None
    metadata: dict[str, Any]
    submitted_at: float


class KeyedTaskCoordinator:
    """Bounded in-process coordinator for replaceable background work.

    This is deliberately not a durable scheduler. It owns immediate coroutine
    execution inside one application process: one runner per semantic key,
    latest-pending-work coalescing, bounded concurrency, timeout, metrics, and
    shutdown.
    """

    def __init__(
        self,
        *,
        max_concurrency: int = 16,
        per_group_limit: int | None = None,
        timeout_seconds: float = 60.0,
        name: str = "background",
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.name = str(name or "background")
        self.max_concurrency = max(1, int(max_concurrency))
        self.per_group_limit = (
            max(1, int(per_group_limit))
            if per_group_limit is not None
            else None
        )
        self.timeout_seconds = max(0.0, float(timeout_seconds))
        self.event_sink = event_sink or (lambda _event: None)
        self._global = asyncio.Semaphore(self.max_concurrency)
        self._groups: dict[str, asyncio.Semaphore] = {}
        self._pending: dict[str, _Submission] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._running: dict[str, dict[str, Any]] = {}
        self._key_stats: dict[str, dict[str, Any]] = {}
        self._stats = {
            "submitted": 0,
            "coalesced": 0,
            "started": 0,
            "completed": 0,
            "failed": 0,
            "timedOut": 0,
            "cancelled": 0,
        }
        self._stopping = False

    def _group_semaphore(
        self,
        group: str | None,
    ) -> asyncio.Semaphore | None:
        if group is None or self.per_group_limit is None:
            return None
        return self._groups.setdefault(
            group,
            asyncio.Semaphore(self.per_group_limit),
        )

    def submit(
        self,
        key: str,
        operation: Callable[[], Awaitable[Any]],
        *,
        group: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        key = str(key or "").strip()
        if not key:
            raise ValueError("background task key must not be empty")
        if self._stopping:
            return False
        now = time.time()
        self._stats["submitted"] += 1
        if key in self._pending or (
            key in self._tasks and not self._tasks[key].done()
        ):
            self._stats["coalesced"] += 1
        self._pending[key] = _Submission(
            operation=operation,
            group=str(group) if group is not None else None,
            metadata=dict(metadata or {}),
            submitted_at=now,
        )
        key_stats = self._key_stats.setdefault(key, {})
        key_stats["lastTriggerAt"] = now
        key_stats["coalescedTriggers"] = int(
            key_stats.get("coalescedTriggers", 0)
        ) + (
            1
            if key in self._tasks and not self._tasks[key].done()
            else 0
        )
        task = self._tasks.get(key)
        if task is None or task.done():
            task = asyncio.create_task(
                self._run_key(key),
                name=f"{self.name}:{key}",
            )
            self._tasks[key] = task
            task.add_done_callback(
                lambda completed, item_key=key: self._done(
                    item_key,
                    completed,
                )
            )
        return True

    def _done(
        self,
        key: str,
        task: asyncio.Task[None],
    ) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)

    async def _invoke(self, submission: _Submission) -> None:
        group_semaphore = self._group_semaphore(submission.group)
        async with self._global:
            if group_semaphore is None:
                await self._invoke_with_timeout(submission)
                return
            async with group_semaphore:
                await self._invoke_with_timeout(submission)

    async def _invoke_with_timeout(
        self,
        submission: _Submission,
    ) -> None:
        coroutine = submission.operation()
        if self.timeout_seconds <= 0:
            await coroutine
            return
        await asyncio.wait_for(
            coroutine,
            timeout=self.timeout_seconds,
        )

    async def _run_key(self, key: str) -> None:
        try:
            while not self._stopping:
                submission = self._pending.pop(key, None)
                if submission is None:
                    return
                started_at = time.time()
                self._stats["started"] += 1
                self._running[key] = {
                    "startedAt": started_at,
                    "group": submission.group,
                    "metadata": dict(submission.metadata),
                }
                key_stats = self._key_stats.setdefault(key, {})
                key_stats["running"] = True
                key_stats["lastStartAt"] = started_at
                try:
                    await self._invoke(submission)
                except asyncio.CancelledError:
                    self._stats["cancelled"] += 1
                    key_stats["cancelled"] = int(
                        key_stats.get("cancelled", 0)
                    ) + 1
                    raise
                except asyncio.TimeoutError:
                    self._stats["timedOut"] += 1
                    key_stats["timedOut"] = int(
                        key_stats.get("timedOut", 0)
                    ) + 1
                    self.event_sink(
                        {
                            "type": "background_task_timed_out",
                            "coordinator": self.name,
                            "key": key,
                            "group": submission.group,
                            **submission.metadata,
                        }
                    )
                except Exception as exc:
                    self._stats["failed"] += 1
                    key_stats["failed"] = int(
                        key_stats.get("failed", 0)
                    ) + 1
                    key_stats["lastError"] = str(exc)[:500]
                    self.event_sink(
                        {
                            "type": "background_task_failed",
                            "coordinator": self.name,
                            "key": key,
                            "group": submission.group,
                            "error": str(exc)[:500],
                            **submission.metadata,
                        }
                    )
                else:
                    self._stats["completed"] += 1
                    key_stats["completed"] = int(
                        key_stats.get("completed", 0)
                    ) + 1
                    key_stats.pop("lastError", None)
                finally:
                    ended_at = time.time()
                    key_stats["running"] = False
                    key_stats["lastEndAt"] = ended_at
                    key_stats["lastDurationSeconds"] = max(
                        0.0,
                        ended_at - started_at,
                    )
                    self._running.pop(key, None)
        finally:
            self._running.pop(key, None)

    def status(self) -> dict[str, Any]:
        now = time.time()
        return {
            **self._stats,
            "running": len(self._running),
            "pending": len(self._pending),
            "taskCount": sum(
                1 for task in self._tasks.values() if not task.done()
            ),
            "maxConcurrency": self.max_concurrency,
            "perGroupLimit": self.per_group_limit,
            "timeoutSeconds": self.timeout_seconds,
            "oldestPendingAgeSeconds": (
                max(
                    0.0,
                    now - min(
                        submission.submitted_at
                        for submission in self._pending.values()
                    ),
                )
                if self._pending
                else 0.0
            ),
            "runningOperations": {
                key: dict(value)
                for key, value in self._running.items()
            },
            "keys": {
                key: dict(value)
                for key, value in self._key_stats.items()
            },
        }

    async def stop(
        self,
        *,
        drain_timeout: float = 5.0,
    ) -> None:
        self._stopping = True
        tasks = [
            task for task in self._tasks.values() if not task.done()
        ]
        timeout = max(0.0, float(drain_timeout))
        if tasks and timeout > 0:
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(asyncio.shield(task) for task in tasks),
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                pass
        remaining = [
            task for task in self._tasks.values() if not task.done()
        ]
        for task in remaining:
            task.cancel()
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)
        self._pending.clear()
        self._running.clear()
        self._tasks.clear()
