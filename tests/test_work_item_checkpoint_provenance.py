from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_web.models import WorkItemState
from codex_web.services.work_item_execution import WorkItemExecutionLifecycleService
from codex_web.services.work_item_state import WorkItemStateMachine
from codex_web.work_item_execution_models import (
    WorkItemCheckpointCreate,
    WorkItemExecutionCheckpoint,
    WorkItemExecutionUpdate,
)


class _Host:
    DEFAULT_VALIDATION_OWNER = "quinn"
    DEFAULT_RELEASE_OWNER = "release manager"
    NON_IMPLEMENTATION_OWNERS = {"quinn", "release manager", "orchestrator"}

    def __init__(self, root: Path) -> None:
        self.DATA_DIR = root
        self.WORK_ITEM_EVENTS_FILE = root / "work_item_events.jsonl"
        self.states: dict[str, WorkItemState] = {}

    def _load_work_item_states(self):
        return {ref: state.model_copy(deep=True) for ref, state in self.states.items()}

    def _save_work_item_states(self, states):
        self.states = {ref: state.model_copy(deep=True) for ref, state in states.items()}

    @staticmethod
    def _leading_owner_cue_in_action(value):
        return None


class WorkItemCheckpointProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.host = _Host(root)
        self.machine = WorkItemStateMachine(self.host)
        self.service = WorkItemExecutionLifecycleService(self.host, self.machine)
        self.ref = "group/app#531"
        self.host.states[self.ref] = WorkItemState(
            ref=self.ref,
            project_id="app",
            project_path="group/app",
            current_owner="james",
            current_stage="implementation_active",
            last_meaningful_update_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_legacy_checkpoint_defaults_to_untrusted_delta_anchor(self) -> None:
        checkpoint = WorkItemExecutionCheckpoint(
            id="checkpoint-legacy",
            sequence=1,
            created_at=1.0,
            summary="Existing compact checkpoint",
        )

        self.assertFalse(checkpoint.delivery_proven)
        self.assertIsNone(checkpoint.delivery_proof_ref)
        self.assertIsNone(checkpoint.delivered_context_hash)
        self.assertIsNone(checkpoint.execution_id)
        self.assertEqual(checkpoint.definition_refs, [])

    def test_continuation_anchor_fails_closed_without_delivery_proof(self) -> None:
        self.service.checkpoint(
            self.ref,
            WorkItemCheckpointCreate(summary="Legacy-style checkpoint"),
        )

        assessment = self.service.continuation_anchor(self.ref)

        self.assertFalse(assessment["trusted"])
        self.assertEqual(assessment["reason"], "checkpoint_provenance_incomplete")
        self.assertIn("delivery_not_proven", assessment["blockers"])
        self.assertIn("work_item_hash_missing", assessment["blockers"])
        self.assertIn("event_watermark_missing", assessment["blockers"])

    def test_checkpoint_persists_positive_delivery_provenance_and_audit_fields(self) -> None:
        result = self.service.checkpoint(
            self.ref,
            WorkItemCheckpointCreate(
                actor="worker-a",
                source="execution-worker",
                reason="delivered continuation context",
                summary="Implementation context delivered.",
                execution_id="exec-531",
                execution_contract_version="work-item/1.4",
                agent_profile_id="agent-james",
                agent_profile_revision=7,
                role_id="implementer",
                provider_id="openai",
                runtime_id="codex",
                session_ref="session-safe-ref",
                work_item_revision="rev-42",
                work_item_hash="sha256:work-item",
                event_watermark="event-99",
                delivered_context_hash="sha256:context",
                delivery_proven=True,
                delivery_proof_ref="assignment-receipt-12",
            ),
        )

        checkpoint = result["checkpoint"]
        self.assertTrue(checkpoint["delivery_proven"])
        self.assertEqual(checkpoint["delivery_proof_ref"], "assignment-receipt-12")
        self.assertEqual(checkpoint["execution_id"], "exec-531")
        self.assertEqual(checkpoint["work_item_revision"], "rev-42")
        self.assertEqual(checkpoint["event_watermark"], "event-99")
        self.assertEqual(checkpoint["delivered_context_hash"], "sha256:context")

        history = self.service.history(self.ref)
        event = history["items"][0]
        self.assertTrue(event["payload"]["delivery_proven"])
        self.assertEqual(event["payload"]["delivery_proof_ref"], "assignment-receipt-12")
        self.assertEqual(event["payload"]["work_item_hash"], "sha256:work-item")

        assessment = self.service.continuation_anchor(self.ref)
        self.assertTrue(assessment["trusted"])
        self.assertEqual(assessment["reason"], "delivery_proven")
        self.assertEqual(assessment["blockers"], [])


    def _record_trusted_snapshot_checkpoint(self, *, schema_version="1.0"):
        snapshot = self.service.continuation_snapshot(self.ref)
        return self.service.checkpoint(
            self.ref,
            WorkItemCheckpointCreate(
                schema_version=schema_version,
                summary="Verified delivered context.",
                objective="Implement context delta continuation.",
                execution_id="exec-delta",
                work_item_revision=snapshot["work_item_revision"],
                work_item_hash=snapshot["work_item_hash"],
                event_watermark=snapshot["event_watermark"],
                delivered_context_hash="sha256:delivered-context",
                delivery_proven=True,
                delivery_proof_ref="assignment-receipt-delta",
            ),
        )

    def test_verified_delta_ignores_volatile_timestamp_and_detects_relevant_change(self) -> None:
        self.host.states[self.ref].title = "Initial title"
        self._record_trusted_snapshot_checkpoint()

        self.host.states[self.ref].updated_at = 9_999.0
        unchanged = self.service.continuation_delta(self.ref)
        self.assertEqual(unchanged["mode"], "delta")
        self.assertNotIn("updated_at", unchanged["changed_fields"])

        self.host.states[self.ref].title = "Changed title"
        changed = self.service.continuation_delta(self.ref)
        self.assertEqual(changed["mode"], "delta")
        self.assertEqual(changed["changed_fields"]["title"], "Changed title")
        self.assertGreaterEqual(changed["metrics"]["baseline_context_bytes"], 1)
        self.assertGreaterEqual(changed["metrics"]["delta_context_bytes"], 1)

    def test_events_after_verified_watermark_are_returned_and_bounded(self) -> None:
        self._record_trusted_snapshot_checkpoint()
        self.service.update(
            self.ref,
            WorkItemExecutionUpdate(
                actor="worker-a",
                source="test",
                reason="new canonical activity",
                retry_attempt=1,
            ),
        )

        delta = self.service.continuation_delta(self.ref, max_events=1)

        self.assertEqual(delta["mode"], "delta")
        self.assertGreaterEqual(delta["event_count_since_checkpoint"], 2)
        self.assertEqual(delta["event_offset"], 0)
        self.assertEqual(delta["events_returned"], 1)
        self.assertEqual(delta["next_event_offset"], 1)
        self.assertTrue(delta["requires_progressive_retrieval"])

        next_page = self.service.continuation_delta(
            self.ref,
            max_events=1,
            event_offset=delta["next_event_offset"],
        )
        self.assertEqual(next_page["event_offset"], 1)
        self.assertEqual(next_page["events_returned"], 1)
        self.assertNotEqual(next_page["events"], delta["events"])

    def test_corrupted_or_stale_event_watermark_falls_back_to_full_context(self) -> None:
        self.service.update(
            self.ref,
            WorkItemExecutionUpdate(
                actor="worker-a",
                source="test",
                reason="baseline event",
                retry_attempt=1,
            ),
        )
        self._record_trusted_snapshot_checkpoint()

        path = self.host.WORK_ITEM_EVENTS_FILE
        rows = path.read_text(encoding="utf-8").splitlines()
        first = json.loads(rows[0])
        first["reason"] = "tampered historical event"
        rows[0] = json.dumps(first)
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")

        delta = self.service.continuation_delta(self.ref)

        self.assertEqual(delta["mode"], "full")
        self.assertEqual(delta["reason"], "event_watermark_stale_or_corrupt")

    def test_incompatible_checkpoint_schema_falls_back_safely(self) -> None:
        self._record_trusted_snapshot_checkpoint(schema_version="0.9")

        delta = self.service.continuation_delta(self.ref)

        self.assertEqual(delta["mode"], "full")
        self.assertEqual(delta["reason"], "checkpoint_schema_incompatible")
        self.assertEqual(delta["checkpoint_schema_version"], "0.9")

    def test_checkpoint_records_continuation_usage_for_run_inspection(self) -> None:
        snapshot = self.service.continuation_snapshot(self.ref)
        result = self.service.checkpoint(
            self.ref,
            WorkItemCheckpointCreate(
                summary="Run completed with delta context.",
                execution_id="exec-delta-observed",
                work_item_revision=snapshot["work_item_revision"],
                work_item_hash=snapshot["work_item_hash"],
                event_watermark=snapshot["event_watermark"],
                delivered_context_hash="sha256:delivered",
                delivery_proven=True,
                delivery_proof_ref="receipt-delta-observed",
                continuation_mode="delta",
                continuation_reason="verified_checkpoint_delta",
                continuation_checkpoint_id="checkpoint-previous",
                context_baseline_bytes=4096,
                context_delta_bytes=512,
                estimated_tokens_reused=896,
            ),
        )

        self.assertEqual(
            result["checkpoint"]["continuation_mode"],
            "delta",
        )
        run_context = self.service.run_context(
            self.ref,
            "exec-delta-observed",
        )
        self.assertIsNotNone(run_context)
        assert run_context is not None
        self.assertEqual(run_context["continuation"]["mode"], "delta")
        self.assertEqual(
            run_context["continuation"]["checkpointId"],
            "checkpoint-previous",
        )
        self.assertEqual(
            run_context["continuation"]["metrics"]["estimatedTokensReused"],
            896,
        )

    def test_runtime_delivery_records_current_snapshot_as_next_trusted_anchor(self) -> None:
        self.host.states[self.ref].title = "Current canonical title"
        selection = self.service.continuation_delta(self.ref)
        self.assertEqual(selection["mode"], "full")

        result = self.service.record_continuation_delivery(
            self.ref,
            "exec-runtime-delivery",
            selection,
            {
                "mode": "full",
                "reason": selection["reason"],
                "work_item_context": selection["snapshot"]["work_item_context"],
            },
            provenance={
                "execution_contract_version": "work-item/1.4",
                "provider_id": "openai",
                "runtime_id": "codex",
                "session_ref": "thread-531",
            },
        )

        checkpoint = result["checkpoint"]
        self.assertTrue(checkpoint["delivery_proven"])
        self.assertEqual(checkpoint["execution_id"], "exec-runtime-delivery")
        self.assertEqual(checkpoint["continuation_mode"], "full")
        self.assertEqual(
            checkpoint["fallback_to_full_context_reason"],
            selection["reason"],
        )
        self.assertEqual(
            checkpoint["delivered_work_item_context"]["title"],
            "Current canonical title",
        )

        pending = self.service.continuation_anchor(self.ref)
        self.assertFalse(pending["trusted"])
        self.assertEqual(
            pending["reason"],
            "checkpoint_execution_untrusted",
        )
        self.assertIn(
            "execution_outcome_pending",
            pending["blockers"],
        )

        outcome = self.service.record_continuation_outcome(
            self.ref,
            "exec-runtime-delivery",
            "succeeded",
        )
        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertTrue(outcome["continuation_eligible"])

        anchor = self.service.continuation_anchor(self.ref)
        self.assertTrue(anchor["trusted"])
        self.assertEqual(
            anchor["checkpoint"]["execution_id"],
            "exec-runtime-delivery",
        )
        self.assertEqual(
            anchor["checkpoint"]["execution_outcome"],
            "succeeded",
        )

    def test_failed_lost_and_poisoned_runtime_checkpoints_never_anchor(self) -> None:
        for outcome in ("failed", "lost", "poisoned", "ambiguous"):
            with self.subTest(outcome=outcome):
                self.host.states[self.ref].execution.latest_checkpoint = None
                self.host.states[self.ref].execution.checkpoint_history = []
                selection = self.service.continuation_delta(self.ref)
                self.service.record_continuation_delivery(
                    self.ref,
                    f"exec-{outcome}",
                    selection,
                    {
                        "mode": "full",
                        "reason": selection["reason"],
                        "work_item_context": selection["snapshot"][
                            "work_item_context"
                        ],
                    },
                )
                self.service.record_continuation_outcome(
                    self.ref,
                    f"exec-{outcome}",
                    outcome,
                )

                anchor = self.service.continuation_anchor(self.ref)
                self.assertFalse(anchor["trusted"])
                self.assertEqual(
                    anchor["reason"],
                    "checkpoint_execution_untrusted",
                )
                self.assertIn(
                    f"execution_outcome_{outcome}",
                    anchor["blockers"],
                )

    def test_proven_checkpoint_without_verified_baseline_does_not_claim_delta(self) -> None:
        self.service.checkpoint(
            self.ref,
            WorkItemCheckpointCreate(
                summary="Old producer with provenance only.",
                execution_id="exec-old",
                work_item_revision="legacy-revision",
                work_item_hash="sha256:" + ("a" * 64),
                event_watermark="wi-events-v1:0:" + ("b" * 64),
                delivered_context_hash="sha256:context",
                delivery_proven=True,
                delivery_proof_ref="receipt-old",
            ),
        )

        delta = self.service.continuation_delta(self.ref)

        self.assertEqual(delta["mode"], "full")
        self.assertEqual(delta["reason"], "checkpoint_baseline_missing")


if __name__ == "__main__":
    unittest.main()
