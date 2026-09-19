from __future__ import annotations

import contextlib
import time
from collections.abc import Awaitable, Callable

from codex_web.models import BotBinding, IndexedThread
from codex_web.storage.thread_index import ThreadIndexRepository


RuntimeRequest = Callable[[str, dict[str, object]], Awaitable[dict[str, object]]]
BindingLoader = Callable[[], list[BotBinding]]
EventSink = Callable[[dict[str, object]], None]


class ThreadNamingService:
    """Own canonical thread naming and bot-name restoration."""

    def __init__(
        self,
        runtime_request: RuntimeRequest,
        thread_index: ThreadIndexRepository,
        load_bindings: BindingLoader,
        *,
        event_sink: EventSink,
    ) -> None:
        self.runtime_request = runtime_request
        self.thread_index = thread_index
        self.load_bindings = load_bindings
        self.event_sink = event_sink

    async def set_name(self, thread_id: str, name: str) -> dict[str, object]:
        response = await self.runtime_request(
            "thread/name/set",
            {"threadId": thread_id, "name": name},
        )
        indexed = IndexedThread(
            id=thread_id,
            name=name,
            updatedAt=time.time(),
        )
        with contextlib.suppress(Exception):
            thread_response = await self.runtime_request(
                "thread/read",
                {"threadId": thread_id, "includeTurns": False},
            )
            thread = (
                thread_response.get("thread", thread_response)
                if isinstance(thread_response, dict)
                else {}
            )
            indexed = IndexedThread(
                id=thread_id,
                name=name,
                cwd=thread.get("cwd"),
                path=thread.get("path"),
                updatedAt=thread.get("updatedAt") or time.time(),
            )
        self.thread_index.upsert(indexed)
        return response

    def canonical_bot_names(self) -> dict[str, str]:
        grouped: dict[str, list[BotBinding]] = {}
        for binding in self.load_bindings():
            if binding.thread_name:
                grouped.setdefault(binding.thread_id, []).append(binding)
        names: dict[str, str] = {}
        for thread_id, bindings in grouped.items():
            bindings.sort(
                key=lambda binding: (
                    binding.created_at,
                    binding.updated_at,
                )
            )
            for binding in bindings:
                name = (binding.thread_name or "").strip()
                if name:
                    names[thread_id] = name
                    break
        return names

    async def restore(self, thread_id: str | None) -> None:
        if not thread_id:
            return
        name = self.canonical_bot_names().get(thread_id)
        if not name:
            return
        try:
            thread_response = await self.runtime_request(
                "thread/read",
                {"threadId": thread_id, "includeTurns": False},
            )
            thread = (
                thread_response.get("thread", thread_response)
                if isinstance(thread_response, dict)
                else {}
            )
            if (thread.get("name") or "").strip() == name:
                return
            await self.set_name(thread_id, name)
            self.event_sink(
                {
                    "type": "bot_thread_name_restored",
                    "thread_id": thread_id,
                    "name": name,
                }
            )
        except Exception as exc:
            self.event_sink(
                {
                    "type": "bot_thread_name_restore_failed",
                    "thread_id": thread_id,
                    "error": str(exc),
                }
            )

    async def restore_all(self) -> None:
        for thread_id in self.canonical_bot_names():
            await self.restore(thread_id)
