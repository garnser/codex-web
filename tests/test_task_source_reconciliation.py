from __future__ import annotations

import unittest

from codex_web.models import TaskSourceIdentity
from codex_web.services.task_source_reconciliation import (
    TaskSourceCanonicalProjection,
    TaskSourceReconciliationOutcome,
    TaskSourceReconciliationPolicy,
    same_task_source_identity,
    task_source_event_key,
    task_source_projection_drift,
)
from codex_web.services.task_sources import TaskSourceEvent, TaskSourceSnapshot


class TaskSourceReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = TaskSourceIdentity(
            source_type="gitlab",
            source_instance="https://gitlab.example/api/v4",
            external_id="group/project#42",
            revision="2026-09-17T19:00:00Z",
        )
        self.policy = TaskSourceReconciliationPolicy()

    def _event(
        self,
        *,
        occurred_at: float | None = 10.0,
        cursor: str | None = None,
        source_state: str = "opened",
    ) -> TaskSourceEvent:
        identity = self.identity.model_copy(update={"event_cursor": cursor})
        snapshot = TaskSourceSnapshot(
            identity=identity,
            title="Issue",
            source_state=source_state,
            owners=("James",),
            labels=("priority::P1",),
        )
        return TaskSourceEvent(
            identity=identity,
            event_type="issue.updated",
            occurred_at=occurred_at,
            snapshot=snapshot,
        )

    def test_same_identity_normalizes_source_type_and_instance_trailing_slash(self) -> None:
        equivalent = TaskSourceIdentity(
            source_type="GITLAB",
            source_instance="https://gitlab.example/api/v4/",
            external_id="group/project#42",
        )
        self.assertTrue(same_task_source_identity(self.identity, equivalent))

    def test_different_external_item_is_authority_conflict(self) -> None:
        incoming_identity = self.identity.model_copy(update={"external_id": "group/project#43"})
        event = TaskSourceEvent(identity=incoming_identity, event_type="issue.updated")

        decision = self.policy.evaluate(event, current_identity=self.identity)

        self.assertEqual(decision.outcome, TaskSourceReconciliationOutcome.CONFLICT)
        self.assertFalse(decision.should_apply)

    def test_cursor_is_preferred_as_stable_idempotency_key(self) -> None:
        first = task_source_event_key(self._event(cursor="cursor-1"))
        same = task_source_event_key(self._event(cursor="cursor-1", source_state="closed"))
        second = task_source_event_key(self._event(cursor="cursor-2"))

        self.assertEqual(first, same)
        self.assertNotEqual(first, second)
        self.assertIn("cursor:cursor-1", first)

    def test_fallback_key_is_stable_over_normalized_event_facts(self) -> None:
        first = task_source_event_key(self._event(cursor=None))
        same = task_source_event_key(self._event(cursor=None))
        changed = task_source_event_key(self._event(cursor=None, source_state="closed"))

        self.assertEqual(first, same)
        self.assertNotEqual(first, changed)
        self.assertIn("sha256:", first)

    def test_duplicate_event_is_rejected_deterministically(self) -> None:
        event = self._event(cursor="cursor-1")
        event_key = task_source_event_key(event)

        decision = self.policy.evaluate(
            event,
            current_identity=self.identity,
            last_event_at=9.0,
            last_event_key=event_key,
        )

        self.assertEqual(decision.outcome, TaskSourceReconciliationOutcome.DUPLICATE)
        self.assertFalse(decision.should_apply)

    def test_older_event_is_stale(self) -> None:
        event = self._event(occurred_at=9.0)

        decision = self.policy.evaluate(
            event,
            current_identity=self.identity,
            last_event_at=10.0,
        )

        self.assertEqual(decision.outcome, TaskSourceReconciliationOutcome.STALE)
        self.assertFalse(decision.should_apply)

    def test_new_event_is_applied(self) -> None:
        event = self._event(occurred_at=11.0)

        decision = self.policy.evaluate(
            event,
            current_identity=self.identity,
            last_event_at=10.0,
        )

        self.assertEqual(decision.outcome, TaskSourceReconciliationOutcome.APPLY)
        self.assertTrue(decision.should_apply)

    def test_missing_provider_timestamp_does_not_invent_ordering(self) -> None:
        event = self._event(occurred_at=None)

        decision = self.policy.evaluate(
            event,
            current_identity=self.identity,
            last_event_at=10.0,
        )

        self.assertEqual(decision.outcome, TaskSourceReconciliationOutcome.APPLY)

    def test_projection_drift_reports_canonical_stage_and_owner_differences(self) -> None:
        projection = TaskSourceCanonicalProjection(
            identity=self.identity,
            stage="validation_running",
            owner="quinn",
            source_state="in review",
        )

        findings = task_source_projection_drift(
            canonical_stage="implementation_active",
            canonical_owner="James",
            projection=projection,
        )

        self.assertEqual(
            [finding.code for finding in findings],
            ["source_stage_drift", "source_owner_drift"],
        )
        self.assertEqual(findings[0].canonical_value, "implementation_active")
        self.assertEqual(findings[0].projected_value, "validation_running")

    def test_unknown_provider_owner_does_not_create_false_owner_drift(self) -> None:
        projection = TaskSourceCanonicalProjection(
            identity=self.identity,
            stage="implementation_active",
            owner=None,
            owner_known=False,
        )

        findings = task_source_projection_drift(
            canonical_stage="implementation_active",
            canonical_owner="james",
            projection=projection,
        )

        self.assertEqual(findings, ())


if __name__ == "__main__":
    unittest.main()
