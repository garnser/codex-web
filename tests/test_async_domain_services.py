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


class _BotHost:
    def __init__(self) -> None:
        self.worker_thread_id: int | None = None

    def _project(self, project_id: str):
        return SimpleNamespace(id=project_id)

    def _bot_channels(self, project_id: str):
        self.worker_thread_id = threading.get_ident()
        return [{"provider": "slack", "id": "C123", "name": project_id}]


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
    async def test_bot_channel_discovery_runs_off_event_loop(self) -> None:
        host = _BotHost()
        service = BotService(host)
        event_loop_thread = threading.get_ident()

        channels = await service.list_channels("home")

        self.assertEqual(channels[0]["id"], "C123")
        self.assertIsNotNone(host.worker_thread_id)
        self.assertNotEqual(host.worker_thread_id, event_loop_thread)

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
