from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException

from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter


class _ThreadRuntimeTransport:
    def __init__(
        self,
        request_for_thread: Callable[
            [str, str, dict[str, Any]],
            Awaitable[Any],
        ],
        thread_id: str,
    ) -> None:
        self.request_for_thread = request_for_thread
        self.thread_id = thread_id

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return await self.request_for_thread(
            self.thread_id,
            method,
            params or {},
        )


class ContextCompactionService:
    """Coordinates manual and automatic native Codex thread compaction."""

    def __init__(
        self,
        *,
        request_for_thread: Callable[
            [str, str, dict[str, Any]],
            Awaitable[Any],
        ],
        pending_approvals: Callable[[], dict[Any, dict[str, Any]]],
        approval_thread_id: Callable[[dict[str, Any]], str | None],
        queue_depth: Callable[[str], int],
        thread_is_active: Callable[[str], bool],
        raise_if_thread_replaced: Callable[[str], None],
        publish_event: Callable[[dict[str, Any]], Awaitable[Any]],
    ) -> None:
        self.request_for_thread = request_for_thread
        self.pending_approvals = pending_approvals
        self.approval_thread_id = approval_thread_id
        self.queue_depth = queue_depth
        self.thread_is_active = thread_is_active
        self.raise_if_thread_replaced = raise_if_thread_replaced
        self.publish_event = publish_event
        self.auto_threshold = self._env_percent(
            "CODEX_WEB_AUTO_COMPACT_PERCENT",
            75.0,
        )
        self.cooldown_seconds = max(
            0.0,
            self._env_float(
                "CODEX_WEB_COMPACT_COOLDOWN_SECONDS",
                300.0,
            ),
        )
        self._usage_percent: dict[str, float] = {}
        self._last_compacted_at: dict[str, float] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._inflight: set[str] = set()

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, str(default)))
        except (TypeError, ValueError):
            return default

    @classmethod
    def _env_percent(cls, name: str, default: float) -> float:
        value = cls._env_float(name, default)
        if value <= 0:
            return 0.0
        return min(95.0, max(50.0, value))

    @staticmethod
    def _token_percent(
        token_usage: dict[str, Any],
    ) -> float | None:
        total = (
            token_usage.get("total")
            or token_usage.get("total_token_usage")
            or {}
        )
        if not isinstance(total, dict):
            total = {}
        total_tokens = total.get(
            "totalTokens",
            total.get("total_tokens", 0),
        )
        context_window = token_usage.get(
            "modelContextWindow",
            token_usage.get("model_context_window", 0),
        )
        try:
            total_value = float(total_tokens or 0)
            window_value = float(context_window or 0)
        except (TypeError, ValueError):
            return None
        if total_value <= 0 or window_value <= 0:
            return None
        return min(
            100.0,
            max(0.0, total_value / window_value * 100.0),
        )

    def observe(self, event: dict[str, Any]) -> None:
        if event.get("type") != "codex.event":
            return
        message = event.get("message") or {}
        if not isinstance(message, dict):
            return
        method = message.get("method")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return
        thread_id = params.get("threadId") or (
            params.get("turn") or {}
        ).get("threadId")
        if not thread_id:
            return
        thread_id = str(thread_id)

        if method == "thread/tokenUsage/updated":
            token_usage = (
                params.get("tokenUsage")
                or params.get("token_usage")
                or {}
            )
            if isinstance(token_usage, dict):
                percent = self._token_percent(token_usage)
                if percent is not None:
                    self._usage_percent[thread_id] = percent
                    if (
                        self.auto_threshold
                        and percent >= self.auto_threshold
                    ):
                        self._schedule_auto(thread_id)
            return

        if method == "thread/status/changed":
            status = params.get("status") or {}
            if (
                isinstance(status, dict)
                and status.get("type") == "idle"
            ):
                self._schedule_auto(thread_id)
        elif method in {"turn/completed", "turn/failed"}:
            self._schedule_auto(thread_id)

    def _schedule_auto(self, thread_id: str) -> None:
        if not self.auto_threshold:
            return
        if (
            self._usage_percent.get(thread_id, 0.0)
            < self.auto_threshold
        ):
            return
        task = self._tasks.get(thread_id)
        if task and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._auto_compact(thread_id))
        self._tasks[thread_id] = task
        task.add_done_callback(
            lambda _completed, tid=thread_id: self._tasks.pop(
                tid,
                None,
            )
        )

    def _has_pending_approval(self, thread_id: str) -> bool:
        for request in self.pending_approvals().values():
            try:
                if self.approval_thread_id(request) == thread_id:
                    return True
            except Exception:
                continue
        return False

    def _queue_depth(self, thread_id: str) -> int:
        try:
            return int(self.queue_depth(thread_id))
        except Exception:
            return 0

    def _active(self, thread_id: str) -> bool:
        try:
            return bool(self.thread_is_active(thread_id))
        except Exception:
            return False

    def _eligible(
        self,
        thread_id: str,
    ) -> tuple[bool, str | None]:
        if thread_id in self._inflight:
            return False, "compaction_in_progress"
        if self._active(thread_id):
            return False, "turn_active"
        if self._queue_depth(thread_id) > 0:
            return False, "queued_turns"
        if self._has_pending_approval(thread_id):
            return False, "approval_pending"
        return True, None

    async def _auto_compact(self, thread_id: str) -> None:
        await asyncio.sleep(0.75)
        if (
            self._usage_percent.get(thread_id, 0.0)
            < self.auto_threshold
        ):
            return
        last = self._last_compacted_at.get(thread_id, 0.0)
        if (
            self.cooldown_seconds
            and time.time() - last < self.cooldown_seconds
        ):
            return
        eligible, _ = self._eligible(thread_id)
        if not eligible:
            return
        try:
            await self.compact(thread_id, reason="auto")
        except Exception:
            return

    async def compact(
        self,
        thread_id: str,
        *,
        reason: str = "manual",
    ) -> dict[str, Any]:
        self.raise_if_thread_replaced(thread_id)
        eligible, blocked_reason = self._eligible(thread_id)
        if not eligible:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "context_compaction_blocked",
                    "reason": blocked_reason,
                    "threadId": thread_id,
                },
            )

        self._inflight.add(thread_id)
        started_at = time.time()
        await self.publish_event(
            {
                "type": "context.compaction.started",
                "threadId": thread_id,
                "reason": reason,
                "usagePercent": self._usage_percent.get(
                    thread_id
                ),
            }
        )
        try:
            result = (
                await CodexAgentRuntimeAdapter(
                    _ThreadRuntimeTransport(
                        self.request_for_thread,
                        thread_id,
                    )
                ).compact_session(thread_id)
            ).payload
        except Exception as exc:
            await self.publish_event(
                {
                    "type": "context.compaction.failed",
                    "threadId": thread_id,
                    "reason": reason,
                    "error": str(exc),
                }
            )
            raise
        finally:
            self._inflight.discard(thread_id)

        self._last_compacted_at[thread_id] = time.time()
        previous_percent = self._usage_percent.pop(
            thread_id,
            None,
        )
        await self.publish_event(
            {
                "type": "context.compaction.accepted",
                "threadId": thread_id,
                "reason": reason,
                "previousUsagePercent": previous_percent,
                "durationMs": round(
                    (time.time() - started_at) * 1000
                ),
            }
        )
        return {
            "ok": True,
            "threadId": thread_id,
            "reason": reason,
            "previousUsagePercent": previous_percent,
            "result": result,
        }

    def status(self, thread_id: str) -> dict[str, Any]:
        self.raise_if_thread_replaced(thread_id)
        eligible, blocked_reason = self._eligible(thread_id)
        usage_percent = self._usage_percent.get(thread_id)
        return {
            "threadId": thread_id,
            "autoEnabled": bool(self.auto_threshold),
            "autoThresholdPercent": self.auto_threshold or None,
            "cooldownSeconds": self.cooldown_seconds,
            "usagePercent": (
                round(usage_percent, 1)
                if usage_percent is not None
                else None
            ),
            "eligible": eligible,
            "blockedReason": blocked_reason,
            "inProgress": thread_id in self._inflight,
            "lastCompactedAt": self._last_compacted_at.get(
                thread_id
            ),
        }
