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
