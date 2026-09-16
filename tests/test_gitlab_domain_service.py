from __future__ import annotations

import unittest
from types import SimpleNamespace

from codex_web.models import GitLabProjectRoutingSettings
from codex_web.services.gitlab import GitLabService, install_gitlab_service


class GitLabDomainServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = SimpleNamespace()
        self.app = SimpleNamespace(state=SimpleNamespace())
        self.service = install_gitlab_service(self.app, self.host)

    def test_installer_rebinds_legacy_entrypoints_to_one_service(self) -> None:
        self.assertIs(self.app.state.gitlab_service, self.service)
        self.assertIs(getattr(self.host._gitlab_event_id, "__self__", None), self.service)
        self.assertIs(getattr(self.host._gitlab_label_names, "__self__", None), self.service)
        self.assertIs(getattr(self.host._dispatch_support_servicedesk_ticket, "__self__", None), self.service)
        self.assertIs(getattr(self.host._support_servicedesk_sweep_loop, "__self__", None), self.service)
        self.assertIs(getattr(self.host._remember_gitlab_semantic_key, "__self__", None), self.service)

    def test_event_dedupe_state_is_service_owned(self) -> None:
        self.assertTrue(self.service.remember_event("event-1"))
        self.assertFalse(self.service.remember_event("event-1"))
        self.assertEqual(list(self.service._event_ids), ["event-1"])

    def test_label_and_owner_projection_are_canonical(self) -> None:
        payload = {
            "object_kind": "issue",
            "labels": [
                {"title": "owner::Dana"},
                {"title": "priority::P1"},
                {"title": "owner::Dana"},
            ],
        }
        settings = GitLabProjectRoutingSettings()
        self.assertEqual(
            self.service.label_names(payload),
            ["owner::Dana", "priority::P1"],
        )
        self.assertEqual(self.service.owner_agents(payload, settings), ["dana"])

    def test_project_path_matching_accepts_nested_projects(self) -> None:
        self.assertTrue(self.service.project_path_matches("group/sub/project", "group"))
        self.assertTrue(self.service.project_path_matches("group/sub/project", "group/sub/project"))
        self.assertFalse(self.service.project_path_matches("another/project", "group"))

    def test_servicedesk_payload_conversion_preserves_issue_identity(self) -> None:
        payload = self.service.issue_to_support_servicedesk_payload(
            {
                "id": 7,
                "iid": 3,
                "title": "Help",
                "description": "Details",
                "state": "opened",
                "web_url": "https://gitlab.example/group/support/-/issues/3",
                "labels": ["owner::james"],
                "created_at": "2026-09-16T10:00:00Z",
                "updated_at": "2026-09-16T11:00:00Z",
            },
            "group/support",
            11,
        )
        self.assertEqual(payload["project"]["id"], 11)
        self.assertEqual(payload["object_attributes"]["iid"], 3)
        self.assertEqual(payload["object_attributes"]["action"], "sweep")
        self.assertEqual(payload["labels"], [{"title": "owner::james"}])


if __name__ == "__main__":
    unittest.main()
