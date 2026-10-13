from __future__ import annotations

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
from codex_web.models import TurnCreate
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore


def actor(*, admin: bool) -> AuthenticationActor:
    return AuthenticationActor(
        identity_id="admin" if admin else "member",
        principal_kind=PrincipalKind.HUMAN,
        organization_id="org",
        workspace_id="ws",
        roles=(
            (MembershipRole.ADMIN,)
            if admin
            else (MembershipRole.MEMBER,)
        ),
        assurance=AuthenticationAssurance.MFA,
    )


class ExecutionPreflightServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "state.sqlite3"
        self.clock_value = 1_800_000_000.0
        self.store = ExecutionPreflightStore(SQLiteStateStore(self.db))
        self.service = ExecutionPreflightService(
            self.store,
            clock=lambda: self.clock_value,
        )
        self.admin = actor(admin=True)
        self.member = actor(admin=False)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_scoped_route_repair_rejects_mutation_and_accepts_late_original_receipt(self):
        from codex_web.models import QueuedTurn, QueuedNativeStartReceipt, BotReplyTarget
        from codex_web.storage.thread_index import ThreadIndexRepository
        routes = ThreadIndexRepository(self.store.store, Path(self.temp.name) / "threads.json")
        routes.remember_replacement("old", "new", project_id="project",
            provenance={"organization_id":"org", "workspace_id":"ws"})
        self.service.replacement_resolver = routes.replacement_thread_id
        q = QueuedTurn(id="q", thread_id="old", project_id="project", message="original",
            source="web", execution_id="original-execution", sandbox="workspace-write",
            approval_policy="on-request", created_at=100,
            agent_profile_id="carl", agent_profile_revision=6,
            repository_resource_id="repo", writable_repository_resource_ids=("repo",),
            reply_target=BotReplyTarget(thread_id="old", provider="slack",
                external_conversation_id="channel", message_id="message", updated_at=100))
        retained = self.service.retain_queued(q, organization_id="org", workspace_id="ws")
        _, claim = self.service.claim_queued_admission(retained.id)
        self.service.begin_queued_start(retained.id, claim_id=claim)
        moved = q.model_copy(update={"thread_id":"new",
            "reply_target":q.reply_target.model_copy(update={"thread_id":"new"})})
        for update in [{"message":"changed"}, {"agent_profile_revision":7},
                       {"writable_repository_resource_ids":("foreign",)},
                       {"reply_target":moved.reply_target.model_copy(update={"external_conversation_id":"other"})}]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.service.retain_queued(moved.model_copy(update=update), organization_id="org", workspace_id="ws")
            self.assertIsNone(self.store.get(retained.id).replacement_thread_id)
        with self.assertRaises(ValueError):
            self.service.retain_queued(moved, organization_id="foreign", workspace_id="ws")
        with self.assertRaises(ValueError):
            self.service.retain_queued(moved.model_copy(update={"thread_id":"unverified"}),
                organization_id="org", workspace_id="ws")
        repaired = self.service.retain_queued(moved, organization_id="org", workspace_id="ws")
        self.assertEqual(repaired.queued_turn_snapshot, q.model_dump(mode="json"))
        self.assertEqual(repaired.status, "reconciliation_required")
        receipt = QueuedNativeStartReceipt(thread_id="old", admission_claim_id=claim,
            requested_execution_id="original-execution", execution_id="bootstrap",
            provider_native_turn_id="actual-original-turn", provider_native_session_id="old",
            provider_id="openai", runtime_id="codex", observed_at=200)
        started = self.service.record_queued_native_start(retained.id, receipt)
        self.assertEqual(started.queued_start_receipt, receipt)
        self.assertEqual(started.replacement_thread_id, "new")

    def test_route_repair_fences_active_admission(self):
        from codex_web.models import QueuedTurn
        from codex_web.storage.thread_index import ThreadIndexRepository
        routes = ThreadIndexRepository(self.store.store, Path(self.temp.name) / "threads.json")
        routes.remember_replacement("old", "new", project_id="project",
            provenance={"organization_id":"org", "workspace_id":"ws"})
        self.service.replacement_resolver = routes.replacement_thread_id
        q = QueuedTurn(id="q", thread_id="old", project_id="project", message="original",
            source="web", sandbox=None, approval_policy=None, created_at=100)
        retained = self.service.retain_queued(q, organization_id="org", workspace_id="ws")
        self.service.claim_queued_admission(retained.id)
        with self.assertRaises(ValueError):
            self.service.retain_queued(q.model_copy(update={"thread_id":"new"}),
                organization_id="org", workspace_id="ws")
        self.assertIsNone(self.store.get(retained.id).replacement_thread_id)

    def _record(self):
        return self.service.record_blocked(
            actor=self.admin,
            thread_id="thread-1",
            project_id="project-1",
            execution_id="thread-turn-abc",
            payload=TurnCreate(
                message="make the change",
                project_id="project-1",
            ),
            effective={
                "sandbox": "workspace-write",
                "approval_policy": "on-request",
                "execution_profile_id": "repository-write",
            },
            detail={
                "code": "execution_preflight_blocked",
                "message": "repository target is ambiguous",
                "blockers": [
                    {
                        "code": "repository_target_ambiguous",
                        "message": "repository target is ambiguous",
                        "retryable": False,
                        "target_type": "repository",
                        "remediation_route": "/api/projects/project-1/resources",
                    }
                ],
            },
        )

    def test_blocked_attempt_survives_store_and_service_reload(self) -> None:
        created = self._record()

        reloaded = ExecutionPreflightService(
            ExecutionPreflightStore(SQLiteStateStore(self.db)),
            clock=lambda: self.clock_value,
        )
        items = reloaded.for_thread(
            "thread-1",
            actor=self.member,
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, created.id)
        self.assertEqual(items[0].message, "make the change")
        self.assertEqual(items[0].correlation_id, "thread-turn-abc")
        self.assertEqual(
            items[0].blockers[0].code,
            "repository_target_ambiguous",
        )
        self.assertEqual(len(items[0].blocker_history), 1)

    def test_execution_identity_cannot_be_rebound_to_different_message(self) -> None:
        self._record()

        with self.assertRaises(HTTPException) as caught:
            self.service.record_blocked(
                actor=self.admin,
                thread_id="thread-1",
                project_id="project-1",
                execution_id="thread-turn-abc",
                payload=TurnCreate(
                    message="different message",
                    project_id="project-1",
                ),
                effective={
                    "sandbox": "workspace-write",
                    "approval_policy": "on-request",
                },
                detail={
                    "code": "execution_preflight_blocked",
                    "message": "still blocked",
                    "blockers": [
                        {
                            "code": "worker_capability_missing",
                            "message": "still blocked",
                        }
                    ],
                },
            )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail["code"],
            "execution_preflight_identity_conflict",
        )

    def test_retry_requires_admin_authority(self) -> None:
        attempt = self._record()

        with self.assertRaises(HTTPException) as caught:
            self.service.claim_retry(
                attempt.id,
                actor=self.member,
            )

        self.assertEqual(caught.exception.status_code, 403)
        public = self.service.public(attempt, actor=self.member)
        self.assertFalse(public["can_retry"])

    def test_duplicate_retry_claims_are_deduplicated_and_stale_claim_recovers(self) -> None:
        attempt = self._record()

        first, first_claim, first_acquired = self.service.claim_retry(
            attempt.id,
            actor=self.admin,
        )
        second, second_claim, second_acquired = self.service.claim_retry(
            attempt.id,
            actor=self.admin,
        )

        self.assertTrue(first_acquired)
        self.assertFalse(second_acquired)
        self.assertEqual(second_claim, first_claim)
        self.assertEqual(first.attempt_number, 2)
        self.assertEqual(second.attempt_number, 2)

        reloaded = ExecutionPreflightService(
            ExecutionPreflightStore(SQLiteStateStore(self.db)),
            clock=lambda: self.clock_value,
        )
        _same, same_claim, same_acquired = reloaded.claim_retry(
            attempt.id,
            actor=self.admin,
        )
        self.assertFalse(same_acquired)
        self.assertEqual(same_claim, first_claim)

        self.clock_value += reloaded.RETRY_CLAIM_TTL_SECONDS + 1
        recovered, recovered_claim, recovered_acquired = reloaded.claim_retry(
            attempt.id,
            actor=self.admin,
        )
        self.assertTrue(recovered_acquired)
        self.assertNotEqual(recovered_claim, first_claim)
        self.assertEqual(recovered.attempt_number, 3)

    def test_replacement_binding_requires_current_claim_and_retains_original_identity(self):
        attempt = self._record()
        claimed, old_claim, _ = self.service.claim_retry(attempt.id, actor=self.admin)
        self.clock_value += self.service.RETRY_CLAIM_TTL_SECONDS + 1
        current, claim, _ = self.service.claim_retry(attempt.id, actor=self.admin)
        for denied_actor, denied_claim in ((self.admin, old_claim), (self.member, claim)):
            with self.assertRaises(HTTPException):
                self.service.bind_replacement(attempt.id, actor=denied_actor,
                    claim_id=denied_claim, previous_thread_id="thread-1",
                    replacement_thread_id="replacement")
        self.assertIsNone(self.service.get(attempt.id, actor=self.admin).replacement_thread_id)
        updated = self.service.bind_replacement(attempt.id, actor=self.admin,
            claim_id=claim, previous_thread_id="thread-1", replacement_thread_id="replacement")
        self.assertEqual(updated.thread_id, attempt.thread_id)
        self.assertEqual(updated.message, attempt.message)
        self.assertEqual(updated.execution_id, attempt.execution_id)
        reloaded = ExecutionPreflightService(ExecutionPreflightStore(SQLiteStateStore(self.db)))
        self.assertEqual(reloaded.for_thread("replacement", actor=self.admin)[0].id, attempt.id)
        with self.assertRaises(HTTPException):
            self.service.bind_replacement(attempt.id, actor=self.admin,
                claim_id=claim, previous_thread_id="thread-1", replacement_thread_id="unrelated")

    def test_started_attempt_is_terminal_for_retry_deduplication(self) -> None:
        attempt = self._record()
        claimed, claim_id, acquired = self.service.claim_retry(
            attempt.id,
            actor=self.admin,
        )
        self.assertTrue(acquired)

        started = self.service.mark_started(
            attempt.id,
            actor=self.admin,
            claim_id=claim_id,
        )
        again, retry_claim, retry_acquired = self.service.claim_retry(
            attempt.id,
            actor=self.admin,
        )

        self.assertEqual(claimed.attempt_number, 2)
        self.assertEqual(started.status, "started")
        self.assertEqual(again.status, "started")
        self.assertIsNone(retry_claim)
        self.assertFalse(retry_acquired)


if __name__ == "__main__":
    unittest.main()
