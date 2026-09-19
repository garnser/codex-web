from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any

from codex_web.services.bot_binding_selection import BotBindingSelectionService
from codex_web.storage.thread_index import ThreadIndexRepository


RuntimeRequest = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
EventSink = Callable[[dict[str, Any]], None]
TextTruncator = Callable[[str, int], str]


class ThreadResumeService:
    """Own web resume coordination and thread timeout/error shaping."""

    def __init__(
        self,
        runtime_request: RuntimeRequest,
        thread_index: ThreadIndexRepository,
        bindings: BotBindingSelectionService,
        *,
        event_sink: EventSink,
        truncate_text: TextTruncator,
    ) -> None:
        self.runtime_request = runtime_request
        self.thread_index = thread_index
        self.bindings = bindings
        self.event_sink = event_sink
        self.truncate_text = truncate_text
        self.tasks: dict[str, asyncio.Task[dict[str, Any]]] = {}

    @staticmethod
    def is_stale_thread_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "no rollout found for thread id",
                "thread not found",
                "already has an active writer",
                "thread-store conflict",
            )
        )

    @staticmethod
    def is_timeout_error(exc: Exception) -> bool:
        detail = getattr(exc, "detail", None)
        text = str(detail or exc).lower()
        return (
            getattr(exc, "status_code", None) == 504
            or "timed out after" in text
        )

    @staticmethod
    def handoff_timeout() -> float:
        try:
            return max(
                0.1,
                float(
                    os.environ.get("CODEX_WEB_RESUME_HANDOFF_TIMEOUT")
                    or "3"
                ),
            )
        except ValueError:
            return 3.0

    @staticmethod
    def retry_delay() -> float:
        try:
            return max(
                1.0,
                float(os.environ.get("CODEX_WEB_RESUME_RETRY_DELAY") or "30"),
            )
        except ValueError:
            return 30.0

    def active_task(self, thread_id: str) -> asyncio.Task[dict[str, Any]] | None:
        task = self.tasks.get(thread_id)
        return task if task is not None and not task.done() else None

    def read_timeout_response(
        self,
        thread_id: str,
        limit: int,
        exc: Exception | str,
        *,
        event_type: str = "web_read_timeout",
    ) -> dict[str, Any]:
        indexed = next(
            (
                thread
                for thread in self.thread_index.load()
                if thread.id == thread_id
            ),
            None,
        )
        bindings = self.bindings.for_thread(thread_id)
        binding = bindings[0] if bindings else None
        thread = {
            "id": thread_id,
            "name": (
                (indexed.name if indexed else None)
                or (binding.thread_name if binding else None)
                or "Untitled thread"
            ),
            "cwd": indexed.cwd if indexed else None,
            "path": indexed.path if indexed else None,
            "turns": [],
            "status": {"type": "notLoaded"},
            "messageLimit": limit,
            "readTimedOut": True,
        }
        error = (
            exc
            if isinstance(exc, str)
            else str(getattr(exc, "detail", exc))
        )
        self.event_sink(
            {
                "type": event_type,
                "thread_id": thread_id,
                "error": self.truncate_text(error, 500),
            }
        )
        return {
            "ok": False,
            "timedOut": True,
            "threadId": thread_id,
            "error": error,
            "thread": thread,
        }

    async def _run(
        self,
        thread_id: str,
        project_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return await self.runtime_request("thread/resume", params)
        except Exception as exc:
            payload = {
                "thread_id": thread_id,
                "project_id": project_id,
                "error": self.truncate_text(
                    str(getattr(exc, "detail", exc)),
                    500,
                ),
            }
            if self.is_timeout_error(exc):
                self.event_sink({"type": "web_resume_timeout", **payload})
                return {
                    "ok": False,
                    "timedOut": True,
                    "threadId": thread_id,
                    "error": str(getattr(exc, "detail", exc)),
                }
            self.event_sink({"type": "web_resume_failed", **payload})
            return {
                "ok": False,
                "threadId": thread_id,
                "error": str(getattr(exc, "detail", exc)),
            }
        finally:
            current = asyncio.current_task()
            if self.tasks.get(thread_id) is current:
                self.tasks.pop(thread_id, None)

    def schedule(
        self,
        thread_id: str,
        project_id: str,
        params: dict[str, Any],
    ) -> tuple[asyncio.Task[dict[str, Any]], bool]:
        existing = self.active_task(thread_id)
        if existing is not None:
            return existing, False
        task = asyncio.create_task(self._run(thread_id, project_id, params))
        self.tasks[thread_id] = task
        return task, True
