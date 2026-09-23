from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.approval_requests import (
    ApprovalConsumeRequest,
    ApprovalDecisionOutcome,
    ApprovalDecisionSubmit,
    ApprovalRequestCreate,
    ApprovalRequestStatus,
    ApprovalRequirement,
    ApprovalTarget,
)
from codex_web.canonical_events import CanonicalEventType
from codex_web.identity import (
    AuthenticationAssurance,
    HumanIdentity,
    Membership,
    MembershipRole,
    PrincipalKind,
    TenantScope,
)
from codex_web.services.approval_requests import (
    ApprovalAtomicMutation,
    ApprovalEligibilityError,
    ApprovalRequestService,
    ApprovalStateError,
    StaleApprovalTargetError,
)
from codex_web.services.canonical_events import (
    CanonicalEventBus,
    CanonicalEventIngestionService,
)
from codex_web.services.identity import IdentityService
from codex_web.services.scheduler import SchedulerService
from codex_web.storage.approval_requests import ApprovalRequestStore
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.scheduler import SchedulerStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class MutableClock:
    def __init__(self, value: float) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value


class CanonicalApprovalRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.identity_store = IdentityStateStore(self.sqlite)
        self.identity = IdentityService(self.identity_store)
        self.identity.bootstrap_local()

        self.clock = MutableClock(100.0)
        self.event_store = CanonicalEventStore(self.sqlite)
        self.event_bus = CanonicalEventBus(self.event_store)
        self.ingestion = CanonicalEventIngestionService(self.event_bus)
        self.scheduler_store = SchedulerStore(self.sqlite)
        self.scheduler = SchedulerService(
            self.scheduler_store,
            self.ingestion,
            clock=self.clock,
            owner_id="scheduler-test",
        )
        self.store = ApprovalRequestStore(self.sqlite)
        self.service = ApprovalRequestService(
            self.store,
            self.identity,
            self.ingestion,
            scheduler=self.scheduler,
            clock=self.clock,
        )

        self.requester = self._human_actor(
            "requester",
            MembershipRole.MEMBER,
        )
        self.approver_a = self._human_actor(
            "approver-a",
            MembershipRole.APPROVER,
        )
        self.approver_b = self._human_actor(
            "approver-b",
            MembershipRole.APPROVER,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _human_actor(self, identity_id: str, role: MembershipRole):
        def seed(state):
            if not any(item.id == identity_id for item in state.humans):
                state.humans.append(
                    HumanIdentity(
                        id=identity_id,
                        display_name=identity_id,
                    )
                )
            return state

        self.identity_store.update(seed)
        self.identity.add_membership(
            Membership(
                identity_id=identity_id,
                principal_kind=PrincipalKind.HUMAN,
                organization_id="local",
                workspace_id="default",
                roles=[role],
            )
        )
        credentials = self.identity.create_session(
            identity_id=identity_id,
            scope=TenantScope(),
            assurance=AuthenticationAssurance.MFA,
        )
        return self.identity.authenticate_session(
            credentials.session_token,
            touch=False,
        ).actor

    @staticmethod
    def _target(version: str = "r1") -> ApprovalTarget:
        return ApprovalTarget(
            operation="definition.publish",
            object_type="definition",
            object_id="authority-role-catalog",
            target_version=version,
            target_digest=f"sha256:{version}",
            resource_ids=("resource-a",),
        )

    async def _request(
        self,
        *,
        quorum: int = 1,
        expires_at: float | None = None,
        target: ApprovalTarget | None = None,
        project_id: str | None = None,
    ):
        return await self.service.create(
            ApprovalRequestCreate(
                target=target or self._target(),
                project_id=project_id,
                reason="review sensitive change",
                policy_source="policy:test",
                authority_source="authority:test",
                requirement=ApprovalRequirement(
                    quorum=quorum,
                    required_assurance=AuthenticationAssurance.MFA,
                    membership_roles=(MembershipRole.APPROVER,),
                    allow_self_approval=False,
                ),
                expires_at=expires_at,
            ),
            requester=self.requester,
        )

    async def _approve(self, request_id: str, actor, key: str):
        return await self.service.decide(
            request_id,
            ApprovalDecisionSubmit(
                outcome=ApprovalDecisionOutcome.APPROVE,
                reason="approved",
                idempotency_key=key,
            ),
            actor=actor,
        )

    async def test_two_person_quorum_is_distinct_and_duplicate_click_is_idempotent(self) -> None:
        request = await self._request(quorum=2)

        partial = await self._approve(
            request.id,
            self.approver_a,
            "decision-a",
        )
        self.assertEqual(
            partial.status,
            ApprovalRequestStatus.PARTIALLY_APPROVED,
        )
        self.assertEqual(len(partial.decisions), 1)

        duplicate = await self._approve(
            request.id,
            self.approver_a,
            "decision-a",
        )
        self.assertEqual(
            duplicate.status,
            ApprovalRequestStatus.PARTIALLY_APPROVED,
        )
        self.assertEqual(len(duplicate.decisions), 1)
        self.assertEqual(duplicate.revision, partial.revision)

        approved = await self._approve(
            request.id,
            self.approver_b,
            "decision-b",
        )
        self.assertEqual(approved.status, ApprovalRequestStatus.APPROVED)
        self.assertEqual(len(approved.decisions), 2)
        self.assertEqual(
            {item.identity_id for item in approved.decisions},
            {"approver-a", "approver-b"},
        )

    async def test_requester_self_approval_fails_closed(self) -> None:
        request = await self._request()

        with self.assertRaisesRegex(
            ApprovalEligibilityError,
            "requester cannot approve",
        ):
            await self._approve(
                request.id,
                self.requester,
                "self-decision",
            )

        self.assertEqual(
            self.store.get(request.id).status,
            ApprovalRequestStatus.PENDING,
        )

    async def test_revoked_session_cannot_approve(self) -> None:
        request = await self._request()
        self.identity.revoke_session(
            self.approver_a.session_id,
            reason="test revoke",
        )

        with self.assertRaisesRegex(
            ApprovalEligibilityError,
            "revoked or expired",
        ):
            await self._approve(
                request.id,
                self.approver_a,
                "revoked-session",
            )

    async def test_stale_target_is_durably_invalidated_on_consumption(self) -> None:
        request = await self._request()
        await self._approve(request.id, self.approver_a, "approve-r1")

        with self.assertRaises(StaleApprovalTargetError):
            await self.service.consume(
                request.id,
                ApprovalConsumeRequest(
                    target=self._target("r2"),
                    idempotency_key="consume-r2",
                    resulting_operation_reference="definition-record-r2",
                ),
                actor=self.requester,
            )

        invalidated = self.store.get(request.id)
        self.assertEqual(
            invalidated.status,
            ApprovalRequestStatus.INVALIDATED,
        )
        self.assertIn(
            "does not match",
            invalidated.invalidation_reason or "",
        )

    async def test_approval_events_include_project_attribution(self) -> None:
        request = await self._request(project_id="project-a")
        events = [
            item
            for item in self.event_store.recent(limit=20)
            if item.event_type == CanonicalEventType.APPROVAL.value
            and item.payload.get("approval_request_id") == request.id
        ]

        self.assertTrue(events)
        self.assertEqual(events[0].payload.get("project_id"), "project-a")

    async def test_scheduler_expiry_survives_as_durable_timer_and_event(self) -> None:
        request = await self._request(expires_at=110.0)
        self.assertIsNotNone(request.expiry_schedule_id)
        schedule = self.scheduler_store.get(str(request.expiry_schedule_id))
        self.assertEqual(schedule.next_run_at, 110.0)
        self.assertEqual(
            schedule.payload["approval_request_id"],
            request.id,
        )

        self.clock.value = 110.0
        result = await self.scheduler.run_due()

        self.assertEqual(result.emitted, 1)
        expired = self.store.get(request.id)
        self.assertEqual(expired.status, ApprovalRequestStatus.EXPIRED)
        event_types = {
            item.event_type
            for item in self.event_store.recent(limit=20)
        }
        self.assertIn(CanonicalEventType.SCHEDULE.value, event_types)
        self.assertIn(CanonicalEventType.APPROVAL.value, event_types)

    async def test_cancel_and_supersede_are_terminal_and_attributable(self) -> None:
        cancelled = await self._request()
        cancelled = await self.service.cancel(
            cancelled.id,
            actor=self.requester,
        )
        self.assertEqual(
            cancelled.status,
            ApprovalRequestStatus.CANCELLED,
        )
        self.assertEqual(
            cancelled.cancelled_by_identity_id,
            self.requester.identity_id,
        )

        superseded = await self._request()
        superseded = await self.service.supersede(
            superseded.id,
            replacement_request_id="approval-replacement",
            actor=self.requester,
        )
        self.assertEqual(
            superseded.status,
            ApprovalRequestStatus.SUPERSEDED,
        )
        self.assertEqual(
            superseded.superseded_by_request_id,
            "approval-replacement",
        )

    async def test_live_membership_revocation_invalidates_quorum_at_consumption(self) -> None:
        request = await self._request()
        await self._approve(request.id, self.approver_a, "approval-a")

        def revoke_membership(state):
            for index, item in enumerate(state.memberships):
                if (
                    item.identity_id == self.approver_a.identity_id
                    and item.revoked_at is None
                ):
                    state.memberships[index] = item.model_copy(
                        update={"revoked_at": 101.0}
                    )
            return state

        self.identity_store.update(revoke_membership)

        with self.assertRaisesRegex(
            ApprovalStateError,
            "quorum is no longer eligible",
        ):
            await self.service.consume(
                request.id,
                ApprovalConsumeRequest(
                    target=self._target(),
                    idempotency_key="consume-after-revoke",
                    resulting_operation_reference="definition-record-r1",
                ),
                actor=self.requester,
            )
        self.assertEqual(
            self.store.get(request.id).status,
            ApprovalRequestStatus.APPROVED,
        )

    async def test_atomic_consumption_commits_guarded_mutation_once(self) -> None:
        request = await self._request()
        await self._approve(request.id, self.approver_a, "approve-atomic")

        payload = ApprovalConsumeRequest(
            target=self._target(),
            idempotency_key="consume-atomic",
            resulting_operation_reference="action-intent-1",
        )
        mutation = ApprovalAtomicMutation(
            namespace="guarded-test",
            default={"value": 0},
            apply=lambda document: (
                {"value": int(document["value"]) + 1},
                int(document["value"]) + 1,
            ),
        )

        consumed, result = await self.service.consume_with_document(
            request.id,
            payload,
            actor=self.requester,
            mutation=mutation,
        )

        self.assertEqual(consumed.status, ApprovalRequestStatus.CONSUMED)
        self.assertEqual(result, 1)
        self.assertEqual(self.sqlite.get("guarded-test"), {"value": 1})

        repeated = await self.service.consume(
            request.id,
            payload,
            actor=self.requester,
        )
        self.assertEqual(repeated.status, ApprovalRequestStatus.CONSUMED)
        self.assertEqual(self.sqlite.get("guarded-test"), {"value": 1})

    async def test_failed_guarded_mutation_rolls_back_approval_consumption(self) -> None:
        request = await self._request()
        await self._approve(request.id, self.approver_a, "approve-rollback")

        def fail(_document):
            raise RuntimeError("guarded mutation failed")

        with self.assertRaisesRegex(RuntimeError, "guarded mutation failed"):
            await self.service.consume_with_document(
                request.id,
                ApprovalConsumeRequest(
                    target=self._target(),
                    idempotency_key="consume-rollback",
                    resulting_operation_reference="action-intent-failed",
                ),
                actor=self.requester,
                mutation=ApprovalAtomicMutation(
                    namespace="guarded-rollback",
                    default={"value": 0},
                    apply=fail,
                ),
            )

        self.assertEqual(
            self.store.get(request.id).status,
            ApprovalRequestStatus.APPROVED,
        )
        self.assertIsNone(self.sqlite.get("guarded-rollback"))


if __name__ == "__main__":
    unittest.main()
