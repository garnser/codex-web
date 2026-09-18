from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.entitlements import (
    CapabilityEntitlementUpdate,
    EntitlementMode,
    QuotaBehavior,
    QuotaPolicyUpdate,
    QuotaWindow,
    UsageEventCreate,
)
from codex_web.services.entitlements import (
    EntitlementDeniedError,
    EntitlementService,
    QuotaExceededError,
)
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.storage.entitlements import EntitlementStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class EntitlementServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.identity = IdentityService(IdentityStateStore(sqlite))
        self.identity.bootstrap_local()
        self.actor = self.identity.local_trusted_actor()
        self.service = EntitlementService(EntitlementStore(sqlite))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _enforce(self) -> None:
        self.service.set_mode(EntitlementMode.ENFORCED, actor=self.actor)
        self.service.set_capability(
            "external_actions",
            CapabilityEntitlementUpdate(enabled=True, source="test-plan"),
            actor=self.actor,
        )

    def test_default_self_hosted_mode_is_explicitly_unlimited(self) -> None:
        decision = self.service.check(
            "external_actions",
            actor=self.actor,
            metric="external_action_attempts",
            projected_amount=1000,
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.mode, EntitlementMode.SELF_HOSTED_UNLIMITED)
        self.assertEqual(decision.reason, "self_hosted_unlimited")

    def test_enforced_mode_requires_capability_and_tracks_changes(self) -> None:
        self.service.set_mode(EntitlementMode.ENFORCED, actor=self.actor)
        denied = self.service.check("external_actions", actor=self.actor)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.reason, "capability_not_entitled")

        self.service.set_capability(
            "external_actions",
            CapabilityEntitlementUpdate(enabled=True),
            actor=self.actor,
        )
        self.assertTrue(
            self.service.check("external_actions", actor=self.actor).allowed
        )

        self.service.set_capability(
            "external_actions",
            CapabilityEntitlementUpdate(enabled=False),
            actor=self.actor,
        )
        disabled = self.service.check("external_actions", actor=self.actor)
        self.assertFalse(disabled.allowed)
        self.assertEqual(disabled.reason, "capability_disabled")

    def test_atomic_hard_quota_consumption_is_idempotent(self) -> None:
        self._enforce()
        self.service.set_quota(
            "external_action_attempts",
            QuotaPolicyUpdate(
                limit=2,
                window=QuotaWindow.LIFETIME,
                behavior=QuotaBehavior.HARD_STOP,
            ),
            actor=self.actor,
        )
        first = self.service.consume(
            "external_actions",
            UsageEventCreate(
                idempotency_key="attempt-1",
                metric="external_action_attempts",
                amount=1,
            ),
            actor=self.actor,
        )
        duplicate = self.service.consume(
            "external_actions",
            UsageEventCreate(
                idempotency_key="attempt-1",
                metric="external_action_attempts",
                amount=1,
            ),
            actor=self.actor,
        )
        self.assertFalse(first.duplicate)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(
            self.service.usage_total(
                self.actor,
                metric="external_action_attempts",
            ),
            1,
        )

        self.service.consume(
            "external_actions",
            UsageEventCreate(
                idempotency_key="attempt-2",
                metric="external_action_attempts",
                amount=1,
            ),
            actor=self.actor,
        )
        with self.assertRaises(QuotaExceededError):
            self.service.consume(
                "external_actions",
                UsageEventCreate(
                    idempotency_key="attempt-3",
                    metric="external_action_attempts",
                    amount=1,
                ),
                actor=self.actor,
            )

    def test_late_usage_is_attributed_to_original_window(self) -> None:
        self._enforce()
        day = 86_400.0
        self.service.set_quota(
            "model_tokens",
            QuotaPolicyUpdate(
                limit=100,
                window=QuotaWindow.DAY,
                behavior=QuotaBehavior.HARD_STOP,
            ),
            actor=self.actor,
        )
        self.service.record_usage(
            UsageEventCreate(
                idempotency_key="late-day-one",
                metric="model_tokens",
                amount=90,
                occurred_at=day + 100,
                source="reconciliation",
            ),
            actor=self.actor,
        )
        decision = self.service.check(
            "external_actions",
            actor=self.actor,
            metric="model_tokens",
            projected_amount=20,
            at=(day * 2) + 100,
        )
        self.assertTrue(decision.allowed)
        self.assertIsNotNone(decision.quota)
        self.assertEqual(decision.quota.usage, 0)

    def test_non_hard_quota_reports_excess_without_denying(self) -> None:
        self._enforce()
        self.service.set_quota(
            "storage_gb",
            QuotaPolicyUpdate(
                limit=1,
                window=QuotaWindow.LIFETIME,
                behavior=QuotaBehavior.GRACE,
            ),
            actor=self.actor,
        )
        result = self.service.consume(
            "external_actions",
            UsageEventCreate(
                idempotency_key="storage-1",
                metric="storage_gb",
                amount=2,
            ),
            actor=self.actor,
        )
        self.assertTrue(result.decision.allowed)
        self.assertTrue(result.decision.quota.exceeded)
        self.assertEqual(result.decision.reason, "quota_exceeded_grace")

    def test_entitlement_check_does_not_grant_administration(self) -> None:
        self._enforce()
        member = self.actor.model_copy(
            update={"roles": ()}
        )
        self.assertTrue(
            self.service.check("external_actions", actor=member).allowed
        )
        with self.assertRaises(AuthorizationError):
            self.service.set_capability(
                "other",
                CapabilityEntitlementUpdate(enabled=True),
                actor=member,
            )


if __name__ == "__main__":
    unittest.main()
