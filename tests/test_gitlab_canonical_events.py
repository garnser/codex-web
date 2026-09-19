from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.canonical_events import CanonicalEventType
from codex_web.services.canonical_events import CanonicalEventBus, CanonicalEventIngestionService
from codex_web.services.gitlab import GitLabService
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _Host:
    GITLAB_API_BASE = "https://gitlab.example/api/v4"

    @staticmethod
    def _project(project_id):
        if project_id != "project-a":
            raise LookupError(project_id)
        return SimpleNamespace(
            id=project_id,
            organization_id="org-a",
            workspace_id="ws-a",
        )


class GitLabCanonicalEventTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        store = CanonicalEventStore(
            SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        )
        self.ingestion = CanonicalEventIngestionService(CanonicalEventBus(store))
        self.service = GitLabService(
            _Host(),
            canonical_events=self.ingestion,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_issue_webhook_normalizes_through_task_source_before_dispatch(self) -> None:
        payload = {
            "object_kind": "issue",
            "project": {"path_with_namespace": "group/project"},
            "object_attributes": {
                "iid": 42,
                "title": "Canonical event",
                "state": "opened",
                "action": "update",
                "updated_at": "2026-09-19T06:00:00Z",
                "url": "https://gitlab.example/group/project/-/issues/42",
            },
            "labels": [{"title": "status::in progress"}],
        }

        first = await self.service._ingest_canonical_event(
            payload,
            project_id="project-a",
            event_id="delivery-issue-1",
            kind="issue",
        )
        duplicate = await self.service._ingest_canonical_event(
            payload,
            project_id="project-a",
            event_id="delivery-issue-1",
            kind="issue",
        )

        self.assertTrue(first.inserted)
        self.assertFalse(duplicate.inserted)
        self.assertEqual(first.event.event_type, CanonicalEventType.TASK_SOURCE.value)
        self.assertEqual(first.event.payload["provider_event_type"], "issue.update")
        self.assertEqual(first.event.tenant_id, "org-a")
        self.assertEqual(first.event.workspace_id, "ws-a")

    async def test_non_issue_gitlab_events_use_canonical_classes(self) -> None:
        cases = (
            ("merge_request", CanonicalEventType.PULL_REQUEST),
            ("pipeline", CanonicalEventType.CI_PIPELINE),
            ("deployment", CanonicalEventType.DEPLOYMENT),
            ("incident", CanonicalEventType.INCIDENT),
        )
        for index, (kind, expected) in enumerate(cases):
            with self.subTest(kind=kind):
                delivery = await self.service._ingest_canonical_event(
                    {
                        "object_kind": kind,
                        "project": {"path_with_namespace": "group/project"},
                        "object_attributes": {
                            "action": "update",
                            "status": "success",
                            "url": f"https://gitlab.example/{kind}/{index}",
                        },
                    },
                    project_id="project-a",
                    event_id=f"delivery-{kind}",
                    kind=kind,
                )
                self.assertEqual(delivery.event.event_type, expected.value)
                self.assertEqual(delivery.event.payload["project_id"], "project-a")

    async def test_failed_provider_event_has_failure_class_when_not_more_specific(self) -> None:
        delivery = await self.service._ingest_canonical_event(
            {
                "object_kind": "system_hook",
                "project": {"path_with_namespace": "group/project"},
                "object_attributes": {"status": "failed"},
            },
            project_id="project-a",
            event_id="delivery-failure",
            kind="system_hook",
        )

        self.assertEqual(delivery.event.event_type, CanonicalEventType.FAILURE.value)


if __name__ == "__main__":
    unittest.main()
