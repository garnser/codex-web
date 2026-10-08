from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from fastapi import HTTPException

from codex_web.agent_runtime import AgentRuntimeEvent
from codex_web.runtime.execution import TurnExecutionService
from codex_web.services.threads import ThreadService
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.thread_history import ThreadHistoryRepository


class _Recovery:
    @staticmethod
    def raise_if_thread_replaced(_thread_id: str) -> None:
        return None


class _Resume:
    @staticmethod
    def active_task(_thread_id: str):
        return None

    @staticmethod
    def is_timeout_error(_exc: Exception) -> bool:
        return False


class ThreadHistoryProjectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = SQLiteStateStore(self.root / "state.sqlite3")
        self.history = ThreadHistoryRepository(self.store)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _service(self, request_for_thread) -> ThreadService:
        return ThreadService(
            runtime_transport=object(),
            runtime_request_for_thread=request_for_thread,
            event_sink=lambda _event: None,
            recovery=_Recovery(),
            resume_runtime=_Resume(),
            thread_history=self.history,
        )

    def test_provider_neutral_user_and_assistant_messages_survive_reload(self) -> None:
        self.history.start_turn(
            "thread-web",
            turn_id="execution-1",
            message="Please fix the regression.",
            provider_id="openai",
            runtime_id="codex-cli",
            created_at=10.0,
        )
        service = TurnExecutionService(
            SimpleNamespace(_load_active_turns=lambda: {}),
            thread_history=self.history,
        )
        service.record_agent_runtime_event(
            "thread-web",
            AgentRuntimeEvent(
                event_type="item.completed",
                provider_native_session_id="native-1",
                provider_native_turn_id="turn-1",
                payload={
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "The regression is fixed.",
                    },
                },
            ),
        )

        reloaded = ThreadHistoryRepository(self.store).thread("thread-web")

        self.assertIsNotNone(reloaded)
        turn = reloaded["turns"][0]
        self.assertEqual(turn["providerTurnId"], "turn-1")
        self.assertEqual(
            [item["type"] for item in turn["items"]],
            ["userMessage", "agentMessage"],
        )
        self.assertEqual(
            turn["items"][0]["content"][0]["text"],
            "Please fix the regression.",
        )
        self.assertEqual(
            turn["items"][1]["text"],
            "The regression is fixed.",
        )

    def test_projection_is_bounded_to_the_newest_turns(self) -> None:
        self.history.MAX_TURNS = 2
        for index in range(3):
            self.history.start_turn(
                "thread-web",
                turn_id=f"execution-{index}",
                message=f"Prompt {index}",
                provider_id="openai",
                runtime_id="codex-cli",
                created_at=float(index),
            )
            self.history.project_message(
                "thread-web",
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-web",
                        "turnId": f"execution-{index}",
                    },
                },
                recorded_at=float(index) + 0.5,
            )

        projected = self.history.thread("thread-web")

        self.assertEqual(
            [turn["id"] for turn in projected["turns"]],
            ["execution-1", "execution-2"],
        )

    async def test_sessionless_runtime_read_returns_canonical_history(self) -> None:
        self.history.start_turn(
            "thread-web",
            turn_id="execution-1",
            message="Keep this message.",
            provider_id="openai",
            runtime_id="codex-cli",
        )

        async def request_for_thread(_thread_id, _method, _params):
            raise HTTPException(
                status_code=503,
                detail="assignment-bound runtime session is unavailable",
            )

        response = await self._service(request_for_thread).read("thread-web")

        self.assertEqual(response["thread"]["historySource"], "canonical")
        self.assertEqual(
            response["thread"]["turns"][0]["items"][0]["content"][0]["text"],
            "Keep this message.",
        )

    async def test_cli_metadata_read_uses_canonical_turns(self) -> None:
        self.history.start_turn(
            "thread-web",
            turn_id="execution-1",
            message="Persisted prompt.",
            provider_id="openai",
            runtime_id="codex-cli",
        )

        async def request_for_thread(_thread_id, method, _params):
            self.assertEqual(method, "thread/read")
            return {
                "id": "thread-web",
                "active": False,
                "known": False,
                "source": "codex-cli",
            }

        response = await self._service(request_for_thread).read("thread-web")

        self.assertEqual(response["thread"]["historySource"], "canonical")
        self.assertEqual(len(response["thread"]["turns"]), 1)

    async def test_authorization_error_is_not_masked_by_canonical_history(self) -> None:
        self.history.start_turn(
            "thread-web",
            turn_id="execution-1",
            message="Authorized history does not bypass runtime authority.",
            provider_id="openai",
            runtime_id="codex-cli",
        )

        async def request_for_thread(_thread_id, _method, _params):
            raise HTTPException(status_code=403, detail="denied")

        with self.assertRaises(HTTPException) as raised:
            await self._service(request_for_thread).read("thread-web")

        self.assertEqual(raised.exception.status_code, 403)

    async def test_degraded_empty_runtime_read_uses_canonical_turns(self) -> None:
        self.history.start_turn(
            "thread-web",
            turn_id="execution-1",
            message="Persisted during startup.",
            provider_id="openai",
            runtime_id="codex-cli",
        )

        async def request_for_thread(_thread_id, _method, _params):
            return {
                "ok": False,
                "thread": {
                    "id": "thread-web",
                    "turns": [],
                    "status": {"type": "notLoaded"},
                    "readTimedOut": True,
                },
            }

        response = await self._service(request_for_thread).read("thread-web")

        self.assertEqual(response["thread"]["historySource"], "canonical")
        self.assertEqual(len(response["thread"]["turns"]), 1)

    async def test_native_codex_turn_history_remains_authoritative(self) -> None:
        self.history.start_turn(
            "thread-web",
            turn_id="execution-1",
            message="Projected prompt.",
            provider_id="openai",
            runtime_id="codex-cli",
        )
        native = {
            "thread": {
                "id": "thread-web",
                "turns": [
                    {
                        "id": "native-turn",
                        "items": [
                            {
                                "id": "native-item",
                                "type": "agentMessage",
                                "text": "native",
                            }
                        ],
                    }
                ],
            }
        }

        async def request_for_thread(_thread_id, _method, _params):
            return native

        response = await self._service(request_for_thread).read("thread-web")

        self.assertEqual(response["thread"]["turns"][0]["id"], "native-turn")
        self.assertNotIn("historySource", response["thread"])


if __name__ == "__main__":
    unittest.main()
