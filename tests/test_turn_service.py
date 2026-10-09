from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from codex_web import application
from codex_web.api.turns import build_turns_router
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
        self.handoffs = set()
        self.start_error = None
        self.interrupt_error = None

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

    async def release_unviable_active_turn(self, thread_id):
        return False

    async def start_thread_turn_now(self, thread_id, **kwargs):
        self.start_calls.append((thread_id, kwargs))
        if self.start_error is not None:
            raise self.start_error
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
        if any(item.id == queued.id for item in self.queue_policy.queued):
            return
        self.queue_policy.queued.insert(0, queued)

    def begin_thread_handoff(self, thread_id):
        if thread_id in self.handoffs:
            return False
        self.handoffs.add(thread_id)
        return True

    def finish_thread_handoff(self, thread_id, *, clear_active=False):
        self.handoffs.discard(thread_id)
        if clear_active:
            self.clear_thread_active(thread_id)

    def clear_thread_active(self, thread_id):
        self.active = False

    async def request_for_thread(self, thread_id, method, params=None):
        if self.interrupt_error is not None:
            raise self.interrupt_error
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

    async def test_deferred_start_persists_work_before_runtime_startup(self):
        service, queue, execution, events, settings = self._service()
        queue.queued = []
        execution.active = False
        execution.start_error = RuntimeError("runtime unavailable")
        result = await service.start("thread-1", TurnCreate(message="resume delivery", project_id="home", defer_start=True),
                                     work_item_ref="github:work-42")
        self.assertTrue(result["queued"])
        self.assertEqual(queue.queued[0].message, "resume delivery")
        self.assertEqual(queue.queued[0].work_item_ref, "github:work-42")
        self.assertEqual(execution.start_calls, [])

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

    async def _replacement_retry(self, *, existing=False, blocked=False, endless=False, mismatch=False):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        preflight = ExecutionPreflightService(ExecutionPreflightStore(
            SQLiteStateStore(Path(temp.name) / "state.sqlite3")))
        service, _queue, execution, _events, _settings = self._service()
        service.preflight = preflight
        mappings = {"original": "replacement"} if existing else {}
        service.recovery.replacement_thread_id = mappings.get
        actor = _admin_actor()
        payload = TurnCreate(message="unchanged correction", project_id="home",
            sandbox="danger-full-access", approval_policy="never",
            agent_profile_id="james-saml", agent_profile_revision=2,
            repository_resource_id="repo-saas", writable_repository_resource_ids=("repo-saas",))
        retained = preflight.record_blocked(actor=actor, thread_id="original",
            project_id="home", execution_id="same-execution", payload=payload,
            effective=payload.model_dump(), detail={"message": "quota"})
        calls = []
        async def start(thread_id, value, **kwargs):
            calls.append((thread_id, value, kwargs))
            if thread_id == "original" or endless:
                target = "replacement" if thread_id == "original" else "second"
                mappings[thread_id] = target
                return {"staleThreadReplaced": True, "threadId": "wrong" if mismatch else target}
            if blocked:
                detail = {"code": "execution_preflight_blocked", "message": "capacity"}
                preflight.record_blocked(actor=actor, thread_id=thread_id,
                    project_id="home", execution_id="same-execution", payload=value,
                    effective=value.model_dump(), detail=detail)
                raise HTTPException(status_code=503, detail=detail)
            preflight.mark_started_for_execution("same-execution", actor=actor,
                claim_id=kwargs["retry_claim_id"])
            return {"turn": {"id": "actually-started"}}
        service.start = start
        execution.active_execution_id = lambda _thread: None
        return service, preflight, retained, actor, calls

    async def test_retry_follows_verified_new_replacement_once_and_deduplicates(self):
        service, preflight, retained, actor, calls = await self._replacement_retry()
        result = await service.retry_preflight("original", retained.id, actor=actor)
        self.assertEqual([item[0] for item in calls], ["original", "replacement"])
        self.assertEqual(result["threadId"], "replacement")
        self.assertEqual(result["attempt"]["thread_id"], "original")
        self.assertEqual(result["attempt"]["replacement_thread_id"], "replacement")
        self.assertEqual(result["attempt"]["status"], "started")
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertEqual(calls[1][1].agent_profile_revision, 2)
        self.assertEqual(calls[1][1].repository_resource_id, "repo-saas")
        self.assertIs(calls[1][2]["actor"], actor)
        self.assertEqual(calls[1][2]["execution_id"], retained.execution_id)
        self.assertEqual(preflight.for_thread("replacement", actor=actor)[0].id, retained.id)
        self.assertIn("/replacement/", result["attempt"]["retry_href"])
        duplicate = await service.retry_preflight("replacement", retained.id, actor=actor)
        self.assertTrue(duplicate["alreadyStarted"])
        self.assertEqual(len(calls), 2)

    async def test_retry_existing_replacement_keeps_denial_and_request_inspectable(self):
        service, preflight, retained, actor, calls = await self._replacement_retry(existing=True, blocked=True)
        with self.assertRaises(HTTPException) as error:
            await service.retry_preflight("original", retained.id, actor=actor)
        self.assertEqual(error.exception.status_code, 503)
        updated = preflight.get(retained.id, actor=actor)
        self.assertEqual(updated.status, "blocked")
        self.assertEqual(updated.message, retained.message)
        self.assertEqual(updated.thread_id, "original")
        self.assertEqual(updated.replacement_thread_id, "replacement")
        self.assertIsNone(updated.started_at)
        self.assertEqual([item[0] for item in calls], ["replacement"])
        self.assertEqual(preflight.for_thread("replacement", actor=actor)[0].id, retained.id)

    async def test_retry_rejects_unverified_replacement_and_bounds_repeated_replacements(self):
        for options, expected_calls in (({"mismatch": True}, 1), ({"endless": True}, 2)):
            with self.subTest(options=options):
                service, preflight, retained, actor, calls = await self._replacement_retry(**options)
                with self.assertRaises(HTTPException) as error:
                    await service.retry_preflight("original", retained.id, actor=actor)
                self.assertEqual(error.exception.status_code, 409)
                self.assertEqual(len(calls), expected_calls)
                self.assertEqual(preflight.get(retained.id, actor=actor).status, "failed")
                self.assertIsNone(preflight.get(retained.id, actor=actor).started_at)

    async def test_existing_replacement_still_enforces_real_profile_admission(self):
        from codex_web.services.agent_profiles import AgentProfileAccessDenied
        service, preflight, retained, actor, calls = await self._replacement_retry(existing=True)
        del service.start  # Exercise actual ordinary admission, not the retry stub.
        resolved = []
        def deny(profile_id, **kwargs):
            resolved.append((profile_id, kwargs))
            raise AgentProfileAccessDenied("profile does not grant repository scope")
        service.agent_profiles = SimpleNamespace(resolve_for_execution=deny)
        with self.assertRaises(HTTPException) as error:
            await service.retry_preflight("original", retained.id, actor=actor)
        self.assertEqual(error.exception.status_code, 403)
        self.assertEqual(error.exception.detail["code"], "agent_profile_access_denied")
        self.assertEqual(resolved[0][0], "james-saml")
        self.assertIs(resolved[0][1]["actor"], actor)
        self.assertEqual(resolved[0][1]["revision"], 2)
        self.assertEqual(preflight.get(retained.id, actor=actor).status, "failed")
        self.assertIsNone(preflight.get(retained.id, actor=actor).started_at)
        self.assertEqual(calls, [])
        other_workspace = actor.model_copy(update={"workspace_id": "other"})
        with self.assertRaises(HTTPException):
            await service.retry_preflight("replacement", retained.id, actor=other_workspace)
        self.assertEqual(len(resolved), 1)

    async def test_wrong_retry_thread_does_not_claim_retained_attempt(self):
        service, preflight, retained, actor, calls = await self._replacement_retry()
        with self.assertRaises(HTTPException) as error:
            await service.retry_preflight("unrelated", retained.id, actor=actor)
        self.assertEqual(error.exception.status_code, 404)
        self.assertIsNone(preflight.get(retained.id, actor=actor).retry_claim_id)
        self.assertEqual(calls, [])

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

    async def test_failed_steer_requeues_exact_turn_and_returns_retryable_error(self) -> None:
        service, queue, execution, events, _settings = self._service()
        queued = queue.queued[0]
        queued.work_item_ref = "github:issue-915"
        queued.writable_repository_resource_ids = ("repo-app", "repo-api")
        queued.read_only_repository_resource_ids = ("repo-docs",)
        queued.execution_profile_id = "repository-write"
        queued.agent_profile_id = "agent-1"
        queued.agent_profile_revision = 3
        queued.agent_profile_actor_id = "operator-1"
        execution.start_error = RuntimeError("Codex app-server stopped")

        with self.assertRaises(HTTPException) as caught:
            await service.steer("thread-1", queued.id)

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(
            caught.exception.detail["code"],
            "steering_runtime_unavailable",
        )
        self.assertTrue(caught.exception.detail["retryable"])
        self.assertTrue(caught.exception.detail["queuePreserved"])
        self.assertEqual(queue.queued, [queued])
        self.assertEqual(queue.queued[0].execution_id, "steer-queued-1")
        self.assertFalse(execution.active)
        self.assertEqual(execution.handoffs, set())
        kwargs = execution.start_calls[0][1]
        self.assertEqual(
            kwargs["writable_repository_resource_ids"],
            ("repo-app", "repo-api"),
        )
        self.assertEqual(kwargs["read_only_repository_resource_ids"], ("repo-docs",))
        self.assertEqual(kwargs["work_item_ref"], "github:issue-915")
        self.assertTrue(kwargs["preserve_active_handoff"])
        self.assertEqual(execution.published, ["thread-1"])
        self.assertEqual(events[-1]["type"], "steering_turn_requeued")

    async def test_interrupt_failure_requeues_and_preserves_active_turn(self) -> None:
        service, queue, execution, events, _settings = self._service()
        queued = queue.queued[0]
        execution.interrupt_error = RuntimeError("interrupt unavailable")

        with self.assertRaises(HTTPException) as caught:
            await service.steer("thread-1", queued.id)

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(queue.queued, [queued])
        self.assertTrue(execution.active)
        self.assertEqual(execution.start_calls, [])
        self.assertEqual(execution.handoffs, set())
        self.assertEqual(events[-1]["stage"], "interrupt")

    async def test_successful_steer_removes_once_and_retry_is_not_redelivered(self) -> None:
        service, queue, execution, events, _settings = self._service()

        result = await service.steer("thread-1", "queued-1")

        self.assertTrue(result["ok"])
        self.assertEqual(result["steeredId"], "queued-1")
        self.assertEqual(result["queueDepth"], 0)
        self.assertEqual(queue.queued, [])
        self.assertEqual(len(execution.start_calls), 1)
        self.assertEqual(
            execution.start_calls[0][1]["execution_id"],
            "steer-queued-1",
        )
        self.assertEqual(execution.handoffs, set())
        self.assertEqual(events[-1]["type"], "steering_turn_resumed")

        with self.assertRaises(HTTPException) as caught:
            await service.steer("thread-1", "queued-1")
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(len(execution.start_calls), 1)

    async def test_competing_handoff_returns_409_and_preserves_exact_item(self) -> None:
        service, queue, execution, _events, _settings = self._service()
        queued = queue.queued[0]
        execution.handoffs.add("thread-1")

        with self.assertRaises(HTTPException) as caught:
            await service.steer("thread-1", queued.id)

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail["code"], "steering_handoff_in_progress")
        self.assertTrue(caught.exception.detail["retryable"])
        self.assertEqual(caught.exception.detail["queuedId"], queued.id)
        self.assertIs(queue.queued[0], queued)
        self.assertEqual(len(queue.queued), 1)
        self.assertTrue(execution.active)
        self.assertEqual(execution.handoffs, {"thread-1"})
        self.assertEqual(execution.start_calls, [])

    async def test_active_record_remains_fenced_during_interrupt_and_start(self) -> None:
        service, _queue, execution, events, _settings = self._service()

        async def interrupt(*args, **kwargs):
            self.assertTrue(execution.active)
            self.assertEqual(execution.handoffs, {"thread-1"})
            return {}

        async def start(*args, **kwargs):
            self.assertTrue(execution.active)
            self.assertEqual(execution.handoffs, {"thread-1"})
            self.assertTrue(kwargs["preserve_active_handoff"])
            return {"turn": {"id": "replacement"}}

        execution.request_for_thread = interrupt
        execution.start_thread_turn_now = start
        result = await service.steer("thread-1", "queued-1")

        self.assertEqual(result["turn"]["id"], "replacement")
        self.assertEqual(execution.handoffs, set())
        self.assertEqual([event["type"] for event in events], [
            "steering_handoff_started", "steering_turn_interrupted", "steering_turn_resumed",
        ])

    async def test_retry_after_transient_failure_reuses_execution_identity(self) -> None:
        service, queue, execution, _events, _settings = self._service()
        execution.start_error = RuntimeError("runtime restarting")

        with self.assertRaises(HTTPException):
            await service.steer("thread-1", "queued-1")
        first_execution_id = execution.start_calls[0][1]["execution_id"]

        execution.start_error = None
        execution.active = False
        result = await service.steer("thread-1", "queued-1")

        self.assertTrue(result["ok"])
        self.assertEqual(len(execution.start_calls), 2)
        self.assertEqual(
            execution.start_calls[1][1]["execution_id"],
            first_execution_id,
        )
        self.assertEqual(queue.queued, [])

    def test_steer_api_serializes_retryable_503_contract(self) -> None:
        service, queue, execution, _events, _settings = self._service()
        execution.start_error = RuntimeError("Codex app-server stopped")
        app = FastAPI()
        from codex_web.identity import AuthenticationActor, PrincipalKind
        actor = AuthenticationActor(identity_id="test", principal_kind=PrincipalKind.HUMAN,
                                    organization_id="local", workspace_id="default", assurance=AuthenticationAssurance.MFA)
        scope = SimpleNamespace(for_thread=lambda *args: SimpleNamespace(id="home"))
        @app.middleware("http")
        async def identity(request, call_next):
            request.state.identity_actor = actor
            return await call_next(request)
        app.include_router(build_turns_router(service, scope))

        response = TestClient(app).post(
            f"/api/threads/thread-1/queue/{queue.queued[0].id}/steer"
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {
                "detail": {
                    "code": "steering_runtime_unavailable",
                    "message": "Codex app-server stopped",
                    "retryable": True,
                    "queuedId": "queued-1",
                    "queuePreserved": True,
                    "reconcile": "refresh_queue_and_turn",
                }
            },
        )
        self.assertEqual(len(queue.queued), 1)


if __name__ == "__main__":
    unittest.main()
