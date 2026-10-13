from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.models import (
    QueuedNativeStartReceipt,
    QueuedSchedulingRetirement,
    QueuedTurn,
    TaskSourceIdentity,
    WorkItemState,
)
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.queued_scheduling_retirement import QueuedSchedulingRetirementService
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.turn_queue import TurnQueueRepository


def actor(*, organization_id: str = "org", workspace_id: str = "ws") -> AuthenticationActor:
    return AuthenticationActor(
        identity_id="admin",
        principal_kind=PrincipalKind.HUMAN,
        organization_id=organization_id,
        workspace_id=workspace_id,
        roles=(MembershipRole.ADMIN,),
        assurance=AuthenticationAssurance.MFA,
    )


class QueuedSchedulingRetirementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        state = SQLiteStateStore(root / "state.sqlite")
        self.clock_value = 200.0
        self.preflight = ExecutionPreflightService(
            ExecutionPreflightStore(state), clock=lambda: self.clock_value,
        )
        self.queues = TurnQueueRepository(state, root / "turn_queues.json")
        self.queued = QueuedTurn(
            id="obsolete", thread_id="thread", project_id="project",
            message="old request", execution_id="old-execution",
            work_item_ref="group/repo#43", repository_resource_id="repo",
            writable_repository_resource_ids=("repo",),
            read_only_repository_resource_ids=("context",),
            execution_profile_id="repository-write",
            agent_profile_id="quinn", agent_profile_revision=3,
            agent_profile_actor_id="requester", source="work-item-handoff",
            created_at=100,
        )
        self.later = self.queued.model_copy(update={
            "id": "later", "message": "new request", "execution_id": "new-execution",
            "work_item_ref": "group/repo#266", "created_at": 101,
        })
        self.queues.put("thread", [self.queued, self.later])
        retained = self.preflight.retain_queued(
            self.queued, organization_id="org", workspace_id="ws",
        )
        self.attempt = self.preflight.queued_disposition(
            retained.id,
            status="reconciliation_required",
            detail={"code": "native_start_unknown", "message": "response lost"},
        )
        identity = TaskSourceIdentity(
            source_type="gitlab", source_instance="https://git.example",
            external_id="group/repo#43", revision="provider-1",
        )
        self.item = WorkItemState(
            ref="group/repo#43", organization_id="org", workspace_id="ws",
            project_id="project", resource_ids=["repo"], source_identity=identity,
            current_stage="closed", terminal_outcome="completed",
            last_meaningful_update_at=150, updated_at=150, created_at=50,
        )
        self.source_state = "merged"
        self.active = set()
        self.audit = []

        async def source_detail(_ref):
            return {"snapshot": {
                "identity": self.item.source_identity.model_dump(mode="json"),
                "source_state": self.source_state,
            }}

        self.service = QueuedSchedulingRetirementService(
            preflight=self.preflight,
            queues=self.queues,
            work_item=lambda ref: self.item if ref == self.item.ref else (_ for _ in ()).throw(KeyError(ref)),
            source_detail=source_detail,
            thread_is_active=lambda thread_id: thread_id in self.active,
            append_audit=self.audit.append,
            clock=lambda: self.clock_value,
        )

    async def test_terminal_retirement_preserves_unknown_evidence_and_later_queue(self) -> None:
        original_snapshot = dict(self.attempt.queued_turn_snapshot)
        original_blockers = self.attempt.blockers

        result = await self.service.retire(
            self.attempt.id, thread_id="thread", actor=actor(),
        )

        self.assertEqual(self.queues.get("thread"), [self.later])
        self.assertEqual(result.status, "reconciliation_required")
        self.assertEqual(result.queued_turn_snapshot, original_snapshot)
        self.assertEqual(result.blockers, original_blockers)
        self.assertIsNone(result.queued_start_receipt)
        self.assertEqual(result.scheduling_retirement.state, "retired")
        self.assertEqual(result.scheduling_retirement.native_outcome, "unknown")
        self.assertEqual(result.scheduling_retirement.source_state, "merged")
        self.assertIsNotNone(result.scheduling_retirement.audit_recorded_at)
        self.assertEqual(self.audit[0].event_type, "queued_scheduling_retired")
        self.assertEqual(self.audit[0].payload["nativeOutcome"], "unknown")
        public = self.preflight.public(result, actor=actor())
        self.assertFalse(public["can_retry"])
        self.assertFalse(public["can_reconcile_prestart"])
        self.assertFalse(public["can_retire_scheduling"])

        same = await self.service.retire(
            self.attempt.id, thread_id="thread", actor=actor(),
        )
        self.assertEqual(same, result)
        self.assertEqual(len(self.audit), 1)

        receipt = QueuedNativeStartReceipt(
            admission_claim_id="claim", requested_execution_id="old-execution",
            execution_id="execution", thread_id="thread",
            provider_native_session_id="session", provider_native_turn_id="turn",
            provider_id="provider", runtime_id="runtime", observed_at=250,
        )
        with self.assertRaises(ValueError):
            self.preflight.record_queued_native_start(result.id, receipt)

    async def test_rejects_open_provider_active_thread_and_resource_drift(self) -> None:
        cases = (
            ("open provider", lambda: setattr(self, "source_state", "opened"), "scheduling_retirement_source_open"),
            ("active thread", lambda: self.active.add("thread"), "scheduling_retirement_turn_active"),
            ("resource drift", lambda: setattr(self.item, "resource_ids", ["other"]), "scheduling_retirement_resource_mismatch"),
        )
        for label, mutate, expected in cases:
            with self.subTest(label=label):
                mutate()
                with self.assertRaises(HTTPException) as caught:
                    await self.service.retire(self.attempt.id, thread_id="thread", actor=actor())
                self.assertEqual(caught.exception.detail["code"], expected)
                self.assertEqual(self.queues.get("thread"), [self.queued, self.later])
                self.source_state = "merged"
                self.active.clear()
                self.item.resource_ids = ["repo"]

    async def test_rejects_changed_queue_without_erasing_it(self) -> None:
        changed = self.queued.model_copy(update={"message": "mutated"})
        self.queues.put("thread", [changed, self.later])

        with self.assertRaises(HTTPException) as caught:
            await self.service.retire(self.attempt.id, thread_id="thread", actor=actor())

        self.assertEqual(caught.exception.detail["code"], "scheduling_retirement_queue_changed")
        self.assertEqual(self.queues.get("thread"), [changed, self.later])
        self.assertIsNone(self.preflight.store.get(self.attempt.id).scheduling_retirement)

    async def test_prepared_but_unconfirmed_removal_stays_fenced_and_unknown(self) -> None:
        evidence = QueuedSchedulingRetirement(
            queued_turn_id=self.queued.id,
            queued_snapshot_sha256=self.service._snapshot_digest(self.attempt.queued_turn_snapshot),
            work_item_ref=self.item.ref,
            source_identity=self.item.source_identity,
            source_state="merged", canonical_terminal_outcome="completed",
            retired_by="admin", prepared_at=190,
        )
        self.preflight.prepare_scheduling_retirement(
            self.attempt.id, evidence=evidence,
            expected_snapshot=self.attempt.queued_turn_snapshot,
        )
        self.queues.put("thread", [self.later])

        with self.assertRaises(HTTPException) as caught:
            await self.service.retire(self.attempt.id, thread_id="thread", actor=actor())

        self.assertEqual(caught.exception.detail["code"], "scheduling_retirement_reconciliation_required")
        retained = self.preflight.store.get(self.attempt.id)
        self.assertEqual(retained.status, "reconciliation_required")
        self.assertEqual(retained.scheduling_retirement.state, "prepared")
        self.assertIsNone(retained.scheduling_retirement.queue_removed_at)
        self.assertIsNone(self.preflight.claim_queued_admission(retained.id)[1])

    async def test_cross_tenant_attempt_is_not_visible(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            await self.service.retire(
                self.attempt.id,
                thread_id="thread",
                actor=actor(organization_id="other"),
            )
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(self.queues.get("thread"), [self.queued, self.later])

    async def test_concurrent_retirements_share_one_exact_removal_and_audit(self) -> None:
        both_read_provider = asyncio.Event()
        arrivals = 0
        original_reader = self.service.source_detail

        async def synchronized_reader(ref):
            nonlocal arrivals
            arrivals += 1
            if arrivals == 2:
                both_read_provider.set()
            await both_read_provider.wait()
            return await original_reader(ref)

        self.service.source_detail = synchronized_reader
        first, second = await asyncio.gather(
            self.service.retire(self.attempt.id, thread_id="thread", actor=actor()),
            self.service.retire(self.attempt.id, thread_id="thread", actor=actor()),
        )

        self.assertEqual(first, second)
        self.assertEqual(first.scheduling_retirement.state, "retired")
        self.assertEqual(self.queues.get("thread"), [self.later])
        self.assertEqual(len(self.audit), 1)


class TurnQueueExactRemovalTests(unittest.TestCase):
    def test_remove_exact_is_conditional_and_preserves_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = SQLiteStateStore(Path(directory) / "state.sqlite")
            queues = TurnQueueRepository(state, Path(directory) / "queues.json")
            first = QueuedTurn(id="one", thread_id="thread", project_id="p", message="one", created_at=1)
            second = QueuedTurn(id="two", thread_id="thread", project_id="p", message="two", created_at=2)
            queues.put("thread", [first, second])
            changed = first.model_copy(update={"message": "changed"})
            self.assertEqual(queues.remove_exact("thread", changed), "changed")
            self.assertEqual(queues.get("thread"), [first, second])
            self.assertEqual(queues.remove_exact("thread", first), "removed")
            self.assertEqual(queues.get("thread"), [second])
