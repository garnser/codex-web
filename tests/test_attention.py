from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.attention import (
    AttentionItemCreate,
    AttentionSeverity,
    AttentionSource,
    AttentionStatus,
)
from codex_web.canonical_events import CanonicalEventType
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.attention import (
    AttentionService,
    install_attention_event_bridges,
)
from codex_web.services.canonical_events import (
    CanonicalEventBus,
    CanonicalEventIngestionService,
)
from codex_web.services.scheduler import SchedulerService
from codex_web.storage.attention import AttentionStore
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.scheduler import SchedulerStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class _FailingNotifier:
    async def deliver(self, item) -> None:
        raise RuntimeError("provider down")


class AttentionServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.event_store = CanonicalEventStore(sqlite)
        self.bus = CanonicalEventBus(self.event_store)
        self.ingestion = CanonicalEventIngestionService(self.bus)
        self.clock = _Clock()
        self.scheduler_store = SchedulerStore(sqlite)
        self.scheduler = SchedulerService(
            self.scheduler_store,
            self.ingestion,
            clock=self.clock,
        )
        self.store = AttentionStore(sqlite)
        self.service = AttentionService(
            self.store,
            self.ingestion,
            scheduler=self.scheduler,
            clock=self.clock,
        )
        self.unsubscribe = install_attention_event_bridges(self.bus, self.service)
        self.actor = AuthenticationActor(
            identity_id="operator-a",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA,
        )

    def tearDown(self) -> None:
        self.unsubscribe()
        self.temp.cleanup()

    async def _approval_event(
        self,
        status: str,
        *,
        key: str,
        expires_at: float | None = None,
    ):
        return await self.ingestion.ingest(
            event_type=CanonicalEventType.APPROVAL,
            source="approval:approval-1",
            idempotency_key=key,
            payload={
                "approval_request_id": "approval-1",
                "status": status,
                "expires_at": expires_at,
            },
            tenant_id="local",
            workspace_id="default",
        )

    async def test_approval_events_dedupe_and_resolution_use_one_attention_item(self) -> None:
        await self._approval_event("pending", key="pending")
        await self._approval_event("partially_approved", key="partial")

        items = self.store.list()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source.object_id, "approval-1")
        self.assertEqual(items[0].status, AttentionStatus.OPEN)

        await self._approval_event("approved", key="approved")
        resolved = self.store.list()[0]
        self.assertEqual(resolved.status, AttentionStatus.RESOLVED)
        self.assertEqual(resolved.resolved_by_identity_id, "approval-bridge")

        await self._approval_event("invalidated", key="invalidated")
        reopened = self.store.list()[0]
        self.assertEqual(reopened.status, AttentionStatus.OPEN)
        self.assertEqual(reopened.type, "approval.invalidated")

    async def test_list_page_is_bounded_cursor_paginated_and_filterable(self) -> None:
        for index in range(5):
            await self.service.upsert(
                AttentionItemCreate(
                    organization_id="local",
                    workspace_id="default",
                    project_id=("project-a" if index < 3 else "project-b"),
                    type=f"test.attention.{index}",
                    severity=(
                        AttentionSeverity.CRITICAL
                        if index in {1, 3}
                        else AttentionSeverity.WARNING
                    ),
                    source=AttentionSource(
                        object_type="work_item",
                        object_id=f"work-{index}",
                    ),
                    reason=f"Needs human attention {index}",
                    dedupe_key=f"test-attention-{index}",
                    owner_identity_id=("operator-a" if index in {0, 2} else "operator-b"),
                ),
                actor_id="test",
            )

        first, next_cursor, total = self.service.list_page(
            self.actor,
            limit=2,
            cursor=0,
        )
        self.assertEqual(len(first), 2)
        self.assertEqual(total, 5)
        self.assertEqual(next_cursor, 2)

        second, final_cursor, second_total = self.service.list_page(
            self.actor,
            limit=10,
            cursor=next_cursor,
        )
        self.assertEqual(len(second), 3)
        self.assertEqual(second_total, 5)
        self.assertIsNone(final_cursor)

        critical, critical_cursor, critical_total = self.service.list_page(
            self.actor,
            limit=100,
            severity="critical",
        )
        self.assertEqual(len(critical), 2)
        self.assertEqual(critical_total, 2)
        self.assertIsNone(critical_cursor)
        self.assertTrue(all(item.severity == AttentionSeverity.CRITICAL for item in critical))

        await self.service.resolve(first[0].id, actor=self.actor, reason="handled")
        active, active_cursor, active_total = self.service.list_page(
            self.actor,
            limit=100,
            status="active",
        )
        self.assertEqual(len(active), 4)
        self.assertEqual(active_total, 4)
        self.assertIsNone(active_cursor)
        self.assertTrue(all(item.status != AttentionStatus.RESOLVED for item in active))

        project_items, _, project_total = self.service.list_page(
            self.actor,
            limit=100,
            project_id="project-b",
        )
        self.assertEqual(project_total, 2)
        self.assertTrue(all(item.project_id == "project-b" for item in project_items))

        typed, _, typed_total = self.service.list_page(
            self.actor,
            limit=100,
            item_type="test.attention.3",
        )
        self.assertEqual(typed_total, 1)
        self.assertEqual(typed[0].type, "test.attention.3")

        assigned, _, assigned_total = self.service.list_page(
            self.actor,
            limit=100,
            assignee="me",
        )
        self.assertEqual(assigned_total, 2)
        self.assertTrue(
            all(item.owner_identity_id == self.actor.identity_id for item in assigned)
        )

    async def test_attention_state_v1_migrates_without_inventing_project(self) -> None:
        item = await self.service.upsert(
            AttentionItemCreate(
                organization_id="local",
                workspace_id="default",
                type="runtime.remediation",
                source=AttentionSource(object_type="runtime", object_id="runtime-1"),
                reason="Runtime needs attention",
                dedupe_key="migration-runtime-1",
            ),
            actor_id="test",
        )
        legacy_item = item.model_dump(mode="json")
        legacy_item.pop("project_id", None)
        self.store.store.put(
            self.store.namespace,
            {
                "schema_version": "1.0",
                "items": {item.id: legacy_item},
            },
        )

        migrated = self.store.load()
        self.assertEqual(migrated.schema_version, "1.1")
        self.assertIsNone(migrated.items[item.id].project_id)

    async def test_notification_provider_failure_cannot_lose_canonical_item(self) -> None:
        self.service.register_notification_adapter(_FailingNotifier())

        await self._approval_event("pending", key="provider-down")

        items = self.store.list()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].status, AttentionStatus.OPEN)

    async def test_acknowledge_and_resolve_are_attributable(self) -> None:
        await self._approval_event("pending", key="pending-a")
        item = self.store.list()[0]

        acknowledged = await self.service.acknowledge(item.id, actor=self.actor)
        self.assertEqual(acknowledged.status, AttentionStatus.ACKNOWLEDGED)
        self.assertEqual(acknowledged.acknowledged_by_identity_id, "operator-a")

        resolved = await self.service.resolve(
            item.id,
            actor=self.actor,
            reason="handled",
        )
        self.assertEqual(resolved.status, AttentionStatus.RESOLVED)
        self.assertEqual(resolved.resolved_by_identity_id, "operator-a")
        self.assertEqual(resolved.resolution_reason, "handled")

    async def test_scheduler_backed_escalation_is_durable_and_idempotent(self) -> None:
        await self._approval_event("pending", key="pending-expiring", expires_at=110.0)
        item = self.store.list()[0]
        self.assertIsNotNone(item.escalation_schedule_id)
        schedule = self.scheduler_store.get(str(item.escalation_schedule_id))
        self.assertEqual(schedule.next_run_at, 110.0)

        self.clock.value = 110.0
        result = await self.scheduler.run_due()
        self.assertEqual(result.emitted, 1)

        escalated = self.store.get(item.id)
        self.assertEqual(escalated.status, AttentionStatus.ESCALATED)
        self.assertEqual(escalated.escalation_count, 1)

        again = await self.scheduler.run_due()
        self.assertEqual(again.emitted, 0)
        self.assertEqual(self.store.get(item.id).escalation_count, 1)


if __name__ == "__main__":
    unittest.main()
