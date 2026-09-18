from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException

from codex_web.models import Project, ThreadRunSettings
from codex_web.runtime.execution import TurnExecutionService, install_turn_execution_service
from codex_web.services.thread_bootstrap_bindings import (
    ThreadBootstrapBindingNotFoundError,
)
from codex_web.services.execution_workers import WorkerLeaseError


class _Hub:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish(self, event: dict) -> None:
        self.events.append(event)


class _Host:
    def __init__(self) -> None:
        self.queues = {}
        self.active = {}
        self.events: list[dict] = []
        self.hub = _Hub()
        self.codex = SimpleNamespace(request=AsyncMock())
        self.IS_SHUTTING_DOWN = False
        self.uuid = __import__("uuid")

    def _load_turn_queues(self):
        return {key: list(value) for key, value in self.queues.items()}

    def _save_turn_queues(self, queues):
        self.queues = {key: list(value) for key, value in queues.items()}

    def _thread_queue(self, thread_id):
        return list(self.queues.get(thread_id, []))

    def _thread_queue_depth(self, thread_id):
        return len(self.queues.get(thread_id, []))

    @staticmethod
    def _work_item_wakeup_entries(message):
        return []

    @staticmethod
    def _render_work_item_wakeup_batch(entries):
        return "batch"

    @staticmethod
    def _max_thread_queue_depth():
        return 10

    def _load_active_turns(self):
        return dict(self.active)

    def _save_active_turns(self, active):
        self.active = dict(active)

    @staticmethod
    def _thread_run_settings(thread_id):
        return ThreadRunSettings()

    @staticmethod
    def _autonomy_enabled():
        return False

    @staticmethod
    def _schedule_native_recovery_cycles(**kwargs):
        return None

    @staticmethod
    def _effective_developer_instructions(thread_id, value):
        return value

    @staticmethod
    def _project_params(project, values):
        return {key: value for key, value in values.items() if value is not None}

    @staticmethod
    def _sandbox_policy(sandbox, path):
        return {"type": sandbox}

    @staticmethod
    def _with_relay_guard(message, source):
        return message

    @staticmethod
    def _turn_source_for_relay_guard(thread_id, source):
        return source

    def _append_bot_event(self, event):
        self.events.append(event)


class TurnExecutionQueueTests(unittest.TestCase):
    def test_queue_is_fifo_and_requeue_front_preserves_item(self) -> None:
        host = _Host()
        service = TurnExecutionService(host)

        first = service.enqueue_turn(thread_id="t1", project_id="p1", message="one")
        second = service.enqueue_turn(thread_id="t1", project_id="p1", message="two")

        self.assertEqual(host._thread_queue_depth("t1"), 2)
        popped = service.pop_next_queued_turn("t1")
        self.assertEqual(popped.id, first.id)
        service.requeue_turn_front(popped)
        self.assertEqual([item.id for item in host._thread_queue("t1")], [first.id, second.id])

    def test_duplicate_source_and_message_reuses_existing_queue_item(self) -> None:
        host = _Host()
        service = TurnExecutionService(host)

        first = service.enqueue_turn(
            thread_id="t1",
            project_id="p1",
            message="same",
            source="slack",
        )
        duplicate = service.enqueue_turn(
            thread_id="t1",
            project_id="p1",
            message="same",
            source="slack",
        )

        self.assertIs(first, duplicate)
        self.assertEqual(host._thread_queue_depth("t1"), 1)


class _BindingService:
    def __init__(self) -> None:
        self.calls = []

    def prepare(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            assignment_id="assignment-1",
            workspace_id="workspace-1",
        )


class _Session:
    def __init__(self) -> None:
        self.workspace_path = Path("/isolated/workspace")
        self.requests = []

    def status(self):
        return SimpleNamespace(worker_id="worker-1", fence=7)

    def validate_current(self):
        return SimpleNamespace(
            id="assignment-1",
            execution_id="bootstrap-exec",
            execution_workspace_id="workspace-1",
            project_id="p1",
            sandbox="workspace-write",
            approval_policy="on-request",
        )

    async def request(self, method, params=None):
        self.requests.append((method, params))
        if method == "thread/resume":
            return {"thread": {"id": "t1"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        return {"ok": True}


class _SessionManager:
    def __init__(self) -> None:
        self.session = _Session()
        self.started = []
        self.completed = []
        self.reconciled = []
        self.reconcile_error = None
        self.reconcile_result = SimpleNamespace(
            id="assignment-1",
            execution_id="bootstrap-exec",
            execution_workspace_id="workspace-1",
            assigned_worker_id="worker-1",
            fence=7,
            status=SimpleNamespace(value="lost"),
        )

    async def start(self, assignment_id):
        self.started.append(assignment_id)
        return self.session

    def get(self, assignment_id):
        return self.session if assignment_id == "assignment-1" else None

    def reconcile_missing_session(self, assignment_id):
        self.reconciled.append(assignment_id)
        if self.reconcile_error is not None:
            raise self.reconcile_error
        return self.reconcile_result

    async def complete(self, assignment_id, **kwargs):
        self.completed.append((assignment_id, kwargs))
        return SimpleNamespace(id=assignment_id)


class _BootstrapBindings:
    def __init__(self, thread_id: str | None = None) -> None:
        self.thread_id = thread_id

    def get_by_thread(self, thread_id, actor):
        if self.thread_id != thread_id:
            raise ThreadBootstrapBindingNotFoundError(
                "thread bootstrap binding not found"
            )
        return SimpleNamespace(
            bootstrap_id="bootstrap-1",
            thread_id=thread_id,
            execution_id="bootstrap-exec",
            assignment_id="assignment-1",
            execution_workspace_id="workspace-1",
        )


class TurnExecutionStartTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, *, bootstrap_thread_id: str | None = None):
        host = _Host()
        binding = _BindingService()
        sessions = _SessionManager()
        service = TurnExecutionService(
            host,
            binding_service=binding,
            session_manager=sessions,
            bootstrap_bindings=_BootstrapBindings(bootstrap_thread_id),
            control_actor=SimpleNamespace(identity_id="control"),
        )
        return host, binding, sessions, service

    async def test_start_routes_resume_and_turn_start_through_assignment_bound_session(self) -> None:
        host, binding, sessions, service = self._service()
        project = Project(
            id="p1",
            name="Project",
            path="/workspace/project",
            sandbox="workspace-write",
            approval_policy="on-request",
        )

        result = await service.start_thread_turn_now(
            "t1",
            project=project,
            message="do work",
            sandbox="workspace-write",
            approval_policy="on-request",
            source="web",
            execution_id="exec-1",
        )

        self.assertEqual(result["turn"]["id"], "turn-1")
        host.codex.request.assert_not_awaited()
        self.assertEqual(sessions.started, ["assignment-1"])
        self.assertEqual(
            [method for method, _params in sessions.session.requests],
            ["thread/resume", "turn/start"],
        )
        resume = sessions.session.requests[0][1]
        turn = sessions.session.requests[1][1]
        self.assertEqual(resume["cwd"], "/isolated/workspace")
        self.assertEqual(turn["cwd"], "/isolated/workspace")
        self.assertEqual(binding.calls[0]["execution_id"], "exec-1")
        active = host.active["t1"]
        self.assertEqual(active.turn_id, "turn-1")
        self.assertEqual(active.execution_id, "exec-1")
        self.assertEqual(active.assignment_id, "assignment-1")
        self.assertEqual(active.execution_workspace_id, "workspace-1")
        self.assertEqual(active.worker_id, "worker-1")
        self.assertEqual(active.fence, 7)
        self.assertEqual(service.last_inputs["t1"]["assignment_id"], "assignment-1")
        self.assertEqual(host.events[-1]["assignment_id"], "assignment-1")
        self.assertEqual(host.hub.events[-1]["type"], "queue.status")

    async def test_idle_bootstrap_thread_request_uses_live_private_session(self) -> None:
        host, _binding, sessions, service = self._service(
            bootstrap_thread_id="t1"
        )

        response = await service.request_for_thread(
            "t1",
            "thread/read",
            {"threadId": "t1", "includeTurns": True},
        )

        self.assertEqual(response, {"ok": True})
        host.codex.request.assert_not_awaited()
        self.assertEqual(
            sessions.session.requests,
            [
                (
                    "thread/read",
                    {"threadId": "t1", "includeTurns": True},
                )
            ],
        )

    async def test_bootstrap_binding_without_live_session_preserves_still_valid_lease(self) -> None:
        host, _binding, sessions, service = self._service(
            bootstrap_thread_id="t1"
        )
        sessions.session = None
        sessions.reconcile_error = WorkerLeaseError(
            "assignment lease is still valid and cannot be recovered"
        )

        with self.assertRaises(HTTPException) as caught:
            await service.request_for_thread(
                "t1",
                "thread/read",
                {"threadId": "t1"},
            )

        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("canonical worker lease remains valid", caught.exception.detail)
        self.assertEqual(sessions.reconciled, ["assignment-1"])
        host.codex.request.assert_not_awaited()

    async def test_bootstrap_binding_missing_session_reconciles_expired_assignment_lost(self) -> None:
        host, _binding, sessions, service = self._service(
            bootstrap_thread_id="t1"
        )
        sessions.session = None

        with self.assertRaises(HTTPException) as caught:
            await service.request_for_thread(
                "t1",
                "thread/read",
                {"threadId": "t1"},
            )

        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("canonical assignment is lost", caught.exception.detail)
        self.assertEqual(sessions.reconciled, ["assignment-1"])
        self.assertEqual(host.events[-1]["type"], "codex_session_missing_reconciled")
        self.assertEqual(host.events[-1]["assignment_id"], "assignment-1")
        self.assertEqual(host.events[-1]["assignment_status"], "lost")
        self.assertTrue(host.events[-1]["bootstrap"])
        host.codex.request.assert_not_awaited()

    async def test_active_turn_missing_session_clears_stale_active_state_after_loss(self) -> None:
        host, _binding, sessions, service = self._service()
        sessions.session = None
        service.mark_thread_active(
            "t1",
            execution_id="bootstrap-exec",
            assignment_id="assignment-1",
            execution_workspace_id="workspace-1",
            worker_id="worker-1",
            fence=7,
        )

        with self.assertRaises(HTTPException) as caught:
            await service.request_for_thread(
                "t1",
                "turn/interrupt",
                {"threadId": "t1"},
            )

        self.assertEqual(caught.exception.status_code, 503)
        self.assertNotIn("t1", host.active)
        self.assertEqual(host.events[-1]["assignment_status"], "lost")
        self.assertFalse(host.events[-1]["bootstrap"])
        host.codex.request.assert_not_awaited()

    async def test_turn_on_bootstrap_thread_reuses_original_assignment_and_session(self) -> None:
        host, binding, sessions, service = self._service(
            bootstrap_thread_id="t1"
        )
        project = Project(
            id="p1",
            name="Project",
            path="/workspace/project",
            sandbox="workspace-write",
            approval_policy="on-request",
        )

        response = await service.start_thread_turn_now(
            "t1",
            project=project,
            message="continue work",
            sandbox="workspace-write",
            approval_policy="on-request",
            source="web",
            execution_id="queued-turn-exec",
        )

        self.assertEqual(response["turn"]["id"], "turn-1")
        host.codex.request.assert_not_awaited()
        self.assertEqual(binding.calls, [])
        self.assertEqual(sessions.started, [])
        self.assertEqual(
            [method for method, _params in sessions.session.requests],
            ["thread/resume", "turn/start"],
        )
        active = host.active["t1"]
        self.assertEqual(active.execution_id, "bootstrap-exec")
        self.assertEqual(active.assignment_id, "assignment-1")
        self.assertEqual(active.execution_workspace_id, "workspace-1")
        self.assertEqual(
            service.last_inputs["t1"]["requested_execution_id"],
            "queued-turn-exec",
        )
        self.assertEqual(
            service.last_inputs["t1"]["bootstrap_id"],
            "bootstrap-1",
        )

    async def test_terminal_bootstrap_turn_retains_session_assignment(self) -> None:
        host, _binding, sessions, service = self._service(
            bootstrap_thread_id="t1"
        )
        service.mark_thread_active(
            "t1",
            turn_id="turn-1",
            project_id="p1",
            execution_id="bootstrap-exec",
            assignment_id="assignment-1",
            execution_workspace_id="workspace-1",
            worker_id="worker-1",
            fence=7,
        )

        service.record_thread_activity(
            {
                "method": "turn/completed",
                "params": {"threadId": "t1", "turn": {"id": "turn-1"}},
            }
        )

        await asyncio.sleep(0)
        self.assertNotIn("t1", host.active)
        self.assertEqual(sessions.completed, [])
        self.assertEqual(
            host.events[-1]["type"],
            "thread_bootstrap_turn_completed",
        )
        self.assertTrue(host.events[-1]["session_retained"])

    async def test_thread_resume_failure_completes_assignment_before_turn_start(self) -> None:
        host, _binding, sessions, service = self._service()
        project = Project(
            id="p1",
            name="Project",
            path="/workspace/project",
            sandbox="workspace-write",
            approval_policy="on-request",
        )
        sessions.session.request = AsyncMock(side_effect=RuntimeError("resume failed"))

        with self.assertRaisesRegex(RuntimeError, "resume failed"):
            await service.start_thread_turn_now(
                "t1",
                project=project,
                message="do work",
                sandbox="workspace-write",
                approval_policy="on-request",
                execution_id="exec-1",
            )

        host.codex.request.assert_not_awaited()
        self.assertNotIn("t1", host.active)
        self.assertEqual(len(sessions.completed), 1)
        assignment_id, kwargs = sessions.completed[0]
        self.assertEqual(assignment_id, "assignment-1")
        self.assertFalse(kwargs["succeeded"])
        self.assertEqual(kwargs["failure_code"], "codex_thread_resume_failed")

    async def test_active_thread_request_never_falls_back_when_session_missing(self) -> None:
        host, _binding, _sessions, service = self._service()
        service.mark_thread_active(
            "t1",
            execution_id="exec-1",
            assignment_id="assignment-missing",
        )

        with self.assertRaisesRegex(HTTPException, "no live Codex session"):
            await service.request_for_thread(
                "t1",
                "turn/interrupt",
                {"threadId": "t1"},
            )

        host.codex.request.assert_not_awaited()

    async def test_completed_activity_schedules_exact_assignment_completion(self) -> None:
        host, _binding, sessions, service = self._service()
        service.mark_thread_active(
            "t1",
            turn_id="turn-1",
            project_id="p1",
            execution_id="exec-1",
            assignment_id="assignment-1",
            execution_workspace_id="workspace-1",
            worker_id="worker-1",
            fence=7,
        )

        service.record_thread_activity(
            {
                "method": "turn/completed",
                "params": {"threadId": "t1", "turn": {"id": "turn-1"}},
            }
        )
        await asyncio.gather(*list(service.assignment_completion_tasks.values()))

        self.assertNotIn("t1", host.active)
        self.assertEqual(len(sessions.completed), 1)
        assignment_id, kwargs = sessions.completed[0]
        self.assertEqual(assignment_id, "assignment-1")
        self.assertTrue(kwargs["succeeded"])
        self.assertEqual(host.events[-1]["type"], "turn_assignment_completed")

    async def test_failed_activity_marks_assignment_failed(self) -> None:
        host, _binding, sessions, service = self._service()
        service.mark_thread_active(
            "t1",
            turn_id="turn-1",
            execution_id="exec-1",
            assignment_id="assignment-1",
        )

        service.record_thread_activity(
            {
                "method": "turn/failed",
                "params": {
                    "threadId": "t1",
                    "turn": {"id": "turn-1"},
                    "error": "provider failure",
                },
            }
        )
        await asyncio.gather(*list(service.assignment_completion_tasks.values()))

        _assignment_id, kwargs = sessions.completed[0]
        self.assertFalse(kwargs["succeeded"])
        self.assertEqual(kwargs["failure_code"], "codex_turn_failed")
        self.assertIn("provider failure", kwargs["failure_message"])


class TurnExecutionInstallationTests(unittest.TestCase):
    def test_installer_rebinds_execution_entrypoints_and_registries(self) -> None:
        app = FastAPI()
        host = _Host()

        first = install_turn_execution_service(app, host)
        second = install_turn_execution_service(app, host)

        self.assertIs(first, second)
        self.assertIs(app.state.turn_execution_service, first)
        self.assertIs(host._enqueue_turn.__self__, first)
        self.assertIs(host._start_thread_turn_now.__self__, first)
        self.assertIs(host._codex_request_for_thread.__self__, first)
        self.assertIs(host._schedule_queue_drain.__self__, first)
        self.assertIs(host._record_terminal_turn_result.__self__, first)
        self.assertIs(host.CODEX_TURN_START_LOCK, first.turn_start_lock)
        self.assertIs(host.QUEUE_DRAIN_TASKS, first.queue_drain_tasks)
        self.assertIs(host.TERMINAL_RECOVERY_TASKS, first.terminal_recovery_tasks)
        self.assertIs(host.THREAD_TERMINAL_FAILURES, first.terminal_failures)
        self.assertIs(host.THREAD_LAST_INPUTS, first.last_inputs)
        self.assertIs(host.ASSIGNMENT_COMPLETION_TASKS, first.assignment_completion_tasks)
        self.assertIs(
            host.THREAD_ASSIGNMENT_COMPLETION_TASKS,
            first.thread_completion_tasks,
        )


if __name__ == "__main__":
    unittest.main()
