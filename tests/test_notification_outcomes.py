import json
import unittest

from codex_web.artifact_evidence import (
    EvidenceCreate, EvidenceLifecycle, EvidenceRequirement, EvidenceResult, EvidenceType,
    VerificationCreate, VerificationResult,
)
from codex_web.notification_outcomes import (
    NotificationOriginalOutcomeBinding, OUTCOME_METADATA_KEY,
    OUTCOME_REVIEW_METHOD, original_snapshot_digest,
)
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.notification_batch_inspection import NotificationBatchInspection
from codex_web.services.notification_outcomes import NotificationOutcomeInspection
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class NotificationOutcomeTests(unittest.TestCase):
    def setUp(self):
        # Reuse the actual admission/native-receipt fixture without collecting
        # the imported TestCase as a second test suite.
        from tests.test_notification_batch_inspection import NotificationBatchInspectionTests
        self.fixture = NotificationBatchInspectionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.test_native_receipt_is_shared_and_original_completion_stays_unresolved()
        self.batch = self.fixture.store.get_notification_batch(self.fixture.batch.id)
        self.actor = self.fixture.actor
        self.evidence_store = ArtifactEvidenceStore(self.fixture.state)
        self.evidence = ArtifactEvidenceService(self.evidence_store)
        self.now = 10**12
        self.fixture.inspector = NotificationBatchInspection(
            self.fixture.store, outcomes=NotificationOutcomeInspection(self.evidence), clock=lambda: self.now,
        )

    def proof(self, *, index=0, disposition="reconciled", review=True, reviewer=None, **changes):
        raw = self.batch.plan.original_snapshots_json[index]
        original = json.loads(raw)
        binding = NotificationOriginalOutcomeBinding(
            admission_id=self.batch.id, original_id=original["id"],
            original_snapshot_sha256=original_snapshot_digest(raw), disposition=disposition,
        )
        values = dict(
            project_id=original["project_id"], work_item_ref=original["work_item_ref"],
            execution_id="fixture-execution", external_id="fixture-turn", provider="fixture-provider",
            evidence_type=EvidenceType.RUNTIME_RESULT,
            result=EvidenceResult.PASS if disposition == "reconciled" else EvidenceResult.WARN,
            summary="Synthetic independently reviewed notification disposition",
            metadata={OUTCOME_METADATA_KEY: binding.model_dump_json()},
        )
        values.update(changes)
        item = self.evidence.create_evidence(EvidenceCreate(**values), actor=self.actor)
        if review:
            self.verify(item, actor=reviewer)
        return item

    def verify(self, item, *, actor=None, **changes):
        values = dict(
            work_item_ref=item.work_item_ref, execution_id=item.execution_id,
            evidence_ids=(item.id,), method=OUTCOME_REVIEW_METHOD, result=VerificationResult.VERIFIED,
        )
        values.update(changes)
        return self.evidence.create_verification(
            VerificationCreate(**values), actor=actor or self.actor.model_copy(update={"identity_id": "independent-reviewer"}),
        )

    def outcome(self, index=0):
        return self.fixture.detail()["originals"][index]

    def change_evidence(self, item, **changes):
        def update(state):
            state.evidence = [row.model_copy(update=changes) if row.id == item.id else row for row in state.evidence]
            return state
        self.evidence_store.update(update)

    def test_reviewed_per_original_results_counts_and_restart_are_read_only(self):
        first = self.proof()
        second = self.proof(index=1, disposition="follow_up_required")
        before = self.fixture.state.namespace_revision(self.fixture.store.NAMESPACE)
        evidence_before = self.evidence_store.load()
        self.assertEqual(self.outcome()["outcome"], "reconciled")
        self.assertEqual(self.outcome(1)["outcome"], "follow_up_required")
        self.assertEqual(self.outcome()["evidence_ids"], [first.id])
        self.assertEqual(self.outcome(1)["evidence_ids"], [second.id])
        self.assertEqual(self.fixture.history()["items"][0]["outcome_counts"], {
            "reconciled": 1, "follow_up_required": 1, "unresolved": 0,
        })
        self.fixture.inspector = NotificationBatchInspection(
            ExecutionPreflightStore(SQLiteStateStore(self.fixture.state.path)),
            outcomes=NotificationOutcomeInspection(ArtifactEvidenceService(
                ArtifactEvidenceStore(SQLiteStateStore(self.fixture.state.path)))), clock=lambda: self.now,
        )
        self.assertEqual(self.outcome()["outcome"], "reconciled")
        self.assertEqual(self.fixture.state.namespace_revision(self.fixture.store.NAMESPACE), before)
        self.assertEqual(self.evidence_store.load(), evidence_before)
        self.assertTrue(all(self.fixture.store.get(key).status == "reconciliation_required" for key in self.batch.attempt_ids))

    def test_aggregate_runtime_result_does_not_reconcile_any_original(self):
        self.proof(metadata={})
        self.assertEqual(self.outcome()["outcome"], "unresolved")
        self.assertEqual(self.outcome()["blockers"], ["per_original_evidence_required"])

    def test_manual_notification_reconciliation_is_not_generic_business_completion(self):
        self.proof()
        self.assertEqual(self.outcome()["outcome"], "reconciled")
        gate = self.evidence.evaluate(self.batch.plan.members[0].work_item_ref,
            [EvidenceRequirement(id="runtime-completion", evidence_type=EvidenceType.RUNTIME_RESULT)], actor=self.actor)
        self.assertFalse(gate.satisfied)

    def test_unreviewed_or_expired_metadata_cannot_revoke_current_verified_proof(self):
        self.proof()
        self.proof(review=False)
        expired = self.proof()
        self.change_evidence(expired, retention_expires_at=self.now)
        self.assertEqual(self.outcome()["outcome"], "reconciled")
        self.assertEqual(self.outcome()["blockers"], [])
        self.assertEqual(self.outcome()["warnings"], ["evidence_inactive_or_expired", "independent_review_required"])

    def test_self_review_or_unreviewed_model_assertion_cannot_grant_completion(self):
        self.proof(review=False)
        self.proof(reviewer=self.actor)
        self.assertEqual(self.outcome()["blockers"], ["independent_review_required"])
        self.assertEqual(self.outcome()["outcome"], "unresolved")

    def test_wrong_execution_native_turn_project_work_item_provider_and_snapshot_fail_closed(self):
        for changes in (
            {"execution_id": "requested-1"}, {"external_id": "other-turn"},
            {"project_id": "foreign"}, {"work_item_ref": "other/repo#1"},
            {"provider": "other-provider"}, {"evidence_type": EvidenceType.USER_OUTCOME},
            {"result": EvidenceResult.INFO},
        ):
            with self.subTest(changes=changes):
                item = self.proof(**changes)
                self.assertEqual(self.outcome()["outcome"], "unresolved")
                self.assertIn("execution_or_scope_binding_mismatch", self.outcome()["blockers"])
                self.change_evidence(item, metadata={})
        item = self.proof(review=False)
        metadata = json.loads(item.metadata[OUTCOME_METADATA_KEY])
        metadata["original_snapshot_sha256"] = "a" * 64
        self.change_evidence(item, metadata={OUTCOME_METADATA_KEY: json.dumps(metadata)})
        self.verify(item)
        self.assertIn("original_binding_mismatch", self.outcome()["blockers"])

    def test_foreign_tenant_and_other_admission_proof_are_not_exposed(self):
        item = self.proof()
        self.change_evidence(item, workspace_id="foreign")
        self.assertEqual(self.outcome()["evidence_ids"], [])
        self.assertEqual(self.outcome()["outcome"], "unresolved")
        binding = json.loads(item.metadata[OUTCOME_METADATA_KEY])
        binding["admission_id"] = "different-admission"
        self.change_evidence(item, workspace_id="default", metadata={OUTCOME_METADATA_KEY: json.dumps(binding)})
        self.assertEqual(self.outcome()["outcome"], "unresolved")

    def test_expired_or_invalidated_evidence_revokes_projected_result(self):
        item = self.proof()
        self.assertEqual(self.outcome()["outcome"], "reconciled")
        self.change_evidence(item, retention_expires_at=self.now)
        self.assertEqual(self.outcome()["outcome"], "unresolved")
        self.change_evidence(item, retention_expires_at=None, lifecycle=EvidenceLifecycle.INVALIDATED)
        self.assertIn("evidence_inactive_or_expired", self.outcome()["blockers"])

    def test_rejected_or_conflicting_reviews_and_dispositions_do_not_pick_latest_success(self):
        item = self.proof()
        self.verify(item, result=VerificationResult.REJECTED)
        self.assertEqual(self.outcome()["outcome"], "unresolved")
        self.assertIn("review_not_verified_or_conflicting", self.outcome()["blockers"])
        self.proof(index=1)
        self.proof(index=1, disposition="follow_up_required")
        self.assertEqual(self.outcome(1)["outcome"], "unresolved")
        self.assertIn("conflicting_dispositions", self.outcome(1)["blockers"])

    def test_review_requires_exact_method_execution_and_single_evidence_target(self):
        item = self.proof(review=False)
        for changes in ({"method": "general review"}, {"execution_id": "other-execution"}):
            self.verify(item, **changes)
            self.assertEqual(self.outcome()["outcome"], "unresolved")
        second = self.proof(index=1, review=False)
        self.verify(item, evidence_ids=(item.id, second.id), work_item_ref=None)
        self.assertEqual(self.outcome()["outcome"], "unresolved")
        self.verify(item, work_item_ref=None)
        # Canonical verification fills an omitted work-item from its evidence;
        # that record is valid and must not be rejected solely for omission.
        self.assertEqual(self.outcome()["outcome"], "reconciled")

    def test_missing_retention_keeps_proof_visible_but_outcome_unresolved(self):
        item = self.proof()
        self.fixture.state.record_apply(self.fixture.store.NAMESPACE, upserts={}, deletes=("a/" + self.batch.attempt_ids[0],))
        self.assertEqual(self.outcome()["outcome"], "unresolved")
        self.assertEqual(self.outcome()["evidence_ids"], [item.id])
        self.assertIn("retained_original_changed_or_missing", self.outcome()["blockers"])

    def test_malformed_unsupported_or_oversized_binding_cannot_be_a_result(self):
        for raw in ("not json", '{"schema_version":"2.0"}', "x" * 4097):
            self.proof(metadata={OUTCOME_METADATA_KEY: raw})
        self.assertEqual(self.outcome()["outcome"], "unresolved")
