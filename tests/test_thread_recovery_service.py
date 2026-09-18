from __future__ import annotations

import asyncio
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, HTTPException

from codex_web.models import BotBinding, Project, ThreadRunSettings
from codex_web.services.thread_recovery import ThreadRecoveryService, install_thread_recovery_service


class ThreadRecoveryServiceTests(unittest.TestCase):
    def test_replacement_chain_is_collapsed_to_latest_thread(self) -> None:
        host = SimpleNamespace(
            THREAD_REPLACEMENTS={"thread-a": "thread-b", "thread-b": "thread-c"},
            THREAD_TERMINAL_FAILURES={},
        )
        service = ThreadRecoveryService(host)

        self.assertEqual(service.replacement_thread_id("thread-a"), "thread-c")
        self.assertEqual(service.replacement_thread_id("thread-b"), "thread-c")
        self.assertIsNone(service.replacement_thread_id("thread-c"))

    def test_replaced_thread_raises_structured_conflict(self) -> None:
        host = SimpleNamespace(
            THREAD_REPLACEMENTS={"old": "new"},
            THREAD_TERMINAL_FAILURES={},
        )
        service = ThreadRecoveryService(host)

        with self.assertRaises(HTTPException) as raised:
            service.raise_if_thread_replaced("old")

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "thread_replaced")
        self.assertEqual(raised.exception.detail["newThreadId"], "new")

    def test_active_turn_stale_timeout_defaults_invalid_and_clamps(self) -> None:
        service = ThreadRecoveryService(SimpleNamespace())

        with patch.dict(os.environ, {"CODEX_WEB_ACTIVE_TURN_STALE_SECONDS": ""}, clear=False):
            self.assertEqual(service.active_turn_stale_seconds(), 120.0)
        with patch.dict(os.environ, {"CODEX_WEB_ACTIVE_TURN_STALE_SECONDS": "bad"}, clear=False):
            self.assertEqual(service.active_turn_stale_seconds(), 120.0)
        with patch.dict(os.environ, {"CODEX_WEB_ACTIVE_TURN_STALE_SECONDS": "5"}, clear=False):
            self.assertEqual(service.active_turn_stale_seconds(), 30.0)
        with patch.dict(os.environ, {"CODEX_WEB_ACTIVE_TURN_STALE_SECONDS": "240"}, clear=False):
            self.assertEqual(service.active_turn_stale_seconds(), 240.0)

    def test_active_turn_stale_detection_and_release_preserve_legacy_behavior(self) -> None:
        active_turns = {"thread-1": SimpleNamespace(updated_at=800.0)}
        append_event = Mock()
        clear_active = Mock()
        host = SimpleNamespace(
            _load_active_turns=lambda: active_turns,
            _append_bot_event=append_event,
            _clear_thread_active=clear_active,
        )
        service = ThreadRecoveryService(host)

        with (
            patch("codex_web.services.thread_recovery.time.time", return_value=1000.0),
            patch.dict(os.environ, {"CODEX_WEB_ACTIVE_TURN_STALE_SECONDS": "120"}, clear=False),
        ):
            self.assertTrue(service.active_turn_is_stale("thread-1"))
            self.assertFalse(service.active_turn_is_stale(None))
            self.assertFalse(service.active_turn_is_stale("missing"))
            self.assertFalse(service.active_turn_is_stale("thread-1", max_age=250.0))
            service.release_stale_active_turn("thread-1", "queue-recovery")

        append_event.assert_called_once_with(
            {
                "type": "stale_active_turn_released",
                "thread_id": "thread-1",
                "reason": "queue-recovery",
            }
        )
        clear_active.assert_called_once_with("thread-1")

    def test_installer_rebinds_recovery_compatibility_entrypoints(self) -> None:
        app = FastAPI()
        host = SimpleNamespace()

        service = install_thread_recovery_service(app, host)

        self.assertIs(app.state.thread_recovery_service, service)
        self.assertIs(host._replace_stale_bot_thread.__self__, service)
        self.assertIs(host._replace_stale_web_thread.__self__, service)
        self.assertIs(host._retarget_bot_thread_state.__self__, service)
        self.assertIs(host._replacement_thread_id.__self__, service)
        self.assertIs(host._raise_if_thread_replaced.__self__, service)
        self.assertIs(host._active_turn_stale_seconds.__self__, service)
        self.assertIs(host._active_turn_is_stale.__self__, service)
        self.assertIs(host._release_stale_active_turn.__self__, service)


class ThreadRecoveryReplacementTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_replacement_preserves_variant_fallback_and_compat_seams(self) -> None:
        binding = BotBinding(
            id="binding-1",
            provider="slack",
            external_conversation_id="C1",
            thread_id="old-thread",
            project_id="home",
            thread_name="James",
            route_prefix="James",
            sandbox="danger-full-access",
            approval_policy="never",
            created_at=1.0,
            updated_at=1.0,
        )
        project = Project(
            id="home",
            name="Home",
            path="/tmp/project",
            sandbox="danger-full-access",
            approval_policy="never",
        )
        calls: list[dict] = []

        async def request(method: str, params: dict):
            self.assertEqual(method, "thread/start")
            calls.append(params)
            if params.get("sessionStartSource") == "bot-thread-replacement":
                raise RuntimeError("unknown variant `bot-thread-replacement`")
            return {"thread": {"id": "new-thread"}}

        replacement = binding.model_copy(update={"thread_id": "new-thread"})
        retarget_bindings = Mock(return_value=replacement)
        retarget_state = Mock()
        archive = AsyncMock(return_value=True)
        publish = AsyncMock()
        host = SimpleNamespace(
            THREAD_REPLACEMENTS={},
            THREAD_TERMINAL_FAILURES={},
            codex=SimpleNamespace(request=request),
            hub=SimpleNamespace(publish=publish),
            _project=lambda project_id: project,
            _thread_run_settings=lambda thread_id: ThreadRunSettings(),
            _project_params=lambda _project, values: values,
            _binding_prefix=lambda item: item.route_prefix or item.thread_id,
            _remember_thread_run_settings=Mock(),
            _set_thread_name=AsyncMock(),
            _upsert_indexed_thread=Mock(),
            _retarget_logical_bot_bindings=retarget_bindings,
            _retarget_bot_thread_state=retarget_state,
            _archive_replaced_bot_thread=archive,
            _logical_binding_name=lambda item: (item.route_prefix or item.thread_id).lower(),
            _append_bot_event=Mock(),
            _truncate_text=lambda text, limit=500: text[:limit],
        )
        service = ThreadRecoveryService(host)

        result = await service.replace_stale_bot_thread(binding, "thread not found")

        self.assertEqual(result.thread_id, "new-thread")
        self.assertEqual(
            [call["sessionStartSource"] for call in calls],
            ["bot-thread-replacement", "startup"],
        )
        retarget_bindings.assert_called_once()
        retarget_state.assert_called_once_with("old-thread", "new-thread")
        archive.assert_awaited_once_with("old-thread", "new-thread")
        self.assertEqual(host.THREAD_REPLACEMENTS["old-thread"], "new-thread")


if __name__ == "__main__":
    unittest.main()
