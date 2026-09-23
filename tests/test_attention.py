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
    AttentionStateError,
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


class _IdentityResolver:
    def __init__(self, valid_ids):
        self.valid_ids = set(valid_ids)

    def actor_for_identity(self, identity_id, *, scope):
        if identity_id not in self.valid_ids:
            raise RuntimeError("identity not found")
        return AuthenticationActor(
            identity_id=identity_id,
            principal_kind=PrincipalKind.HUMAN,
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            roles=(MembershipRole.MEMBER,),
            assurance=AuthenticationAssurance.MFA,
        )


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
        self.store.store.delete(self.store.record_namespace)
        self.store.store.delete(self.store.index_namespace)
        self.store.store.put(
            self.store.namespace,
            {
                "schema_version": "1.0",
                "items": {item.id: legacy_item},
            },
        )

        migrated = self.store.load()
        self.assertEqual(migrated.schema_version, "1.2")
        self.assertIsNone(migrated.items[item.id].project_id)
        self.assertIsNone(migrated.items[item.id].requesting_agent_profile_id)
        self.assertIsNone(migrated.items[item.id].requesting_agent_team_id)
        self.assertEqual(migrated.items[item.id].evidence_ids, ())
        self.assertEqual(migrated.items[item.id].diagnostic_refs, ())
        self.assertTrue(
            self.store.store.record_collection_exists(
                self.store.record_namespace
            )
        )
        self.assertFalse(self.store.store.contains(self.store.namespace))

    async def test_list_page_uses_index_without_full_list_scan(self) -> None:
        for index in range(3):
            await self.service.upsert(
                AttentionItemCreate(
                    organization_id="local",
                    workspace_id="default",
                    type="runtime.remediation",
                    source=AttentionSource(
                        object_type="runtime",
                        object_id=f"indexed-runtime-{index}",
                    ),
                    reason=f"Indexed remediation {index}",
                    dedupe_key=f"indexed-remediation-{index}",
                ),
                actor_id="test",
            )

        original_list = self.store.list
        self.store.list = lambda: (_ for _ in ()).throw(
            AssertionError("full Attention list scan is forbidden")
        )
        try:
            page, cursor, total = self.service.list_page(
                self.actor,
                limit=2,
                status="active",
            )
        finally:
            self.store.list = original_list

        self.assertEqual(len(page), 2)
        self.assertEqual(total, 3)
        self.assertEqual(cursor, 2)

    async def test_missing_query_index_rebuilds_from_canonical_records(self) -> None:
        item = await self.service.upsert(
            AttentionItemCreate(
                organization_id="local",
                workspace_id="default",
                project_id="project-index",
                type="runtime.remediation",
                source=AttentionSource(
                    object_type="runtime",
                    object_id="runtime-index-rebuild",
                ),
                reason="Rebuild the query index",
                dedupe_key="index-rebuild",
            ),
            actor_id="test",
        )
        self.store.store.delete(self.store.index_namespace)

        page, _, total = self.service.list_page(
            self.actor,
            limit=10,
            project_id="project-index",
        )

        self.assertEqual(total, 1)
        self.assertEqual(page[0].id, item.id)
        index = self.store.store.get(self.store.index_namespace)
        self.assertEqual(index.get("version"), self.store.index_version)

    async def test_dedupe_key_is_scoped_by_tenant_workspace(self) -> None:
        first = await self.service.upsert(
            AttentionItemCreate(
                organization_id="local",
                workspace_id="default",
                type="runtime.remediation",
                source=AttentionSource(object_type="runtime", object_id="runtime-local"),
                reason="Local remediation",
                dedupe_key="shared-provider-failure",
            ),
            actor_id="test",
        )
        second = await self.service.upsert(
            AttentionItemCreate(
                organization_id="other-org",
                workspace_id="other-workspace",
                type="runtime.remediation",
                source=AttentionSource(object_type="runtime", object_id="runtime-other"),
                reason="Other tenant remediation",
                dedupe_key="shared-provider-failure",
            ),
            actor_id="test",
        )

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(len(self.store.list()), 2)

    async def test_attention_preserves_agent_and_evidence_provenance(self) -> None:
        item = await self.service.upsert(
            AttentionItemCreate(
                organization_id="local",
                workspace_id="default",
                project_id="project-a",
                type="agent.request",
                source=AttentionSource(
                    object_type="work_item",
                    object_id="work-provenance",
                ),
                reason="Agent needs operator evidence",
                dedupe_key="agent-evidence-provenance",
                requesting_agent_profile_id="agent-profile-a",
                requesting_agent_team_id="team-a",
                evidence_ids=("evidence-1", "evidence-1", "evidence-2"),
                diagnostic_refs=("run:exec-1", "run:exec-1", "incident:7"),
            ),
            actor_id="test",
        )

        persisted = self.store.get(item.id)
        self.assertEqual(persisted.requesting_agent_profile_id, "agent-profile-a")
        self.assertEqual(persisted.requesting_agent_team_id, "team-a")
        self.assertEqual(persisted.evidence_ids, ("evidence-1", "evidence-2"))
        self.assertEqual(persisted.diagnostic_refs, ("run:exec-1", "incident:7"))

    async def test_notification_provider_failure_cannot_lose_canonical_item(self) -> None:
        self.service.register_notification_adapter(_FailingNotifier())

        await self._approval_event("pending", key="provider-down")

        items = self.store.list()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].status, AttentionStatus.OPEN)

    async def test_approval_attention_cannot_be_resolved_locally(self) -> None:
        await self._approval_event("pending", key="pending-source-owned")
        item = self.store.list()[0]

        with self.assertRaisesRegex(
            AttentionStateError,
            "canonical ApprovalRequest",
        ):
            await self.service.resolve(
                item.id,
                actor=self.actor,
                reason="dismiss locally",
            )

        self.assertEqual(
            self.store.get(item.id).status,
            AttentionStatus.OPEN,
        )

    async def test_acknowledge_and_resolve_are_attributable(self) -> None:
        item = await self.service.upsert(
            AttentionItemCreate(
                organization_id="local",
                workspace_id="default",
                type="runtime.remediation",
                source=AttentionSource(
                    object_type="runtime",
                    object_id="runtime-a",
                ),
                reason="Runtime requires operator remediation",
                dedupe_key="runtime-a-remediation",
            ),
            actor_id="runtime-bridge",
        )

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

    async def test_reassign_validates_identity_and_records_new_owner(self) -> None:
        self.service.identity = _IdentityResolver({"operator-a", "operator-b"})
        item = await self.service.upsert(
            AttentionItemCreate(
                organization_id="local",
                workspace_id="default",
                type="runtime.remediation",
                source=AttentionSource(object_type="runtime", object_id="runtime-reassign"),
                reason="Assign remediation owner",
                dedupe_key="runtime-reassign",
                owner_identity_id="operator-a",
            ),
            actor_id="runtime-bridge",
        )

        reassigned = await self.service.reassign(
            item.id,
            actor=self.actor,
            owner_identity_id="operator-b",
        )
        self.assertEqual(reassigned.owner_identity_id, "operator-b")
        self.assertEqual(reassigned.updated_by, "operator-a")

        with self.assertRaisesRegex(
            AttentionStateError,
            "current tenant/workspace identity",
        ):
            await self.service.reassign(
                item.id,
                actor=self.actor,
                owner_identity_id="missing-identity",
            )

    async def test_manual_escalation_is_owner_or_admin_controlled(self) -> None:
        item = await self.service.upsert(
            AttentionItemCreate(
                organization_id="local",
                workspace_id="default",
                type="runtime.remediation",
                source=AttentionSource(object_type="runtime", object_id="runtime-escalate"),
                reason="Escalate remediation",
                dedupe_key="runtime-escalate",
                owner_identity_id="operator-a",
                recipient_identity_ids=("operator-outsider",),
            ),
            actor_id="runtime-bridge",
        )

        escalated = await self.service.escalate_for_actor(
            item.id,
            actor=self.actor,
        )
        self.assertEqual(escalated.status, AttentionStatus.ESCALATED)
        self.assertEqual(escalated.escalation_count, 1)
        self.assertEqual(escalated.updated_by, "operator-a")

        outsider = self.actor.model_copy(
            update={
                "identity_id": "operator-outsider",
                "roles": (MembershipRole.MEMBER,),
            }
        )
        with self.assertRaisesRegex(
            Exception,
            "current owner or administrator",
        ):
            await self.service.escalate_for_actor(
                item.id,
                actor=outsider,
            )

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
