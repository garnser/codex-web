from __future__ import annotations

import asyncio
import threading
import unittest
from types import SimpleNamespace

from codex_web.services.bots import BotService
from codex_web.services.work_items import WorkItemService


class _Hub:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish(self, event: dict) -> None:
        self.events.append(event)


class _SlackClient:
    def __init__(self) -> None:
        self.thread_id: int | None = None
        self.tokens: list[str] = []

    async def list_channels(self, token: str) -> list[dict[str, str]]:
        await asyncio.sleep(0)
        self.thread_id = threading.get_ident()
        self.tokens.append(token)
        return [{"provider": "slack", "id": "C123", "name": "general", "label": "#general"}]

    async def channel_info(self, token: str, channel_id: str):
        await asyncio.sleep(0)
        return None


class _BotHost:
    def __init__(self) -> None:
        self.BOT_CHANNEL_CACHE = {}
        self.connections = [
            SimpleNamespace(
                project_id="home",
                provider="slack",
                bot_token="xoxb-test",
            )
        ]

    def _project(self, project_id: str):
        return SimpleNamespace(id=project_id)

    def _known_bot_channels(self, project_id: str):
        return []

    def _load_bot_connections(self):
        return self.connections

    def _channel_needs_name(self, channel):
        return not channel.get("name")

    def _bot_channels(self, project_id: str):
        raise AssertionError("legacy synchronous channel discovery must not be used")


class _WorkItemHost:
    def __init__(self) -> None:
        self.worker_thread_id: int | None = None
        self.progress_thread_id: int | None = None
        self.GITLAB_SYNC_CONSECUTIVE_FAILURES = 0
        self.GITLAB_SYNC_LAST_ERROR = None
        self.GITLAB_SYNC_LAST_ERROR_AT = 0.0
        self.GITLAB_SYNC_LAST_SUCCESS_AT = 0.0
        self.hub = _Hub()
        self.scheduled: list[str] = []

    def _sync_work_item_states_from_gitlab(self):
        self.worker_thread_id = threading.get_ident()
        return {"synced": 2}

    def _structured_progress(self, ref, payload):
        self.progress_thread_id = threading.get_ident()
        return SimpleNamespace(ref=ref)

    def _work_item_state_public(self, state):
        return {"ref": state.ref}

    def _schedule_actionable_owner_dispatch(self, state, *, source, actor):
        self.scheduled.append(source)

    def _schedule_actionable_owner_continuity_check(self, state, *, source):
        self.scheduled.append(source)

    def _work_item_split_brain_findings(self, state):
        return []

    def _truncate_text(self, value, limit):
        return str(value)[:limit]

    def _append_bot_event(self, event):
        pass


class AsyncDomainServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_channel_discovery_uses_async_client_not_legacy_sync_helper(self) -> None:
        host = _BotHost()
        slack = _SlackClient()
        service = BotService(host, slack_client=slack)
        event_loop_thread = threading.get_ident()

        channels = await service.list_channels("home")

        self.assertEqual(channels[0]["id"], "C123")
        self.assertEqual(slack.thread_id, event_loop_thread)
        self.assertEqual(slack.tokens, ["xoxb-test"])
        self.assertEqual(host.BOT_CHANNEL_CACHE["home"][1], channels)

    async def test_gitlab_work_item_sync_runs_off_event_loop(self) -> None:
        host = _WorkItemHost()
        service = WorkItemService(host)
        event_loop_thread = threading.get_ident()

        result = await service.sync_from_gitlab()

        self.assertEqual(result, {"ok": True, "synced": 2})
        self.assertIsNotNone(host.worker_thread_id)
        self.assertNotEqual(host.worker_thread_id, event_loop_thread)
        self.assertEqual(host.hub.events[-1]["type"], "work-item.sync")

    async def test_progress_state_machine_runs_off_event_loop(self) -> None:
        host = _WorkItemHost()
        service = WorkItemService(host)
        event_loop_thread = threading.get_ident()
        payload = SimpleNamespace(actor="dana")

        result = await service.progress("group/project#1", payload)

        self.assertEqual(result["item"]["ref"], "group/project#1")
        self.assertIsNotNone(host.progress_thread_id)
        self.assertNotEqual(host.progress_thread_id, event_loop_thread)
        self.assertIn("work-item-progress", host.scheduled)
        self.assertEqual(host.hub.events[-1]["type"], "work-item.progress")


if __name__ == "__main__":
    unittest.main()
