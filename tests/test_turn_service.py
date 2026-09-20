from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

from codex_web import application
from codex_web.models import Project, QueuedTurn, ThreadRunSettings, TurnCreate
from codex_web.services.turns import TurnService


def _path_tags(path: str) -> set[str]:
    operations = application.app.openapi().get("paths", {}).get(path, {})
    return {
        tag
        for operation in operations.values()
        if isinstance(operation, dict)
        for tag in operation.get("tags", [])
    }


class _Projects:
    def __init__(self) -> None:
        self.project = Project(
            id="home",
            name="Home",
            path="/tmp/home",
            sandbox="workspace-write",
            approval_policy="on-request",
        )

    def get(self, project_id):
        return self.project

    @staticmethod
    def params(project, overrides=None):
        return {
            "cwd": project.path,
            **{
                key: value
                for key, value in (overrides or {}).items()
                if value is not None
            },
        }


class _Settings:
    def __init__(self) -> None:
        self.remembered = None

    def get(self, thread_id):
        return ThreadRunSettings()

    def remember(self, thread_id, **kwargs):
        self.remembered = kwargs
        return ThreadRunSettings(**kwargs)

    @staticmethod
    def effective_developer_instructions(thread_id, value):
        return value


class _Recovery:
    def raise_if_thread_replaced(self, thread_id):
        return None

    def release_stale_active_turn(self, thread_id, reason):
        return None

    def replacement_thread_id(self, thread_id):
        return None

    async def replace_stale_bot_thread(self, binding, reason):
        return binding

    async def replace_stale_web_thread(self, thread_id, project, reason):
        return "replacement-thread"


class _Resume:
    @staticmethod
    def handoff_timeout():
        return 3.0

    @staticmethod
    def is_timeout_error(exc):
        return False

    @staticmethod
    def is_stale_thread_error(exc):
        return False

    def schedule(self, thread_id, project_id, params):
        raise AssertionError("forced resume was not expected")


class _Bindings:
    @staticmethod
    def for_thread(thread_id):
        return []


class _QueuePolicy:
    def __init__(self) -> None:
        self.queued = [
            QueuedTurn(
                id="queued-1",
                thread_id="thread-1",
                project_id="home",
                message="queued message",
                created_at=time.time(),
            )
        ]

    def queue(self, thread_id):
        return list(self.queued)

    def depth(self, thread_id):
        return len(self.queued)

    def record_steer(self, thread_id):
        return None


class _Execution:
    def __init__(self, queue_policy: _QueuePolicy) -> None:
        self.queue_policy = queue_policy
        self.published = []
        self.active = True

    def enqueue_turn(self, **kwargs):
        queued = QueuedTurn(
            id="queued-1",
            thread_id=kwargs["thread_id"],
            project_id=kwargs["project_id"],
            message=kwargs["message"],
            sandbox=kwargs["sandbox"],
            approval_policy=kwargs["approval_policy"],
            model=kwargs["model"],
            reasoning_effort=kwargs["reasoning_effort"],
            execution_id=kwargs.get("execution_id"),
            repository_resource_id=kwargs.get("repository_resource_id"),
            read_only_repository_resource_ids=kwargs.get(
                "read_only_repository_resource_ids",
                (),
            ),
            created_at=time.time(),
        )
        self.queue_policy.queued = [queued]
        return queued

    async def publish_queue_status(self, thread_id):
        self.published.append(thread_id)

    def thread_is_active(self, thread_id):
        return self.active

    async def start_thread_turn_now(self, thread_id, **kwargs):
        return {"turn": {"id": "turn-1"}}

    def wait_for_thread_capacity(self, **kwargs):
        return None

    def schedule_queue_drain(self, thread_id):
        return None

    def pop_latest_queued_turn(self, thread_id):
        return self.queue_policy.queued.pop() if self.queue_policy.queued else None

    def pop_queued_turn(self, thread_id, queued_id):
        for index, item in enumerate(self.queue_policy.queued):
            if item.id == queued_id:
                return self.queue_policy.queued.pop(index)
        return None

    def requeue_turn_front(self, queued):
        self.queue_policy.queued.insert(0, queued)

    def clear_thread_active(self, thread_id):
        self.active = False

    async def request_for_thread(self, thread_id, method, params=None):
        return {}


class TurnServiceTests(unittest.IsolatedAsyncioTestCase):
    def _service(self):
        queue = _QueuePolicy()
        execution = _Execution(queue)
        events = []
        settings = _Settings()
        service = TurnService(
            projects=_Projects(),
            settings=settings,
            recovery=_Recovery(),
            resume_runtime=_Resume(),
            bindings=_Bindings(),
            queue_policy=queue,
            execution=execution,
            event_sink=events.append,
            truncate_text=lambda value, limit: str(value)[:limit],
            binding_public=lambda binding: binding.model_dump(),
        )
        return service, queue, execution, events, settings

    def test_turn_routes_are_owned_by_turn_domain(self) -> None:
        self.assertIn("turns", _path_tags("/api/threads/{thread_id}/turns"))
        self.assertIn("turns", _path_tags("/api/threads/{thread_id}/queue"))
        self.assertGreater(application.EXTRACTED_ROUTE_COUNTS["turns"], 0)

    async def test_non_forced_resume_is_a_read_only_noop(self) -> None:
        service, _queue, _execution, _events, settings = self._service()

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
        self.assertEqual(settings.remembered["sandbox"], "workspace-write")

    async def test_start_queues_when_thread_is_already_active(self) -> None:
        service, queue, execution, events, _settings = self._service()

        result = await service.start(
            "thread-1",
            TurnCreate(message="next task", project_id="home"),
        )

        self.assertTrue(result["queued"])
        self.assertEqual(result["queuedId"], "queued-1")
        self.assertEqual(queue.queued[0].message, "next task")
        self.assertEqual(events[-1]["type"], "web_turn_queued")
        self.assertEqual(execution.published, ["thread-1"])

    async def test_start_queues_repository_target_with_message(self) -> None:
        service, queue, _execution, _events, _settings = self._service()

        result = await service.start(
            "thread-1",
            TurnCreate(
                message="next task",
                project_id="home",
                repository_resource_id="repo-app",
                read_only_repository_resource_ids=("repo-docs",),
            ),
        )

        self.assertTrue(result["queued"])
        self.assertEqual(queue.queued[0].repository_resource_id, "repo-app")
        self.assertEqual(
            queue.queued[0].read_only_repository_resource_ids,
            ("repo-docs",),
        )

    def test_queue_snapshot_is_presentational(self) -> None:
        service, _queue, _execution, _events, _settings = self._service()

        result = service.queue("thread-1")

        self.assertTrue(result["active"])
        self.assertEqual(result["queueDepth"], 1)
        self.assertEqual(result["queued"][0]["id"], "queued-1")


if __name__ == "__main__":
    unittest.main()
