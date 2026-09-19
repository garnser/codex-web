from __future__ import annotations

import unittest
from copy import deepcopy

from codex_web.models import TaskSourceIdentity
from codex_web.services.jira_task_source import JiraTaskSource
from codex_web.services.task_source_conformance import TaskSourceConformanceSuite
from codex_web.services.task_sources import (
    TaskSourceCapability,
    TaskSourceCreateRequest,
    TaskSourceReconciliationCursor,
    UnsupportedTaskSourceCapability,
)


def _issue(key: str = "OPS-42", *, updated: str = "2026-09-19T06:00:00.000+0000"):
    return {
        "id": "10042",
        "key": key,
        "fields": {
            "summary": "Investigate outage",
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Customer impact observed"}],
                    }
                ],
            },
            "status": {"name": "In Progress"},
            "assignee": {
                "accountId": "acct-42",
                "displayName": "Dana Operator",
            },
            "labels": ["priority-p1", "service-api"],
            "priority": {"name": "Highest"},
            "issuetype": {"name": "Incident"},
            "parent": {"key": "OPS-1"},
            "updated": updated,
        },
    }


class _FakeJiraClient:
    def __init__(self) -> None:
        self.issues = {"OPS-42": _issue()}
        self.search_calls = []
        self.created_payloads = []
        self.updated_payloads = []
        self.comments = []
        self.transition_calls = []

    async def search_issues(
        self,
        api_base,
        *,
        token,
        username,
        jql,
        start_at=0,
        max_results=100,
    ):
        self.search_calls.append((jql, start_at, max_results))
        values = list(self.issues.values())
        page = values[start_at:start_at + max_results]
        return {"issues": deepcopy(page), "total": len(values)}

    async def issue(self, api_base, key, *, token, username):
        return deepcopy(self.issues[key])

    async def create_issue(self, api_base, *, token, username, payload):
        self.created_payloads.append(deepcopy(payload))
        issue = _issue("OPS-43", updated="2026-09-19T07:00:00.000+0000")
        issue["fields"]["summary"] = payload["fields"]["summary"]
        self.issues["OPS-43"] = issue
        return {"id": "10043", "key": "OPS-43"}

    async def update_issue(self, api_base, key, *, token, username, payload):
        self.updated_payloads.append((key, deepcopy(payload)))
        assignee = payload.get("fields", {}).get("assignee")
        if assignee is None:
            self.issues[key]["fields"]["assignee"] = None
        elif isinstance(assignee, dict):
            self.issues[key]["fields"]["assignee"] = {
                "accountId": assignee["accountId"],
                "displayName": assignee["accountId"],
            }

    async def add_comment(self, api_base, key, *, token, username, body):
        self.comments.append((key, deepcopy(body)))
        return {"id": "1"}

    async def transitions(self, api_base, key, *, token, username):
        return (
            {"id": "21", "name": "Start Progress"},
            {"id": "31", "name": "Resolve"},
        )

    async def transition_issue(
        self,
        api_base,
        key,
        transition_id,
        *,
        token,
        username,
    ):
        self.transition_calls.append((key, transition_id))
        if transition_id == "31":
            self.issues[key]["fields"]["status"] = {"name": "Resolved"}


class JiraTaskSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = _FakeJiraClient()
        self.source = JiraTaskSource(
            "https://jira.example",
            "credential",
            username="agent@example.com",
            client=self.client,
        )
        self.suite = TaskSourceConformanceSuite()

    def test_adapter_declares_enterprise_capabilities(self) -> None:
        self.suite.validate_adapter(self.source)
        for capability in (
            TaskSourceCapability.CREATE,
            TaskSourceCapability.DISCOVERY,
            TaskSourceCapability.PAGED_DISCOVERY,
            TaskSourceCapability.READ,
            TaskSourceCapability.EVENTS,
            TaskSourceCapability.OWNER_WRITE,
            TaskSourceCapability.COMMENTS,
            TaskSourceCapability.WORKFLOW_TRANSITIONS,
            TaskSourceCapability.INCREMENTAL_RECONCILIATION,
            TaskSourceCapability.PROVIDER_IDENTITIES,
            TaskSourceCapability.RICH_TEXT,
        ):
            self.assertTrue(self.source.capabilities.supports(capability))
        self.assertFalse(self.source.capabilities.supports(TaskSourceCapability.STATE_WRITE))
        self.assertFalse(self.source.capabilities.supports(TaskSourceCapability.ARTIFACT_LINKS))

    async def test_discovery_normalizes_adf_identity_and_provider_metadata(self) -> None:
        page = await self.source.discover_page(scope="OPS", limit=25)
        self.suite.validate_page(self.source, page)
        self.assertTrue(page.exhausted)
        snapshot = page.items[0]
        self.assertEqual(snapshot.identity.external_id, "OPS-42")
        self.assertEqual(snapshot.body_text, "Customer impact observed")
        self.assertEqual(snapshot.owner_references[0].provider_id, "acct-42")
        self.assertEqual(snapshot.priority, "Highest")
        self.assertEqual(snapshot.category, "Incident")
        self.assertEqual(snapshot.parent_external_id, "OPS-1")
        self.assertIn('project = "OPS"', self.client.search_calls[0][0])

    async def test_incremental_reconciliation_uses_stable_compound_ordering(self) -> None:
        cursor = TaskSourceReconciliationCursor(
            watermark="2026-09-19T05:00:00.000+0000",
            tiebreaker="OPS-1",
        )
        page = await self.source.reconcile_since(scope="OPS", cursor=cursor)
        self.suite.validate_page(self.source, page)
        self.assertEqual(len(page.items), 1)
        self.assertEqual(
            page.watermark,
            "2026-09-19T06:00:00.000+0000|OPS-42",
        )
        self.assertIn("ORDER BY updated ASC, key ASC", self.client.search_calls[-1][0])

    async def test_create_maps_canonical_request_without_leaking_jira_objects(self) -> None:
        snapshot = await self.source.create(
            TaskSourceCreateRequest(
                title="Generated work",
                body="Bounded description",
                owners=("acct-99",),
                labels=("automation",),
            ),
            scope="OPS",
        )
        self.suite.validate_snapshot(self.source, snapshot)
        fields = self.client.created_payloads[-1]["fields"]
        self.assertEqual(fields["project"], {"key": "OPS"})
        self.assertEqual(fields["summary"], "Generated work")
        self.assertEqual(fields["assignee"], {"accountId": "acct-99"})
        self.assertEqual(fields["labels"], ["automation"])
        self.assertEqual(fields["description"]["type"], "doc")

    async def test_webhook_normalization_and_projection_are_provider_neutral(self) -> None:
        event = await self.source.normalize_event(
            {
                "webhookEvent": "jira:issue_updated",
                "issue": _issue(),
            }
        )
        self.assertIsNotNone(event)
        self.suite.validate_event(self.source, event)
        projection = self.source.project(event.snapshot)
        self.suite.validate_projection(self.source, event.snapshot, projection)
        self.assertEqual(projection.stage, "implementation_active")
        self.assertEqual(projection.owner, "Dana Operator")

    async def test_owner_comment_and_workflow_operations_are_capability_gated(self) -> None:
        identity = TaskSourceIdentity(
            source_type="jira",
            source_instance="https://jira.example",
            external_id="OPS-42",
        )
        snapshot = await self.source.write_owner(identity, "acct-77")
        self.assertEqual(snapshot.owner_references[0].provider_id, "acct-77")

        await self.source.add_comment(identity, "Investigating")
        self.assertEqual(self.client.comments[-1][0], "OPS-42")

        transitions = await self.source.available_transitions(identity)
        self.assertEqual(
            self.suite.validate_transition_names(self.source, transitions),
            ("Start Progress", "Resolve"),
        )
        resolved = await self.source.transition(identity, "resolve")
        self.assertEqual(self.client.transition_calls[-1], ("OPS-42", "31"))
        self.assertEqual(self.source.project(resolved).stage, "closed")

        with self.assertRaises(UnsupportedTaskSourceCapability):
            await self.source.write_state(identity, "closed")
        with self.assertRaises(UnsupportedTaskSourceCapability):
            await self.source.attach_artifact(identity, "https://example/artifact")


if __name__ == "__main__":
    unittest.main()
