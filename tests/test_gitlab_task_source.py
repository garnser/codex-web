from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx
from codex_web.integrations.gitlab_client import GitLabClient
from copy import deepcopy

from codex_web.models import TaskSourceIdentity
from codex_web.services.gitlab_task_source import GitLabTaskSource
from codex_web.services.task_source_conformance import TaskSourceConformanceSuite
from codex_web.services.task_sources import (
    InvalidTaskSourceIdentity,
    TaskSource,
    TaskSourceCapability,
    TaskSourceCapabilities,
    TaskSourceCreateRequest,
    UnsupportedTaskSourceCapability,
)


class _FakeGitLabClient:
    def __init__(self) -> None:
        self.issue = {
            "id": 9001,
            "project_id": 501,
            "iid": 42,
            "title": "Implement adapter",
            "state": "opened",
            "web_url": "https://gitlab.example/group/project/-/issues/42",
            "updated_at": "2026-09-17T20:00:00Z",
            "references": {"full": "group/project#42"},
            "labels": ["priority::P1", "owner::james", "status::in progress"],
            "assignees": [{"username": "provider-user"}],
        }
        self.merge_request_row = {
            "id": 9101,
            "iid": 42,
            "title": "Merge adapter",
            "description": "Native merge-request body",
            "state": "opened",
            "web_url": "https://gitlab.example/group/project/-/merge_requests/42",
            "updated_at": "2026-09-17T21:00:00Z",
            "references": {"full": "group/project!42"},
            "labels": ["owner::sally", "status::in progress"],
            "assignees": [{"username": "merge-owner"}],
        }
        self.group_calls: list[tuple[str, str, str]] = []
        self.read_calls: list[tuple[str, int]] = []
        self.merge_request_calls: list[tuple[str, int]] = []
        self.merge_request_updates: list[dict[str, object]] = []
        self.update_payloads: list[dict[str, object]] = []
        self.create_payloads: list[tuple[str, dict[str, object]]] = []
        self.notes: list[tuple[str, int, str]] = []
        self.related_merge_requests: list[dict[str, object]] = []
        self.related_calls: list[tuple[str, int]] = []

    async def group_issues(self, api_base, group, *, token, labels=None, state="opened"):
        self.group_calls.append((api_base, group, state))
        return [deepcopy(self.issue)]

    async def project_issue(self, api_base, project, iid, *, token):
        self.read_calls.append((project, iid))
        return deepcopy(self.issue)

    async def merge_request(self, api_base, project, iid, *, token):
        self.merge_request_calls.append((project, iid))
        return deepcopy(self.merge_request_row)

    async def update_merge_request(self, api_base, project, iid, *, token, payload):
        self.merge_request_updates.append(dict(payload))
        if "labels" in payload:
            self.merge_request_row["labels"] = str(payload["labels"]).split(",")
        return deepcopy(self.merge_request_row)

    async def issue_related_merge_requests(
        self, api_base, project, iid, *, token
    ):
        self.related_calls.append((project, iid))
        return deepcopy(self.related_merge_requests)

    async def create_project_issue(self, api_base, project, *, token, payload):
        self.create_payloads.append((project, dict(payload)))
        issue = {
            **deepcopy(self.issue),
            "id": 9002,
            "iid": 43,
            "title": payload["title"],
            "references": {"full": f"{project}#43"},
            "web_url": f"https://gitlab.example/{project}/-/issues/43",
            "labels": [
                value for value in str(payload.get("labels") or "").split(",") if value
            ],
        }
        return issue

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

    async def test_mr_projection_writes_only_labels_and_preserves_lifecycle(self):
        for lifecycle in ("opened", "closed", "merged"):
            with self.subTest(lifecycle=lifecycle):
                self.client.merge_request_row.update(state=lifecycle,
                    labels=["priority::P1", "custom", "owner::sally", "status::in progress"])
                identity = self.source._identity("group/project!42")
                current = await self.source.read(identity)
                result = await self.source.write_projection(identity, current,
                    owner="quinn", stage="ready_for_validation")
                self.assertTrue(result.mutated)
                self.assertEqual(result.snapshot.source_state, lifecycle)
                self.assertEqual(result.snapshot.identity.external_id, "group/project!42")
                self.assertEqual(self.client.merge_request_updates[-1], {"labels":
                    "custom,priority::P1,owner::quinn,status::awaiting confirmation"})
        self.assertEqual(self.client.update_payloads, [])
        self.assertEqual(self.client.read_calls, [])

    async def test_mr_projection_noop_never_mutates_provider(self):
        identity = self.source._identity("group/project!42")
        current = await self.source.read(identity)
        result = await self.source.write_projection(identity, current,
            owner="sally", stage="implementation_active")
        self.assertFalse(result.mutated)
        self.assertEqual(self.client.merge_request_updates, [])
        self.assertEqual(self.client.update_payloads, [])

    async def test_mr_canonical_closed_stage_does_not_close_or_merge_mr(self):
        identity = self.source._identity("group/project!42")
        current = await self.source.read(identity)
        result = await self.source.write_projection(identity, current,
            owner="sally", stage="closed")
        self.assertEqual(self.client.merge_request_updates, [{"labels": "owner::sally"}])
        self.assertEqual(result.snapshot.source_state, "opened")

    async def test_mr_projection_rejects_mismatched_current_identity_before_write(self):
        current = await self.source.read(self.source._identity("group/project!42"))
        with self.assertRaises(InvalidTaskSourceIdentity):
            await self.source.write_projection(self.source._identity("other/project!42"),
                current, owner="quinn", stage="ready_for_validation")
        self.assertEqual(self.client.merge_request_updates, [])

    async def test_mr_projection_rejects_mismatched_provider_response(self):
        identity = self.source._identity("group/project!42")
        current = await self.source.read(identity)
        self.client.merge_request_row["iid"] = 99
        with self.assertRaises(InvalidTaskSourceIdentity):
            await self.source.write_projection(identity, current,
                owner="quinn", stage="ready_for_validation")
        self.assertEqual(self.client.update_payloads, [])

    async def test_mr_projection_rejects_malformed_identity_before_write(self):
        current = await self.source.read(self.source._identity("group/project!42"))
        for ref in ("group/project!0", "group/project!-1", "group/project!42!1",
                    "group/project#42!1", "group/project!１２"):
            with self.subTest(ref=ref), self.assertRaises(InvalidTaskSourceIdentity):
                await self.source.write_projection(self.source._identity(ref), current,
                    owner="quinn", stage="ready_for_validation")
        self.assertEqual(self.client.merge_request_updates, [])

    async def test_mr_projection_uses_real_mr_put_endpoint_with_labels_only(self):
        requests = []
        def respond(request):
            import json
            body = json.loads(request.content) if request.content else None
            requests.append((request.method, request.url.raw_path.decode(), body))
            row = deepcopy(self.client.merge_request_row)
            if body:
                row["labels"] = body["labels"].split(",")
            return httpx.Response(200, json=row)
        source = GitLabTaskSource(self.source.api_base, "test", client=GitLabClient(
            transport=httpx.MockTransport(respond)))
        identity = source._identity("group/project!42")
        current = await source.read(identity)
        result = await source.write_projection(identity, current,
            owner="quinn", stage="ready_for_validation")
        self.assertTrue(result.mutated)
        self.assertEqual(requests, [
            ("GET", "/api/v4/projects/group%2Fproject/merge_requests/42", None),
            ("PUT", "/api/v4/projects/group%2Fproject/merge_requests/42",
             {"labels": "owner::quinn,status::awaiting confirmation"}),
        ])

    async def test_merge_request_read_uses_real_mr_endpoint_and_preserves_identity(self):
        requests = []
        def respond(request):
            requests.append(request.url.raw_path.decode())
            return httpx.Response(200, json={
                "iid": 42, "references": {"full": "group/project!42"},
                "title": "MR title", "description": "MR body", "state": "merged",
                "updated_at": "revision", "web_url": "https://gitlab.example/mr/42",
                "labels": ["owner::james"], "assignees": [{"username": "quinn"}],
            })
        source = GitLabTaskSource(self.source.api_base, "test", client=GitLabClient(
            transport=httpx.MockTransport(respond)))
        identity = self.source._identity("group/project!42")
        snapshot = await source.read(identity)
        self.assertEqual(requests, ["/api/v4/projects/group%2Fproject/merge_requests/42"])
        self.assertEqual(snapshot.identity.external_id, "group/project!42")
        self.assertEqual(snapshot.identity.revision, "revision")
        self.assertEqual((snapshot.title, snapshot.body_text, snapshot.source_state),
                         ("MR title", "MR body", "merged"))
        self.assertEqual(snapshot.owners, ("quinn",))
        self.assertEqual(snapshot.labels, ("owner::james",))
        issue = await self.source.read(self.source._identity("group/project#42"))
        self.assertEqual(issue.identity.external_id, "group/project#42")
        self.assertEqual(self.client.read_calls, [("group/project", 42)])

    async def test_merge_request_read_supports_pre_relation_snapshot_schema(self):
        from dataclasses import make_dataclass
        legacy = make_dataclass("LegacySnapshot", ["identity", "title", "body_text",
            "source_state", "owners", "labels"], frozen=True)
        async def read(*args, **kwargs):
            return {"iid": 42, "title": "old schema"}
        self.client.merge_request = read
        with patch("codex_web.services.gitlab_task_source.TaskSourceSnapshot", legacy):
            snapshot = await self.source.read(self.source._identity("group/project!42"))
        self.assertEqual(snapshot.title, "old schema")
        self.assertEqual(snapshot.identity.external_id, "group/project!42")

    async def test_merge_request_malformed_or_foreign_source_rejected_before_transport(self):
        calls = []
        async def read(*args, **kwargs):
            calls.append(args)
            return {"iid": 42}
        self.client.merge_request = read
        for ref in ("!42", "group/project!0", "group/project!-1", "group/project!４２",
                    "group/project!42!7", "group/project#42!7"):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                await self.source.read(self.source._identity(ref))
        for field, value in (("source_type", "github"),
                             ("source_instance", "https://foreign.example/api/v4")):
            identity = self.source._identity("group/project!42").model_copy(update={field:value})
            with self.assertRaises(ValueError):
                await self.source.read(identity)
        self.source.capabilities = TaskSourceCapabilities(frozenset())
        with self.assertRaises(UnsupportedTaskSourceCapability):
            await self.source.read(self.source._identity("group/project!42"))
        self.assertEqual(calls, [])

    async def test_merge_request_response_must_match_project_and_iid(self):
        for response in ({"iid": 7}, {"iid": True},
                         {"iid":42,"references":{"full":"foreign/project!42"}}):
            async def read(*args, **kwargs):
                return response
            self.client.merge_request = read
            with self.subTest(response=response), self.assertRaises(ValueError):
                await self.source.read(self.source._identity("group/project!42"))

    async def test_merge_request_read_does_not_enable_issue_mutation(self):
        async def read(*args, **kwargs):
            return {"iid": 42}
        self.client.merge_request = read
        identity = self.source._identity("group/project!42")
        snapshot = await self.source.write_owner(identity, "quinn")
        self.assertEqual(snapshot.identity.external_id, "group/project!42")
        self.assertEqual(self.client.merge_request_updates, [{"labels": "owner::quinn"}])
        with self.assertRaises(ValueError):
            await self.source.add_comment(identity, "review")
        self.assertEqual(self.client.update_payloads, [])
        self.assertEqual(self.client.notes, [])

    def test_adapter_declares_provider_neutral_contract_and_capabilities(self) -> None:
        self.assertIsInstance(self.source, TaskSource)
        self.conformance.validate_adapter(self.source)
        self.assertEqual(self.source.source_type, "gitlab")
        self.assertEqual(self.source.source_instance, "https://gitlab.example/api/v4")
        self.assertTrue(self.source.capabilities.supports(TaskSourceCapability.CREATE))
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

    async def test_create_uses_configured_project_scope_and_normalizes_identity(self) -> None:
        snapshot = await self.source.create(
            TaskSourceCreateRequest(
                title="Generated goal work",
                body="Created only after approved decomposition.",
                owners=("quinn",),
                labels=("priority::P1",),
            ),
            scope="group/project",
        )

        self.conformance.validate_snapshot(self.source, snapshot)
        self.assertEqual(snapshot.identity.external_id, "group/project#43")
        self.assertEqual(snapshot.title, "Generated goal work")
        self.assertIn("owner::quinn", snapshot.labels)
        self.assertIn("priority::P1", snapshot.labels)
        project, payload = self.client.create_payloads[-1]
        self.assertEqual(project, "group/project")
        self.assertEqual(payload["title"], "Generated goal work")
        self.assertEqual(
            payload["description"],
            "Created only after approved decomposition.",
        )

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
        self.assertEqual(self.client.related_calls[-1], ("group/project", 42))
        self.assertEqual(read.artifact_relations, ())

    async def test_read_normalizes_only_same_project_exact_head_relations(self) -> None:
        head = "a" * 40
        self.client.related_merge_requests = [
            {
                "iid": 287,
                "source_project_id": 501,
                "target_project_id": 501,
                "sha": head,
                "state": "opened",
                "updated_at": "2026-09-17T20:05:00Z",
                "web_url": "https://gitlab.example/group/project/-/merge_requests/287",
            },
            {
                "iid": 288,
                "source_project_id": 999,
                "target_project_id": 501,
                "sha": "b" * 40,
                "state": "opened",
                "updated_at": "2026-09-17T20:06:00Z",
            },
            {
                "iid": 289,
                "source_project_id": 501,
                "target_project_id": 501,
                "sha": "not-an-exact-head",
                "state": "opened",
                "updated_at": "2026-09-17T20:07:00Z",
            },
        ]

        snapshot = await self.source.read(
            TaskSourceIdentity(
                source_type="gitlab",
                source_instance="https://gitlab.example/api/v4",
                external_id="group/project#42",
            )
        )

        self.assertEqual(len(snapshot.artifact_relations), 1)
        relation = snapshot.artifact_relations[0]
        self.assertEqual(relation.ref, "group/project!287")
        self.assertEqual(relation.head_revision, head)
        self.assertEqual(relation.state, "opened")

    async def test_read_rejects_missing_or_ambiguous_identity_without_provider_call(self) -> None:
        for external_id in (
            "group/project",
            "group/project!",
            "!42",
            "group/project!not-a-number",
            "group/project#42!42",
        ):
            with self.subTest(external_id=external_id):
                with self.assertRaises(InvalidTaskSourceIdentity):
                    await self.source.read(
                        TaskSourceIdentity(
                            source_type="gitlab",
                            source_instance="https://gitlab.example/api/v4",
                            external_id=external_id,
                        )
                    )

        self.assertEqual(self.client.read_calls, [])
        self.assertEqual(self.client.merge_request_calls, [])

    async def test_merge_request_read_rejects_mismatched_provider_identity(self) -> None:
        self.client.merge_request_row["references"] = {
            "full": "other/project!42"
        }
        with self.assertRaises(InvalidTaskSourceIdentity):
            await self.source.read(
                TaskSourceIdentity(
                    source_type="gitlab",
                    source_instance="https://gitlab.example/api/v4",
                    external_id="group/project!42",
                )
            )

    async def test_description_survives_discovery_read_and_both_webhook_paths(self) -> None:
        from codex_web.services.gitlab_task_source_events import GitLabWebhookTaskSource

        body = "## Acceptance\n- Verify exact persisted revision.\n\nMR: https://gitlab.example/group/project/-/merge_requests/7"
        self.client.issue["description"] = f"  {body}\n"
        discovered = (await self.source.discover(scope="group"))[0]
        self.assertEqual(discovered.body_text, body)
        self.assertEqual((await self.source.read(discovered.identity)).body_text, body)
        payload = {
            "object_kind": "issue",
            "project": {"path_with_namespace": "group/project"},
            "object_attributes": {"iid": 42, "description": f"  {body}\n"},
        }
        asynchronous = await self.source.normalize_event(payload)
        synchronous = GitLabWebhookTaskSource(
            "https://gitlab.example/api/v4"
        ).normalize_event_sync(payload)
        self.assertEqual(asynchronous.snapshot.body_text, body)
        self.assertEqual(synchronous.snapshot.body_text, body)
        self.conformance.validate_event(self.source, asynchronous)

    async def test_missing_or_blank_descriptions_remain_absent(self) -> None:
        from codex_web.services.gitlab_task_source_events import GitLabWebhookTaskSource

        webhook = GitLabWebhookTaskSource("https://gitlab.example/api/v4")
        for description in [None, "", " \n "]:
            with self.subTest(description=description):
                issue = {**self.client.issue, "description": description}
                self.assertIsNone(self.source._snapshot_from_issue(issue).body_text)
                payload = {
                    "object_kind": "issue",
                    "project": {"path_with_namespace": "group/project"},
                    "object_attributes": {"iid": 42, "description": description},
                }
                self.assertIsNone((await self.source.normalize_event(payload)).snapshot.body_text)
                self.assertIsNone(webhook.normalize_event_sync(payload).snapshot.body_text)

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
