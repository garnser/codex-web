from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

from codex_web import application
from codex_web.models import QueuedTurn, ThreadRunSettings, TurnCreate
from codex_web.services.turns import TurnService


def _path_tags(path: str) -> set[str]:
    operations = application.app.openapi().get("paths", {}).get(path, {})
    return {
        tag
        for operation in operations.values()
        if isinstance(operation, dict)
        for tag in operation.get("tags", [])
    }


class _TurnHost:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.published: list[str] = []
        self.remembered: dict | None = None
        self.queued = QueuedTurn(
            id="queued-1",
            thread_id="thread-1",
            project_id="home",
            message="queued message",
            created_at=time.time(),
        )

    def _raise_if_thread_replaced(self, thread_id):
        return None

    def _project(self, project_id):
        return SimpleNamespace(
            id=project_id or "home",
            sandbox="workspace-write",
            approval_policy="on-request",
            model=None,
        )

    def _thread_run_settings(self, thread_id):
        return ThreadRunSettings()

    def _effective_developer_instructions(self, thread_id, value):
        return value

    def _remember_thread_run_settings(self, thread_id, **kwargs):
        self.remembered = kwargs
        return ThreadRunSettings(**kwargs)

    def _project_params(self, project, params):
        return params

    def _release_stale_active_turn(self, thread_id, source):
        return False

    def _thread_is_active(self, thread_id):
        return True

    def _thread_queue_depth(self, thread_id):
        return 1

    def _enqueue_turn(self, **kwargs):
        self.queued = QueuedTurn(
            id="queued-1",
            thread_id=kwargs["thread_id"],
            project_id=kwargs["project_id"],
            message=kwargs["message"],
            sandbox=kwargs["sandbox"],
            approval_policy=kwargs["approval_policy"],
            model=kwargs["model"],
            reasoning_effort=kwargs["reasoning_effort"],
            created_at=time.time(),
        )
        return self.queued

    def _append_bot_event(self, event):
        self.events.append(event)

    async def _publish_queue_status(self, thread_id):
        self.published.append(thread_id)

    def _truncate_text(self, value, limit):
        return str(value)[:limit]

    def _thread_queue(self, thread_id):
        return [self.queued]


class TurnServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_turn_routes_are_owned_by_turn_domain(self) -> None:
        self.assertIn("turns", _path_tags("/api/threads/{thread_id}/turns"))
        self.assertIn("turns", _path_tags("/api/threads/{thread_id}/queue"))
        self.assertGreater(application.EXTRACTED_ROUTE_COUNTS["turns"], 0)

    async def test_non_forced_resume_is_a_read_only_noop(self) -> None:
        service = TurnService(_TurnHost())

        result = await service.resume(
            "thread-1",
            project_id="home",
            sandbox=None,
            approval_policy=None,
            model=None,
            reasoning_effort=None,
            force_resume=False,
        )

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "web_load_uses_thread_read")

    async def test_start_queues_when_thread_is_already_active(self) -> None:
        host = _TurnHost()
        service = TurnService(host)

        result = await service.start("thread-1", TurnCreate(message="next task", project_id="home"))

        self.assertTrue(result["queued"])
        self.assertEqual(result["queuedId"], "queued-1")
        self.assertEqual(host.queued.message, "next task")
        self.assertEqual(host.events[-1]["type"], "web_turn_queued")
        self.assertEqual(host.published, ["thread-1"])

    def test_queue_snapshot_is_presentational(self) -> None:
        service = TurnService(_TurnHost())

        result = service.queue("thread-1")

        self.assertTrue(result["active"])
        self.assertEqual(result["queueDepth"], 1)
        self.assertEqual(result["queued"][0]["id"], "queued-1")


if __name__ == "__main__":
    unittest.main()
