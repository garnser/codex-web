from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.models import WorkItemState
from codex_web.services.work_item_execution import WorkItemExecutionLifecycleService
from codex_web.services.work_item_state import WorkItemStateMachine
from codex_web.work_item_execution_models import (
    WorkItemCheckpointCreate,
    WorkItemExecutionCheckpoint,
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


if __name__ == "__main__":
    unittest.main()
