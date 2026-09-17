from __future__ import annotations

import unittest
from copy import deepcopy

from codex_web.models import TaskSourceIdentity
from codex_web.services.gitlab_task_source import GitLabTaskSource
from codex_web.services.task_source_conformance import TaskSourceConformanceSuite
from codex_web.services.task_sources import (
    TaskSource,
    TaskSourceCapability,
    UnsupportedTaskSourceCapability,
)


class _FakeGitLabClient:
    def __init__(self) -> None:
        self.issue = {
            "id": 9001,
            "iid": 42,
            "title": "Implement adapter",
            "state": "opened",
            "web_url": "https://gitlab.example/group/project/-/issues/42",
            "updated_at": "2026-09-17T20:00:00Z",
            "references": {"full": "group/project#42"},
            "labels": ["priority::P1", "owner::james", "status::in progress"],
            "assignees": [{"username": "provider-user"}],
        }
        self.group_calls: list[tuple[str, str, str]] = []
        self.read_calls: list[tuple[str, int]] = []
        self.update_payloads: list[dict[str, object]] = []
        self.notes: list[tuple[str, int, str]] = []

    async def group_issues(self, api_base, group, *, token, labels=None, state="opened"):
        self.group_calls.append((api_base, group, state))
        return [deepcopy(self.issue)]

    async def project_issue(self, api_base, project, iid, *, token):
        self.read_calls.append((project, iid))
        return deepcopy(self.issue)

    async def update_project_issue(self, api_base, project, iid, *, token, payload):
        self.update_payloads.append(dict(payload))
        if "labels" in payload:
            self.issue["labels"] = [
                value for value in str(payload["labels"]).split(",") if value
            ]
        if payload.get("state_event") == "close":
            self.issue["state"] = "closed"
        elif payload.get("state_event") == "reopen":
            self.issue["state"] = "opened"
        return deepcopy(self.issue)

    async def create_project_issue_note(self, api_base, project, iid, *, token, body):
        self.notes.append((project, iid, body))
        return {"id": 1, "body": body}


class GitLabTaskSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = _FakeGitLabClient()
        self.source = GitLabTaskSource(
            "https://gitlab.example/api/v4/",
            "token",
            client=self.client,
        )
        self.conformance = TaskSourceConformanceSuite()

    def test_adapter_declares_provider_neutral_contract_and_capabilities(self) -> None:
        self.assertIsInstance(self.source, TaskSource)
        self.conformance.validate_adapter(self.source)
        self.assertEqual(self.source.source_type, "gitlab")
        self.assertEqual(self.source.source_instance, "https://gitlab.example/api/v4")
        self.assertTrue(self.source.capabilities.supports(TaskSourceCapability.DISCOVERY))
        self.assertTrue(self.source.capabilities.supports(TaskSourceCapability.READ))
        self.assertTrue(self.source.capabilities.supports(TaskSourceCapability.EVENTS))
        self.assertTrue(self.source.capabilities.supports(TaskSourceCapability.OWNER_WRITE))
        self.assertTrue(self.source.capabilities.supports(TaskSourceCapability.STATE_WRITE))
        self.assertTrue(self.source.capabilities.supports(TaskSourceCapability.COMMENTS))
        self.assertFalse(self.source.capabilities.supports(TaskSourceCapability.ARTIFACT_LINKS))

    def test_constructor_requires_instance_and_credential(self) -> None:
        with self.assertRaises(ValueError):
            GitLabTaskSource("", "token", client=self.client)
        with self.assertRaises(ValueError):
            GitLabTaskSource("https://gitlab.example/api/v4", "", client=self.client)

    async def test_discovery_and_read_normalize_provider_objects(self) -> None:
        discovered = await self.source.discover(scope="group")
        self.assertEqual(len(discovered), 1)
        snapshot = discovered[0]
        self.conformance.validate_snapshot(self.source, snapshot)
        self.assertEqual(snapshot.identity.external_id, "group/project#42")
        self.assertEqual(snapshot.identity.revision, "2026-09-17T20:00:00Z")
        self.assertEqual(snapshot.source_state, "opened")
        self.assertEqual(snapshot.owners, ("provider-user",))
        self.assertIn("owner::james", snapshot.labels)
        self.assertEqual(self.client.group_calls, [("https://gitlab.example/api/v4", "group", "opened")])

        read = await self.source.read(snapshot.identity)
        self.conformance.validate_snapshot(self.source, read)
        self.assertEqual(read.identity.external_id, snapshot.identity.external_id)
        self.assertEqual(self.client.read_calls[-1], ("group/project", 42))

    def test_projection_preserves_existing_label_semantics(self) -> None:
        snapshot = self.source._snapshot_from_issue(self.client.issue)
        projection = self.source.project(snapshot, current_stage="validation_running")
        self.conformance.validate_projection(self.source, snapshot, projection)
        self.assertEqual(projection.stage, "validation_running")
        self.assertEqual(projection.owner, "james")
        self.assertTrue(projection.owner_known)

        blocked = self.source._snapshot_from_issue(
            {**self.client.issue, "labels": ["owner::quinn", "status::blocked"]}
        )
        blocked_projection = self.source.project(blocked)
        self.assertEqual(blocked_projection.stage, "failed_with_action_owner")
        self.assertEqual(blocked_projection.owner, "quinn")

        awaiting = self.source._snapshot_from_issue(
            {**self.client.issue, "labels": ["status::awaiting confirmation"]}
        )
        awaiting_projection = self.source.project(awaiting)
        self.assertEqual(awaiting_projection.stage, "ready_for_validation")
        self.assertIsNone(awaiting_projection.owner)
        self.assertFalse(awaiting_projection.owner_known)

        closed = self.source._snapshot_from_issue({**self.client.issue, "state": "closed"})
        closed_projection = self.source.project(closed)
        self.assertEqual(closed_projection.stage, "closed")
        self.assertIsNone(closed_projection.owner)

    async def test_issue_event_normalization_passes_shared_conformance(self) -> None:
        event = await self.source.normalize_event(
            {
                "object_kind": "issue",
                "project": {"path_with_namespace": "group/project"},
                "object_attributes": {
                    "iid": 42,
                    "title": "Implement adapter",
                    "state": "opened",
                    "action": "update",
                    "updated_at": "2026-09-17T20:00:00Z",
                    "url": "https://gitlab.example/group/project/-/issues/42",
                },
                "labels": [
                    {"title": "owner::james"},
                    {"title": "status::in progress"},
                ],
                "assignees": [{"username": "provider-user"}],
            }
        )
        self.assertIsNotNone(event)
        self.conformance.validate_event(self.source, event)
        self.assertEqual(event.identity.external_id, "group/project#42")
        self.assertEqual(event.event_type, "issue.update")
        self.assertIsNotNone(event.occurred_at)
        projection = self.source.project(event.snapshot)
        self.conformance.validate_projection(self.source, event.snapshot, projection)
        self.assertEqual(projection.owner, "james")

        self.assertIsNone(
            await self.source.normalize_event({"object_kind": "pipeline"})
        )

    async def test_owner_write_replaces_only_owner_label(self) -> None:
        identity = TaskSourceIdentity(
            source_type="gitlab",
            source_instance="https://gitlab.example/api/v4",
            external_id="group/project#42",
        )
        snapshot = await self.source.write_owner(identity, "quinn")
        self.conformance.validate_snapshot(self.source, snapshot)
        self.assertIn("owner::quinn", snapshot.labels)
        self.assertNotIn("owner::james", snapshot.labels)
        self.assertIn("status::in progress", snapshot.labels)
        self.assertIn("priority::P1", snapshot.labels)

    async def test_state_write_projects_status_and_close_reopen_semantics(self) -> None:
        identity = TaskSourceIdentity(
            source_type="gitlab",
            source_instance="https://gitlab.example/api/v4",
            external_id="group/project#42",
        )

        blocked = await self.source.write_state(identity, "failed_with_action_owner")
        self.assertIn("status::blocked", blocked.labels)
        self.assertNotIn("status::in progress", blocked.labels)

        closed = await self.source.write_state(identity, "closed")
        self.assertEqual(closed.source_state, "closed")
        self.assertFalse(any(label.startswith("status::") for label in closed.labels))
        self.assertEqual(self.client.update_payloads[-1]["state_event"], "close")

        reopened = await self.source.write_state(identity, "implementation_active")
        self.assertEqual(reopened.source_state, "opened")
        self.assertIn("status::in progress", reopened.labels)
        self.assertEqual(self.client.update_payloads[-1]["state_event"], "reopen")

        with self.assertRaises(ValueError):
            await self.source.write_state(identity, "provider_done")

    async def test_comment_write_uses_issue_notes_capability(self) -> None:
        identity = TaskSourceIdentity(
            source_type="gitlab",
            source_instance="https://gitlab.example/api/v4",
            external_id="group/project#42",
        )
        await self.source.add_comment(identity, "Canonical update")
        self.assertEqual(self.client.notes, [("group/project", 42, "Canonical update")])
        with self.assertRaises(ValueError):
            await self.source.add_comment(identity, "   ")

    async def test_unsupported_artifact_operation_fails_closed(self) -> None:
        identity = TaskSourceIdentity(
            source_type="gitlab",
            source_instance="https://gitlab.example/api/v4",
            external_id="group/project#42",
        )
        with self.assertRaises(UnsupportedTaskSourceCapability):
            await self.source.attach_artifact(identity, "https://artifact.example/build")


if __name__ == "__main__":
    unittest.main()
