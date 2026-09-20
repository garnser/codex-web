from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class _PendingWork:
    factory: Callable[[], Awaitable[None]]
    revision: str | None
    scope: str | None
    timeout_seconds: float | None
    queued_at: float


class KeyedTaskCoordinator:
    """Bounded in-process coalescing for ephemeral coroutine work.

    This is deliberately not a durable scheduler. It coordinates already
    triggered in-process work so repeated triggers do not create overlapping
    task storms. Durable time/event scheduling remains owned by SchedulerService.
    """

    def __init__(
        self,
        *,
        max_concurrency: int = 16,
        per_scope_concurrency: int = 4,
    ) -> None:
        self.max_concurrency = max(1, int(max_concurrency))
        self.per_scope_concurrency = max(
            1,
            int(per_scope_concurrency),
        )
        self._global_semaphore = asyncio.Semaphore(
            self.max_concurrency
        )
        self._scope_semaphores: dict[str, asyncio.Semaphore] = {}
        self._pending: dict[str, _PendingWork] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._last_desired_revision: dict[str, str | None] = {}
        self._last_completed_revision: dict[str, str | None] = {}
        self._last_started_at: dict[str, float] = {}
        self._last_completed_at: dict[str, float] = {}
        self._last_duration: dict[str, float] = {}
        self._last_error: dict[str, str] = {}
        self._metrics: dict[str, int] = {
            "scheduled": 0,
            "coalesced": 0,
            "started": 0,
            "completed": 0,
            "failed": 0,
            "timedOut": 0,
            "cancelled": 0,
        }
        self._running = 0
        self._max_running = 0
        self._stopping = False

    def _scope_semaphore(
        self,
        scope: str | None,
    ) -> asyncio.Semaphore | None:
        if not scope:
            return None
        semaphore = self._scope_semaphores.get(scope)
        if semaphore is None:
            semaphore = asyncio.Semaphore(
                self.per_scope_concurrency
            )
            self._scope_semaphores[scope] = semaphore
        return semaphore

    def schedule(
        self,
        key: str,
        factory: Callable[[], Awaitable[None]],
        *,
        revision: str | None = None,
        scope: str | None = None,
        timeout_seconds: float | None = None,
    ) -> bool:
        """Schedule latest work for a key.

        Returns True when a new coordinator task was created. If the key already
        has pending/running work, the pending factory is replaced by the latest
        trigger and False is returned.
        """

        if self._stopping:
            return False
        key = str(key or "").strip()
        if not key:
            raise ValueError("keyed background task key must not be empty")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False

        existing = self._tasks.get(key)
        coalesced = (
            key in self._pending
            or (existing is not None and not existing.done())
        )
        self._pending[key] = _PendingWork(
            factory=factory,
            revision=revision,
            scope=scope,
            timeout_seconds=(
                max(0.0, float(timeout_seconds))
                if timeout_seconds is not None
                else None
            ),
            queued_at=time.time(),
        )
        self._last_desired_revision[key] = revision
        self._metrics["scheduled"] += 1
        if coalesced:
            self._metrics["coalesced"] += 1
            return False

        task = loop.create_task(
            self._run_key(key),
            name=f"keyed-background:{key}",
        )
        self._tasks[key] = task

        def done(completed: asyncio.Task[None]) -> None:
            if self._tasks.get(key) is completed:
                self._tasks.pop(key, None)
            try:
                completed.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                # _run_key accounts for normal work failures; this protects
                # the coordinator lifecycle from an unexpected wrapper error.
                self._metrics["failed"] += 1

        task.add_done_callback(done)
        return True

    async def _run_one(
        self,
        key: str,
        work: _PendingWork,
    ) -> None:
        scope_semaphore = self._scope_semaphore(work.scope)
        await self._global_semaphore.acquire()
        if scope_semaphore is not None:
            await scope_semaphore.acquire()

        started = time.time()
        self._last_started_at[key] = started
        self._metrics["started"] += 1
        self._running += 1
        self._max_running = max(
            self._max_running,
            self._running,
        )
        try:
            coroutine = work.factory()
            timeout = work.timeout_seconds
            if timeout is not None and timeout > 0:
                await asyncio.wait_for(
                    coroutine,
                    timeout=timeout,
                )
            else:
                await coroutine
        except asyncio.CancelledError:
            self._metrics["cancelled"] += 1
            raise
        except asyncio.TimeoutError:
            self._metrics["timedOut"] += 1
            self._last_error[key] = "timeout"
        except Exception as exc:
            self._metrics["failed"] += 1
            self._last_error[key] = str(exc)[:500]
        else:
            self._metrics["completed"] += 1
            self._last_completed_revision[key] = work.revision
            self._last_error.pop(key, None)
        finally:
            finished = time.time()
            self._last_completed_at[key] = finished
            self._last_duration[key] = max(
                0.0,
                finished - started,
            )
            self._running = max(0, self._running - 1)
            if scope_semaphore is not None:
                scope_semaphore.release()
            self._global_semaphore.release()

    async def _run_key(self, key: str) -> None:
        while not self._stopping:
            work = self._pending.pop(key, None)
            if work is None:
                return
            await self._run_one(key, work)

    def status(self) -> dict[str, Any]:
        now = time.time()
        queued_at = [
            item.queued_at for item in self._pending.values()
        ]
        return {
            **self._metrics,
            "queueDepth": len(self._pending),
            "activeTasks": sum(
                1
                for task in self._tasks.values()
                if not task.done()
            ),
            "running": self._running,
            "maxRunningObserved": self._max_running,
            "maxConcurrency": self.max_concurrency,
            "perScopeConcurrency": self.per_scope_concurrency,
            "oldestPendingAgeSeconds": (
                max(0.0, now - min(queued_at))
                if queued_at
                else 0.0
            ),
            "lastDesiredRevision": dict(
                self._last_desired_revision
            ),
            "lastCompletedRevision": dict(
                self._last_completed_revision
            ),
            "lastStartedAt": dict(self._last_started_at),
            "lastCompletedAt": dict(self._last_completed_at),
            "lastDurationSeconds": dict(self._last_duration),
            "lastError": dict(self._last_error),
        }

    async def stop(self) -> None:
        self._stopping = True
        self._pending.clear()
        tasks = [
            task
            for task in self._tasks.values()
            if not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )
        self._tasks.clear()
        self._scope_semaphores.clear()
