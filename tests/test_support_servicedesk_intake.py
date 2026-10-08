from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.services.gitlab import GitLabService, install_gitlab_compatibility
from codex_web.storage.auxiliary_state import ServiceDeskStateRepository
from codex_web.storage.sqlite_state import SQLiteStateStore


def _support_issue_payload() -> dict:
    return {
        "object_kind": "issue",
        "event_name": "issue",
        "project": {"id": 5, "path_with_namespace": "veridataops/support"},
        "object_attributes": {
            "id": 1001,
            "iid": 5,
            "title": "Customer cannot sign in",
            "state": "opened",
            "action": "open",
            "url": "https://dev.veridataops.com/gitlab/veridataops/support/-/issues/5",
        },
        "labels": [],
    }



class SupportServiceDeskIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state_file = self.root / "support_servicedesk_intake.json"
        self.store = SQLiteStateStore(self.root / "state.sqlite3")
        self.repository = ServiceDeskStateRepository(self.store, self.state_file)
        self.host = SimpleNamespace(
            _load_support_servicedesk_state=self.repository.load,
            _save_support_servicedesk_state=self.repository.save,
        )
        self.service = GitLabService(self.host)
        install_gitlab_compatibility(self.host, self.service)
        # Isolate both accepted settings from the caller's environment.
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_PATHS", None)
        os.environ["CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_PATH"] = "veridataops/support"

    def test_ticket_detection_is_disabled_with_empty_project_config(self) -> None:
        # Absent settings intentionally select the default Support project.
        # An explicitly empty resolved project list disables intake.
        os.environ["CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_PATHS"] = " , "
        self.assertEqual(self.host._support_servicedesk_project_paths(), [])
        self.assertFalse(self.host._is_support_servicedesk_ticket_payload(_support_issue_payload()))

    def test_absent_settings_use_default_project(self) -> None:
        os.environ.pop("CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_PATH", None)
        self.assertEqual(self.host._support_servicedesk_project_paths(), ["veridataops/support"])
        self.assertTrue(self.host._is_support_servicedesk_ticket_payload(_support_issue_payload()))

    def test_ticket_detection_uses_configured_project(self) -> None:
        self.assertTrue(self.host._is_support_servicedesk_ticket_payload(_support_issue_payload()))

    def test_ticket_detection_ignores_closed_and_other_projects(self) -> None:
        closed = _support_issue_payload()
        closed["object_attributes"]["state"] = "closed"
        other_project = _support_issue_payload()
        other_project["project"]["path_with_namespace"] = "veridataops/saas-app"
        self.assertFalse(self.host._is_support_servicedesk_ticket_payload(closed))
        self.assertFalse(self.host._is_support_servicedesk_ticket_payload(other_project))

    def test_ticket_state_is_idempotent(self) -> None:
        payload = _support_issue_payload()
        self.assertTrue(self.host._remember_support_servicedesk_ticket(payload, "webhook", "event-1"))
        self.assertFalse(self.host._remember_support_servicedesk_ticket(payload, "sweep", "event-2"))
        # Reopen canonical storage, rather than only inspecting the service instance.
        reloaded = ServiceDeskStateRepository(
            SQLiteStateStore(self.root / "state.sqlite3"), self.state_file
        ).load()
        self.assertEqual(set(reloaded["tickets"]), {"5:5"})
        ticket = reloaded["tickets"]["5:5"]
        self.assertEqual(ticket["first_source"], "webhook")
        self.assertEqual(ticket["last_source"], "sweep")
        self.assertEqual(ticket["event_id"], "event-1")
        self.assertEqual(json.loads(self.state_file.read_text()), reloaded)

    def test_issue_payload_preserves_ticket_identity(self) -> None:
        issue = {
            "id": 1001,
            "iid": 5,
            "title": "Customer cannot sign in",
            "description": "Login loop after password reset.",
            "state": "opened",
            "created_at": "2026-06-11T09:00:00Z",
            "updated_at": "2026-06-11T09:05:00Z",
            "web_url": "https://dev.veridataops.com/gitlab/veridataops/support/-/issues/5",
            "labels": ["priority::1"],
        }
        payload = self.host._issue_to_support_servicedesk_payload(issue, "veridataops/support", 5)
        self.assertEqual(payload["object_attributes"]["iid"], 5)
        self.assertEqual(payload["object_attributes"]["action"], "sweep")
        self.assertEqual(payload["labels"], [{"title": "priority::1"}])
        self.assertTrue(self.host._is_support_servicedesk_ticket_payload(payload))


if __name__ == "__main__":
    unittest.main()
