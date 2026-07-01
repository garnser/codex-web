from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import server
from codex_web.models import WorkItemAckCreate, WorkItemHandoff, WorkItemHandoffCreate, WorkItemProgressUpdate, WorkItemState


class WorkItemLaneContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.data_dir = Path(self.tempdir.name) / "data"
        self.data_dir.mkdir()
        self.events_file = self.data_dir / "work_item_events.jsonl"
        self.bot_events_file = self.data_dir / "bot_events.jsonl"
        self.patches = [
            patch.object(server, "DATA_DIR", self.data_dir),
            patch.object(server, "WORK_ITEM_STATES_FILE", self.data_dir / "work_item_states.json"),
            patch.object(server, "WORK_ITEM_EVENTS_FILE", self.events_file),
            patch.object(server, "BOTS_EVENTS_FILE", self.bot_events_file),
            patch.object(server, "GITLAB_SEMANTIC_EVENTS_FILE", self.data_dir / "gitlab_semantic_events.json"),
            patch.object(server, "_sync_gitlab_issue_labels_from_work_item", lambda state: state),
        ]
        for current in self.patches:
            current.start()
            self.addCleanup(current.stop)

    def _state(
        self,
        *,
        owner: str,
        stage: str,
        artifact_state: str,
        handoff: WorkItemHandoff | None = None,
    ) -> WorkItemState:
        return WorkItemState(
            ref="veridataops/connector#35",
            project_id="connector",
            project_path="veridataops/connector",
            title="Connector gate",
            url="https://dev.veridataops.com/gitlab/veridataops/connector/-/work_items/35",
            kind="issue",
            priority="priority::P1",
            current_owner=owner,
            current_stage=stage,
            implementation_owner="james",
            validation_owner="quinn",
            release_owner="release manager",
            artifact_state=artifact_state,
            handoff=handoff,
            handoff_history=[],
            last_meaningful_update_at=100.0,
            last_gitlab_event_at=100.0,
            blocker=None,
            next_action="next",
            next_owner=None,
            release_gate=True,
            status_label="status::in progress",
            labels=[],
            mr_refs=["veridataops/connector!26"] if artifact_state == "merge_request" else [],
            notes=[],
            closed_at=None,
            updated_at=100.0,
            created_at=90.0,
        )

    def _save(self, state: WorkItemState) -> None:
        server._save_work_item_state(state)

    def _load(self) -> WorkItemState:
        return server._work_item_state("veridataops/connector#35")

    def test_structured_handoff_rejects_direct_implementation_to_release_for_branch_artifact(self) -> None:
        self._save(self._state(owner="james", stage="ready_for_validation", artifact_state="branch"))

        with self.assertRaises(HTTPException) as exc:
            server._structured_handoff(
                "veridataops/connector#35",
                WorkItemHandoffCreate(
                    from_agent="James",
                    to_agent="Release Manager",
                    current_owner="james",
                    current_stage="ready_for_validation",
                    artifact_state="branch",
                ),
            )

        self.assertEqual(exc.exception.status_code, 409)
        self.assertEqual(exc.exception.detail["code"], "branch_only_artifact")

    def test_structured_handoff_rejects_quinn_to_release_before_merge(self) -> None:
        self._save(self._state(owner="quinn", stage="validation_running", artifact_state="merge_request"))

        with self.assertRaises(HTTPException) as exc:
            server._structured_handoff(
                "veridataops/connector#35",
                WorkItemHandoffCreate(
                    from_agent="Quinn",
                    to_agent="Release Manager",
                    current_owner="quinn",
                    current_stage="validation_running",
                    artifact_state="merge_request",
                ),
            )

        self.assertEqual(exc.exception.status_code, 409)
        self.assertEqual(exc.exception.detail["code"], "missing_merge")

    def test_structured_handoff_allows_quinn_to_release_after_merged_main(self) -> None:
        self._save(self._state(owner="quinn", stage="validation_running", artifact_state="merged_main"))

        updated = server._structured_handoff(
            "veridataops/connector#35",
            WorkItemHandoffCreate(
                from_agent="Quinn",
                to_agent="Release Manager",
                current_owner="quinn",
                current_stage="validation_running",
                artifact_state="merged_main",
                expected_action="Validate the main pipeline.",
            ),
        )

        self.assertEqual(updated.handoff.to_agent, "release manager")
        self.assertEqual(updated.handoff.status, "pending")
        self.assertEqual(updated.current_owner, "quinn")
        self.assertEqual(updated.next_owner, "release manager")
        self.assertEqual(updated.artifact_state, "merged_main")

    def test_structured_handoff_allows_orchestrator_to_quinn_after_merged_main(self) -> None:
        self._save(self._state(owner="orchestrator", stage="ready_for_validation", artifact_state="merged_main"))

        updated = server._structured_handoff(
            "veridataops/connector#35",
            WorkItemHandoffCreate(
                from_agent="Orchestrator",
                to_agent="Quinn",
                current_owner="orchestrator",
                current_stage="ready_for_validation",
                artifact_state="merged_main",
                expected_action="Validate the merged-main artifact and close the reconciliation lane.",
            ),
        )

        self.assertEqual(updated.handoff.to_agent, "quinn")
        self.assertEqual(updated.handoff.status, "pending")
        self.assertEqual(updated.current_owner, "orchestrator")
        self.assertEqual(updated.next_owner, "quinn")
        self.assertEqual(updated.artifact_state, "merged_main")

    def test_structured_ack_rejects_release_accept_on_branch_artifact(self) -> None:
        self._save(
            self._state(
                owner="quinn",
                stage="validation_running",
                artifact_state="branch",
                handoff=WorkItemHandoff(
                    from_agent="quinn",
                    to_agent="release manager",
                    requested_at=100.0,
                    status="pending",
                    artifact_state="branch",
                    stage="validation_running",
                ),
            )
        )

        with self.assertRaises(HTTPException) as exc:
            server._structured_ack(
                "veridataops/connector#35",
                WorkItemAckCreate(
                    actor="Release Manager",
                    accepted=True,
                    artifact_state="branch",
                ),
            )

        self.assertEqual(exc.exception.status_code, 409)
        self.assertEqual(exc.exception.detail["code"], "wrong_lane")

    def test_new_handoff_archives_previous_direction_in_history(self) -> None:
        self._save(
            self._state(
                owner="james",
                stage="implementation_active",
                artifact_state="branch",
                handoff=WorkItemHandoff(
                    from_agent="quinn",
                    to_agent="james",
                    requested_at=90.0,
                    acknowledged_at=95.0,
                    status="accepted",
                    artifact_state="merge_request",
                    stage="failed_with_action_owner",
                ),
            )
        )

        updated = server._structured_handoff(
            "veridataops/connector#35",
            WorkItemHandoffCreate(
                from_agent="James",
                to_agent="Quinn",
                current_owner="james",
                current_stage="ready_for_validation",
                artifact_state="branch",
            ),
        )

        self.assertEqual(updated.handoff.from_agent, "james")
        self.assertEqual(updated.handoff.to_agent, "quinn")
        self.assertEqual(len(updated.handoff_history), 2)
        self.assertEqual(updated.handoff_history[0].from_agent, "quinn")
        self.assertEqual(updated.handoff_history[0].to_agent, "james")
        self.assertEqual(updated.handoff_history[1].status, "pending")

    def test_progress_archives_accepted_handoff_when_lane_returns_to_sender(self) -> None:
        self._save(
            self._state(
                owner="james",
                stage="failed_with_action_owner",
                artifact_state="branch",
                handoff=WorkItemHandoff(
                    from_agent="james",
                    to_agent="quinn",
                    requested_at=90.0,
                    acknowledged_at=95.0,
                    status="accepted",
                    artifact_state="branch",
                    stage="ready_for_validation",
                ),
            )
        )

        updated = server._structured_progress(
            "veridataops/connector#35",
            WorkItemProgressUpdate(
                actor="Orchestrator",
                current_owner="James",
                current_stage="failed_with_action_owner",
                next_action="Implement the blocker and hand back to Quinn.",
                next_owner="James",
                blocker="Validation found a defect.",
            ),
        )

        self.assertIsNone(updated.handoff)
        self.assertEqual(len(updated.handoff_history), 1)
        self.assertEqual(updated.handoff_history[0].status, "superseded")
        self.assertEqual(updated.current_owner, "james")
        self.assertEqual(updated.current_stage, "failed_with_action_owner")

    def test_progress_preserves_accepted_handoff_for_active_validation_lane(self) -> None:
        self._save(
            self._state(
                owner="quinn",
                stage="validation_running",
                artifact_state="branch",
                handoff=WorkItemHandoff(
                    from_agent="james",
                    to_agent="quinn",
                    requested_at=90.0,
                    acknowledged_at=95.0,
                    status="accepted",
                    artifact_state="branch",
                    stage="ready_for_validation",
                ),
            )
        )

        updated = server._structured_progress(
            "veridataops/connector#35",
            WorkItemProgressUpdate(
                actor="Orchestrator",
                current_owner="Quinn",
                current_stage="validation_running",
                next_action="Validate the artifact and merge or hand back one exact defect.",
                next_owner="Quinn",
                blocker="",
            ),
        )

        self.assertIsNotNone(updated.handoff)
        self.assertEqual(updated.handoff.status, "accepted")
        self.assertEqual(updated.current_owner, "quinn")
        self.assertEqual(updated.current_stage, "validation_running")

    def test_progress_close_clears_owner_next_owner_and_handoff(self) -> None:
        self._save(
            self._state(
                owner="quinn",
                stage="ready_to_close",
                artifact_state="merge_request",
                handoff=WorkItemHandoff(
                    from_agent="james",
                    to_agent="quinn",
                    requested_at=90.0,
                    acknowledged_at=95.0,
                    status="accepted",
                    artifact_state="merge_request",
                    stage="validation_running",
                ),
            )
        )

        updated = server._structured_progress(
            "veridataops/connector#35",
            WorkItemProgressUpdate(
                actor="Orchestrator",
                current_owner="Quinn",
                current_stage="closed",
                next_action="Closed after validation.",
                next_owner="Quinn",
            ),
        )

        self.assertEqual(updated.current_stage, "closed")
        self.assertIsNone(updated.current_owner)
        self.assertIsNone(updated.next_owner)
        self.assertIsNone(updated.handoff)
        self.assertIsNone(updated.status_label)
