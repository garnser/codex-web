from __future__ import annotations

import asyncio
import unittest
from typing import Any

from fastapi import HTTPException

from codex_web.events import EventHub
from codex_web.services.context import ContextCompactionService


class FakeHub:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def publish(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class FakeCodex:
    def __init__(self) -> None:
        self.pending_approvals: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((method, params))
        return {"started": True}


class FakeHost:
    def __init__(self) -> None:
        self.hub = FakeHub()
        self.codex = FakeCodex()
        self.active = False
        self.queue_depth = 0

    def _raise_if_thread_replaced(self, thread_id: str) -> None:
        return None

    def _thread_is_active(self, thread_id: str) -> bool:
        return self.active

    def _thread_queue_depth(self, thread_id: str) -> int:
        return self.queue_depth

    def _approval_thread_id(self, request: dict[str, Any]) -> str | None:
        return request.get("threadId")


class ContextCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_compaction_uses_native_codex_method(self) -> None:
        host = FakeHost()
        service = ContextCompactionService(host)
        service._usage_percent["thread-1"] = 81.5

        result = await service.compact("thread-1")

        self.assertTrue(result["ok"])
        self.assertEqual(
            host.codex.requests,
            [("thread/compact/start", {"threadId": "thread-1"})],
        )
        self.assertEqual(host.hub.events[0]["type"], "context.compaction.started")
        self.assertEqual(host.hub.events[-1]["type"], "context.compaction.accepted")
        self.assertNotIn("thread-1", service._usage_percent)

    async def test_compaction_is_blocked_during_active_turn(self) -> None:
        host = FakeHost()
        host.active = True
        service = ContextCompactionService(host)

        with self.assertRaises(HTTPException) as raised:
            await service.compact("thread-1")

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["reason"], "turn_active")
        self.assertEqual(host.codex.requests, [])

    async def test_compaction_is_blocked_when_approval_is_pending(self) -> None:
        host = FakeHost()
        host.codex.pending_approvals["approval-1"] = {"threadId": "thread-1"}
        service = ContextCompactionService(host)

        with self.assertRaises(HTTPException) as raised:
            await service.compact("thread-1")

        self.assertEqual(raised.exception.detail["reason"], "approval_pending")

    async def test_high_token_usage_auto_compacts_when_idle(self) -> None:
        host = FakeHost()
        service = ContextCompactionService(host)
        service.auto_threshold = 75.0
        service.cooldown_seconds = 0.0

        service.observe(
            {
                "type": "codex.event",
                "message": {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": "thread-1",
                        "tokenUsage": {
                            "total": {"totalTokens": 800},
                            "modelContextWindow": 1000,
                        },
                    },
                },
            }
        )

        await asyncio.sleep(0.9)

        self.assertEqual(
            host.codex.requests,
            [("thread/compact/start", {"threadId": "thread-1"})],
        )
        self.assertEqual(host.hub.events[-1]["reason"], "auto")

    async def test_event_hub_notifies_runtime_observers(self) -> None:
        hub = EventHub()
        seen: list[dict[str, Any]] = []
        hub.subscribe(seen.append)

        await hub.publish({"type": "test.event", "value": 1})
        self.assertEqual(seen, [{"type": "test.event", "value": 1}])

        hub.unsubscribe(seen.append)
        await hub.publish({"type": "test.event", "value": 2})
        self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
