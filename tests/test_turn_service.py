from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from codex_web import application
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.models import Project, QueuedTurn, ThreadRunSettings, TurnCreate
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.turns import TurnService
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore


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
        self.start_calls = []
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
            work_item_ref=kwargs.get("work_item_ref"),
            repository_resource_id=kwargs.get("repository_resource_id"),
            writable_repository_resource_ids=kwargs.get(
                "writable_repository_resource_ids",
                (),
            ),
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

    def active_execution_id(self, thread_id):
        return getattr(self, "active_execution", None)

    async def start_thread_turn_now(self, thread_id, **kwargs):
        self.start_calls.append((thread_id, kwargs))
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


class _PreflightBlockingExecution(_Execution):
    def __init__(self, queue_policy: _QueuePolicy) -> None:
        super().__init__(queue_policy)
        self.active = False
        self.blocked = True
        self.start_calls = []

    async def start_thread_turn_now(self, thread_id, **kwargs):
        self.start_calls.append((thread_id, kwargs))
        if self.blocked:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "execution_preflight_blocked",
                    "message": "worker command execution is unavailable",
                    "blockers": [
                        {
                            "code": "worker_capability_missing",
                            "message": (
                                "worker command execution is unavailable"
                            ),
                            "retryable": False,
                            "remediation": "Repair the execution worker.",
                        }
                    ],
                    "retryable": False,
                },
            )
        return {"turn": {"id": "turn-retried"}}


def _admin_actor() -> AuthenticationActor:
    return AuthenticationActor(
        identity_id="admin",
        principal_kind=PrincipalKind.HUMAN,
        organization_id="local",
        workspace_id="default",
        roles=(MembershipRole.ADMIN,),
        assurance=AuthenticationAssurance.MFA,
    )


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

    async def test_work_item_ref_reaches_immediate_execution(self) -> None:
        service, queue, execution, _events, _settings = self._service()
        queue.queued = []
        execution.active = False

        await service.start(
            "thread-1",
            TurnCreate(message="do work", project_id="home"),
            work_item_ref="github:work-42",
        )

        self.assertEqual(len(execution.start_calls), 1)
        self.assertEqual(
            execution.start_calls[0][1]["work_item_ref"],
            "github:work-42",
        )

    async def test_work_item_ref_survives_turn_queueing(self) -> None:
        service, queue, _execution, _events, _settings = self._service()

        result = await service.start(
            "thread-1",
            TurnCreate(message="queued work", project_id="home"),
            work_item_ref="github:work-43",
        )

        self.assertTrue(result["queued"])
        self.assertEqual(len(queue.queued), 1)
        self.assertEqual(
            queue.queued[0].work_item_ref,
            "github:work-43",
        )

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

    async def test_coordinated_writable_targets_reach_immediate_execution(self) -> None:
        service, queue, execution, _events, _settings = self._service()
        queue.queued = []
        execution.active = False

        await service.start(
            "thread-1",
            TurnCreate(
                message="coordinate repositories",
                project_id="home",
                writable_repository_resource_ids=("repo-app", "repo-api"),
            ),
        )

        self.assertEqual(
            execution.start_calls[0][1]["writable_repository_resource_ids"],
            ("repo-app", "repo-api"),
        )

    async def test_thread_writable_targets_apply_when_turn_omits_scope(self) -> None:
        service, queue, execution, _events, settings = self._service()
        queue.queued = []
        execution.active = False
        settings.get = lambda _thread_id: ThreadRunSettings(
            writable_repository_resource_ids=("repo-app", "repo-api"),
        )

        await service.start(
            "thread-1",
            TurnCreate(message="use thread scope", project_id="home"),
        )

        self.assertEqual(
            execution.start_calls[0][1]["writable_repository_resource_ids"],
            ("repo-app", "repo-api"),
        )

    async def test_coordinated_writable_targets_survive_queueing(self) -> None:
        service, queue, _execution, _events, _settings = self._service()

        result = await service.start(
            "thread-1",
            TurnCreate(
                message="coordinate queued repositories",
                project_id="home",
                writable_repository_resource_ids=("repo-app", "repo-api"),
            ),
        )

        self.assertTrue(result["queued"])
        self.assertEqual(
            queue.queued[0].writable_repository_resource_ids,
            ("repo-app", "repo-api"),
        )

    async def test_preflight_failure_is_retained_and_retry_reuses_execution_id(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        queue = _QueuePolicy()
        queue.queued = []
        execution = _PreflightBlockingExecution(queue)
        preflight = ExecutionPreflightService(
            ExecutionPreflightStore(
                SQLiteStateStore(Path(temp.name) / "state.sqlite3")
            )
        )
        service = TurnService(
            projects=_Projects(),
            settings=_Settings(),
            recovery=_Recovery(),
            resume_runtime=_Resume(),
            bindings=_Bindings(),
            queue_policy=queue,
            execution=execution,
            event_sink=lambda _event: None,
            truncate_text=lambda value, limit: str(value)[:limit],
            binding_public=lambda binding: binding.model_dump(),
            preflight=preflight,
        )
        actor = _admin_actor()

        with self.assertRaises(HTTPException) as caught:
            await service.start(
                "thread-1",
                TurnCreate(
                    message="apply fix",
                    project_id="home",
                    writable_repository_resource_ids=("repo-app", "repo-api"),
                ),
                actor=actor,
                execution_id="thread-turn-retained",
            )

        self.assertEqual(caught.exception.status_code, 503)
        detail = caught.exception.detail
        self.assertEqual(detail["code"], "execution_preflight_blocked")
        self.assertTrue(detail["retainedMessage"])
        attempt_id = detail["attemptId"]
        retained = preflight.get(attempt_id, actor=actor)
        self.assertEqual(retained.message, "apply fix")
        self.assertEqual(retained.execution_id, "thread-turn-retained")
        self.assertEqual(
            retained.writable_repository_resource_ids,
            ("repo-app", "repo-api"),
        )
        self.assertEqual(retained.status, "blocked")

        execution.blocked = False
        retried = await service.retry_preflight(
            "thread-1",
            attempt_id,
            actor=actor,
        )

        self.assertEqual(retried["result"]["turn"]["id"], "turn-retried")
        self.assertEqual(retried["attempt"]["status"], "started")
        self.assertEqual(len(execution.start_calls), 2)
        self.assertEqual(
            execution.start_calls[-1][1]["execution_id"],
            "thread-turn-retained",
        )
        self.assertEqual(
            execution.start_calls[-1][1]["writable_repository_resource_ids"],
            ("repo-app", "repo-api"),
        )

        duplicate = await service.retry_preflight(
            "thread-1",
            attempt_id,
            actor=actor,
        )
        self.assertTrue(duplicate["alreadyStarted"])
        self.assertEqual(len(execution.start_calls), 2)

    async def test_retry_recovers_same_active_execution_without_duplicate_dispatch(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        queue = _QueuePolicy()
        queue.queued = []
        execution = _PreflightBlockingExecution(queue)
        preflight = ExecutionPreflightService(
            ExecutionPreflightStore(
                SQLiteStateStore(Path(temp.name) / "state.sqlite3")
            )
        )
        service = TurnService(
            projects=_Projects(),
            settings=_Settings(),
            recovery=_Recovery(),
            resume_runtime=_Resume(),
            bindings=_Bindings(),
            queue_policy=queue,
            execution=execution,
            event_sink=lambda _event: None,
            truncate_text=lambda value, limit: str(value)[:limit],
            binding_public=lambda binding: binding.model_dump(),
            preflight=preflight,
        )
        actor = _admin_actor()

        with self.assertRaises(HTTPException) as caught:
            await service.start(
                "thread-1",
                TurnCreate(message="apply fix", project_id="home"),
                actor=actor,
                execution_id="thread-turn-recovered",
            )
        attempt_id = caught.exception.detail["attemptId"]
        self.assertEqual(len(execution.start_calls), 1)

        execution.active = True
        execution.active_execution = "thread-turn-recovered"
        result = await service.retry_preflight(
            "thread-1",
            attempt_id,
            actor=actor,
        )

        self.assertTrue(result["alreadyStarted"])
        self.assertTrue(result["recoveredActiveExecution"])
        self.assertEqual(result["attempt"]["status"], "started")
        self.assertEqual(len(execution.start_calls), 1)
        self.assertEqual(queue.queued, [])

    def test_preflight_routes_are_owned_by_turn_domain(self) -> None:
        self.assertIn(
            "turns",
            _path_tags(
                "/api/threads/{thread_id}/preflight-attempts"
            ),
        )
        self.assertIn(
            "turns",
            _path_tags(
                "/api/threads/{thread_id}/preflight-attempts/{attempt_id}/retry"
            ),
        )

    def test_queue_snapshot_is_presentational(self) -> None:
        service, _queue, _execution, _events, _settings = self._service()

        result = service.queue("thread-1")

        self.assertTrue(result["active"])
        self.assertEqual(result["queueDepth"], 1)
        self.assertEqual(result["queued"][0]["id"], "queued-1")


if __name__ == "__main__":
    unittest.main()
