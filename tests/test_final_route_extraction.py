from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_web.api.telegram import build_telegram_router
from codex_web.services.runtime import RuntimeService


class _Codex:
    async def ensure_started(self) -> None:
        return None


class RuntimeRecoveryTests(unittest.TestCase):
    def test_recovery_resume_is_owned_by_runtime_service(self) -> None:
        async def run() -> None:
            now = time.time()
            resumed: list[set[str]] = []
            drained: list[str] = []
            active = {
                "stale": SimpleNamespace(updated_at=now - 500),
                "fresh": SimpleNamespace(updated_at=now - 5),
            }
            queues = {"stale": [1, 2], "queued": [3]}

            class Host:
                codex = _Codex()

                def _load_active_turns(self):
                    return active

                def _active_turn_stale_seconds(self):
                    return 120.0

                async def _resume_active_threads_after_startup(self, thread_ids):
                    resumed.append(set(thread_ids))

                def _load_turn_queues(self):
                    return queues

                def _schedule_queue_drain(self, thread_id):
                    drained.append(thread_id)

            host = Host()
            service = RuntimeService(host)
            result = await service.recovery_resume()
            await asyncio.sleep(0)

            self.assertEqual(result["resumingStaleThreads"], ["stale"])
            self.assertEqual(result["activeTurns"], 2)
            self.assertEqual(result["queuedTurns"], 3)
            self.assertEqual(resumed, [{"stale"}])
            self.assertEqual(drained, ["stale", "queued"])
            self.assertIs(host.recovery_resume.__self__, service)

        asyncio.run(run())


class TelegramRouteTests(unittest.TestCase):
    def test_webhook_maps_update_into_canonical_bot_inbound_message(self) -> None:
        verified: list[bool] = []
        captured = []

        class Host:
            def _verify_telegram_secret(self, request):
                verified.append(True)

        class Routing:
            async def handle_inbound(self, message):
                captured.append(message)
                return {"ok": True, "threadId": "thread-123"}

        app = FastAPI()
        app.include_router(build_telegram_router(Host(), Routing()))
        response = TestClient(app).post(
            "/bots/telegram/webhook",
            json={
                "message": {
                    "message_id": 42,
                    "text": "  hello  ",
                    "chat": {"id": -1001, "title": "Engineering"},
                    "from": {"id": 7, "username": "jon"},
                }
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "accepted": True, "threadId": "thread-123"})
        self.assertEqual(verified, [True])
        self.assertEqual(len(captured), 1)
        message = captured[0]
        self.assertEqual(message.provider, "telegram")
        self.assertEqual(message.external_conversation_id, "-1001")
        self.assertEqual(message.external_name, "Engineering")
        self.assertEqual(message.sender_id, "7")
        self.assertEqual(message.sender_name, "jon")
        self.assertEqual(message.text, "hello")
        self.assertEqual(message.message_id, "42")

    def test_webhook_ignores_non_message_updates(self) -> None:
        class Host:
            def _verify_telegram_secret(self, request):
                return None

        class Routing:
            async def handle_inbound(self, message):
                raise AssertionError("ignored updates must not be routed")

        app = FastAPI()
        app.include_router(build_telegram_router(Host(), Routing()))
        response = TestClient(app).post("/bots/telegram/webhook", json={"update_id": 1})
        self.assertEqual(response.json(), {"ok": True, "ignored": True})


if __name__ == "__main__":
    unittest.main()
