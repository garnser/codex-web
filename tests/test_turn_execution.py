from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI

from codex_web.models import Project, ThreadRunSettings
from codex_web.runtime.execution import TurnExecutionService, install_turn_execution_service


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


class TurnExecutionStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_resumes_then_starts_and_marks_active(self) -> None:
        host = _Host()
        service = TurnExecutionService(host)
        host.codex.request.side_effect = [
            {"thread": {"id": "t1"}},
            {"turn": {"id": "turn-1"}},
        ]
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
        )

        self.assertEqual(result["turn"]["id"], "turn-1")
        self.assertEqual(
            [call.args[0] for call in host.codex.request.await_args_list],
            ["thread/resume", "turn/start"],
        )
        self.assertEqual(host.active["t1"].turn_id, "turn-1")
        self.assertEqual(host.active["t1"].project_id, "p1")
        self.assertEqual(service.last_inputs["t1"]["message"], "do work")
        self.assertEqual(host.events[-1]["type"], "turn_started")
        self.assertEqual(host.hub.events[-1]["type"], "queue.status")

    async def test_completed_activity_clears_active_state(self) -> None:
        host = _Host()
        service = TurnExecutionService(host)
        service.mark_thread_active("t1", turn_id="turn-1", project_id="p1")

        service.record_thread_activity(
            {
                "method": "turn/completed",
                "params": {"threadId": "t1", "turn": {"id": "turn-1"}},
            }
        )

        self.assertNotIn("t1", host.active)


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
        self.assertIs(host._schedule_queue_drain.__self__, first)
        self.assertIs(host._record_terminal_turn_result.__self__, first)
        self.assertIs(host.CODEX_TURN_START_LOCK, first.turn_start_lock)
        self.assertIs(host.QUEUE_DRAIN_TASKS, first.queue_drain_tasks)
        self.assertIs(host.TERMINAL_RECOVERY_TASKS, first.terminal_recovery_tasks)
        self.assertIs(host.THREAD_TERMINAL_FAILURES, first.terminal_failures)
        self.assertIs(host.THREAD_LAST_INPUTS, first.last_inputs)


if __name__ == "__main__":
    unittest.main()
