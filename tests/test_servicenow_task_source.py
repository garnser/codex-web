from __future__ import annotations

import unittest
from copy import deepcopy

from codex_web.models import TaskSourceIdentity
from codex_web.services.servicenow_task_source import (
    ServiceNowFieldMapping,
    ServiceNowTaskSource,
)
from codex_web.services.task_source_conformance import TaskSourceConformanceSuite
from codex_web.services.task_sources import (
    TaskSourceCapability,
    TaskSourceCreateRequest,
    TaskSourceReconciliationCursor,
    UnsupportedTaskSourceCapability,
)


def _dv(value, display=None):
    return {"value": value, "display_value": display if display is not None else value}


def _record(
    sys_id: str = "abc123",
    *,
    number: str = "INC0010042",
    updated: str = "2026-09-19 06:00:00",
):
    return {
        "sys_id": _dv(sys_id),
        "number": _dv(number),
        "short_description": _dv("API incident"),
        "description": _dv("Customers cannot authenticate"),
        "state": _dv("2", "In Progress"),
        "assigned_to": _dv("user42", "Dana Operator"),
        "priority": _dv("1", "1 - Critical"),
        "category": _dv("software", "Software"),
        "parent": _dv("parent01", "PRB0001"),
        "sys_updated_on": _dv(updated),
    }


class _FakeServiceNowClient:
    def __init__(self) -> None:
        self.records = {"abc123": _record()}
        self.list_calls = []
        self.created_payloads = []
        self.updated_payloads = []

    async def list_records(
        self,
        api_base,
        table,
        *,
        token,
        query,
        fields,
        limit,
        offset=0,
    ):
        self.list_calls.append((table, query, fields, limit, offset))
        values = list(self.records.values())
        return tuple(deepcopy(values[offset:offset + limit]))

    async def record(self, api_base, table, sys_id, *, token, fields):
        return deepcopy(self.records.get(sys_id, {}))

    async def create_record(self, api_base, table, *, token, payload):
        self.created_payloads.append((table, deepcopy(payload)))
        record = _record("new001", number="TASK001")
        record["short_description"] = _dv(payload["short_description"])
        record["description"] = _dv(payload.get("description", ""))
        if "assigned_to" in payload:
            record["assigned_to"] = _dv(payload["assigned_to"], payload["assigned_to"])
        self.records["new001"] = record
        return deepcopy(record)

    async def update_record(self, api_base, table, sys_id, *, token, payload):
        self.updated_payloads.append((table, sys_id, deepcopy(payload)))
        record = self.records[sys_id]
        if "assigned_to" in payload:
            value = payload["assigned_to"]
            record["assigned_to"] = _dv(value, value) if value else _dv("", "")
        if "state" in payload:
            value = payload["state"]
            display = {"2": "In Progress", "7": "Closed"}.get(value, value)
            record["state"] = _dv(value, display)
        return deepcopy(record)


class ServiceNowTaskSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = _FakeServiceNowClient()
        self.source = ServiceNowTaskSource(
            "https://instance.service-now.com",
            "credential",
            table="incident",
            canonical_state_values={
                "implementation_active": "2",
                "closed": "7",
            },
            client=self.client,
        )
        self.suite = TaskSourceConformanceSuite()

    def test_adapter_declares_capabilities_from_configuration(self) -> None:
        self.suite.validate_adapter(self.source)
        for capability in (
            TaskSourceCapability.CREATE,
            TaskSourceCapability.DISCOVERY,
            TaskSourceCapability.PAGED_DISCOVERY,
            TaskSourceCapability.READ,
            TaskSourceCapability.EVENTS,
            TaskSourceCapability.OWNER_WRITE,
            TaskSourceCapability.STATE_WRITE,
            TaskSourceCapability.COMMENTS,
            TaskSourceCapability.WORKFLOW_TRANSITIONS,
            TaskSourceCapability.INCREMENTAL_RECONCILIATION,
            TaskSourceCapability.PROVIDER_IDENTITIES,
        ):
            self.assertTrue(self.source.capabilities.supports(capability))
        self.assertFalse(self.source.capabilities.supports(TaskSourceCapability.ARTIFACT_LINKS))

    def test_state_capability_fails_closed_without_mapping(self) -> None:
        source = ServiceNowTaskSource(
            "https://instance.service-now.com",
            "credential",
            client=self.client,
        )
        self.suite.validate_adapter(source)
        self.assertFalse(source.capabilities.supports(TaskSourceCapability.STATE_WRITE))
        self.assertFalse(source.capabilities.supports(TaskSourceCapability.WORKFLOW_TRANSITIONS))

    async def test_discovery_uses_allowlisted_field_mapping_and_normalizes_display_values(self) -> None:
        page = await self.source.discover_page(
            scope="active=true^assignment_group=ops",
            limit=50,
        )
        self.suite.validate_page(self.source, page)
        snapshot = page.items[0]
        self.assertEqual(snapshot.identity.external_id, "abc123")
        self.assertEqual(snapshot.identity.event_cursor, "INC0010042")
        self.assertEqual(snapshot.title, "API incident")
        self.assertEqual(snapshot.source_state, "In Progress")
        self.assertEqual(snapshot.owner_references[0].provider_id, "user42")
        self.assertEqual(snapshot.owner_references[0].display_name, "Dana Operator")
        self.assertEqual(snapshot.priority, "1 - Critical")
        self.assertEqual(snapshot.category, "Software")
        self.assertEqual(snapshot.parent_external_id, "parent01")
        table, query, fields, limit, offset = self.client.list_calls[-1]
        self.assertEqual(table, "incident")
        self.assertEqual(query, "active=true^assignment_group=ops")
        self.assertIn("sys_id", fields)
        self.assertNotIn("password", fields)
        self.assertEqual((limit, offset), (50, 0))

    async def test_reconciliation_uses_compound_timestamp_and_sys_id_order(self) -> None:
        cursor = TaskSourceReconciliationCursor(
            watermark="2026-09-19 05:00:00",
            tiebreaker="aaa000",
        )
        page = await self.source.reconcile_since(
            scope="active=true",
            cursor=cursor,
        )
        self.suite.validate_page(self.source, page)
        self.assertEqual(
            page.watermark,
            "2026-09-19 06:00:00|abc123",
        )
        query = self.client.list_calls[-1][1]
        self.assertIn("sys_updated_on>=2026-09-19 05:00:00", query)
        self.assertIn("ORDERBYsys_updated_on^ORDERBYsys_id", query)

    async def test_create_does_not_inject_scope_into_provider_record(self) -> None:
        snapshot = await self.source.create(
            TaskSourceCreateRequest(
                title="Generated task",
                body="Investigate deterministically",
                owners=("user99",),
            ),
            scope="active=true",
        )
        self.suite.validate_snapshot(self.source, snapshot)
        table, payload = self.client.created_payloads[-1]
        self.assertEqual(table, "incident")
        self.assertEqual(payload["short_description"], "Generated task")
        self.assertEqual(payload["description"], "Investigate deterministically")
        self.assertEqual(payload["assigned_to"], "user99")
        self.assertNotIn("u_codex_scope", payload)

    async def test_event_projection_writeback_and_comments_are_provider_isolated(self) -> None:
        event = await self.source.normalize_event(
            {
                "event_type": "incident.updated",
                "record": _record(),
            }
        )
        self.assertIsNotNone(event)
        self.suite.validate_event(self.source, event)
        projection = self.source.project(event.snapshot)
        self.suite.validate_projection(self.source, event.snapshot, projection)
        self.assertEqual(projection.stage, "implementation_active")
        self.assertEqual(projection.owner, "Dana Operator")

        identity = TaskSourceIdentity(
            source_type="servicenow",
            source_instance="https://instance.service-now.com",
            external_id="abc123",
        )
        owner = await self.source.write_owner(identity, "user77")
        self.assertEqual(owner.owner_references[0].provider_id, "user77")

        closed = await self.source.write_state(identity, "closed")
        self.assertEqual(closed.source_state, "Closed")
        self.assertEqual(self.source.project(closed).stage, "closed")

        await self.source.add_comment(identity, "Validated")
        self.assertEqual(
            self.client.updated_payloads[-1][2],
            {"comments": "Validated"},
        )

        transitions = await self.source.available_transitions(identity)
        self.assertEqual(
            self.suite.validate_transition_names(self.source, transitions),
            ("implementation_active", "closed"),
        )
        reopened = await self.source.transition(identity, "implementation_active")
        self.assertEqual(reopened.source_state, "In Progress")

        with self.assertRaises(UnsupportedTaskSourceCapability):
            await self.source.attach_artifact(identity, "https://example/artifact")

    def test_custom_field_mapping_remains_adapter_local(self) -> None:
        mapping = ServiceNowFieldMapping(
            number="u_number",
            title="u_title",
            body="u_body",
            state="u_state",
            assignee="u_owner",
            priority="u_priority",
            category="u_category",
            parent="u_parent",
            updated="u_updated",
            sys_id="sys_id",
            comment="u_comment",
        )
        source = ServiceNowTaskSource(
            "https://instance.service-now.com",
            "credential",
            table="u_custom_task",
            fields=mapping,
            client=self.client,
        )
        self.assertEqual(source.table, "u_custom_task")
        self.assertEqual(source.fields.title, "u_title")
        self.assertNotEqual(source.fields.title, "short_description")


if __name__ == "__main__":
    unittest.main()
