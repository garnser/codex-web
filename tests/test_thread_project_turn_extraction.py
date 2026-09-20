from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from codex_web import application
from codex_web.models import Project
from codex_web.runtime import core
from codex_web.services.project_runtime import ProjectRuntimeService
from codex_web.services.thread_resume import ThreadResumeService


class _Projects:
    def __init__(self) -> None:
        self.project = Project(
            id="p1",
            name="Project",
            path="/workspace/project",
            sandbox="workspace-write",
            approval_policy="on-request",
            model="gpt-test",
        )

    def get(self, project_id):
        if project_id not in {None, "p1"}:
            raise RuntimeError("not found")
        return self.project

    def list(self):
        return [self.project]


class ThreadProjectTurnExtractionTests(unittest.TestCase):
    def test_application_compatibility_surface_points_at_extracted_owners(self) -> None:
        self.assertIs(core._project.__self__, application.project_runtime_service)
        self.assertIs(
            core._project_for_cwd.__self__,
            application.project_runtime_service,
        )
        self.assertIs(core._set_thread_name.__self__, application.thread_naming_service)
        self.assertIs(
            core._web_thread_resume_task.__self__,
            application.app.state.thread_resume_compatibility_service,
        )
        self.assertIs(
            core._set_thread_primary.__self__,
            application.thread_bot_collaboration_service,
        )
        self.assertIs(
            core._set_thread_primary_channel.__self__,
            application.thread_bot_collaboration_service,
        )
        self.assertIs(
            core._raise_if_thread_replaced.__self__,
            application.app.state.thread_recovery_compatibility_service,
        )
        self.assertIs(
            core._thread_run_settings.__self__,
            application.thread_execution_settings_service,
        )

    def test_thread_and_turn_services_do_not_retain_general_purpose_legacy_host(self) -> None:
        self.assertFalse(hasattr(application.thread_service, "host"))
        self.assertFalse(hasattr(application.turn_service, "host"))

    def test_project_runtime_preserves_project_parameter_contract(self) -> None:
        service = ProjectRuntimeService(_Projects())
        project = service.get("p1")
        params = service.params(
            project,
            {
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "model": None,
            },
        )
        self.assertEqual(params["cwd"], "/workspace/project")
        self.assertEqual(params["sandbox"], "read-only")
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertEqual(params["approvalsReviewer"], "user")
        self.assertEqual(params["model"], "gpt-test")
        self.assertEqual(
            service.sandbox_policy("read-only", project.path),
            {"type": "readOnly", "networkAccess": False},
        )


class ThreadResumeExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_schedule_coalesces_concurrent_requests_and_cleans_task(self) -> None:
        gate = asyncio.Event()
        calls = []

        async def request(method, params):
            calls.append((method, params))
            await gate.wait()
            return {"thread": {"id": params["threadId"]}}

        service = ThreadResumeService(
            request,
            SimpleNamespace(load=lambda: []),
            SimpleNamespace(for_thread=lambda _thread_id: []),
            event_sink=lambda _event: None,
            truncate_text=lambda value, limit: str(value)[:limit],
        )

        first, first_scheduled = service.schedule(
            "thread-1",
            "p1",
            {"threadId": "thread-1"},
        )
        second, second_scheduled = service.schedule(
            "thread-1",
            "p1",
            {"threadId": "thread-1"},
        )

        self.assertIs(first, second)
        self.assertTrue(first_scheduled)
        self.assertFalse(second_scheduled)
        gate.set()
        result = await first
        self.assertEqual(result["thread"]["id"], "thread-1")
        await asyncio.sleep(0)
        self.assertNotIn("thread-1", service.tasks)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
