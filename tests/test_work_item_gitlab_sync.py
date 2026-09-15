from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from codex_web.models import WorkItemHandoff, WorkItemProgressUpdate, WorkItemState


def _iso(second: int) -> str:
    return f"1970-01-01T00:00:{second:02d}+00:00"


class WorkItemGitLabSyncTests(unittest.TestCase):
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
        ]
        for current in self.patches:
            current.start()
            self.addCleanup(current.stop)

    def _state(
        self,
        *,
        stage: str,
        owner: str | None,
        meaningful_at: float,
        gitlab_at: float | None,
        handoff: WorkItemHandoff | None = None,
        status_label: str | None = None,
        next_owner: str | None = None,
        blocker: str | None = None,
        closed_at: float | None = None,
    ) -> WorkItemState:
        return WorkItemState(
            ref="veridataops/platform#134",
            project_id="platform",
            project_path="veridataops/platform",
            title="Permanent fix",
            url="https://dev.veridataops.com/gitlab/veridataops/platform/-/work_items/134",
            kind="issue",
            priority="priority::P2",
            current_owner=owner,
            current_stage=stage,
            handoff=handoff,
            last_meaningful_update_at=meaningful_at,
            last_gitlab_event_at=gitlab_at,
            blocker=blocker,
            next_action="next",
            next_owner=next_owner,
            release_gate=False,
            status_label=status_label,
            labels=[],
            mr_refs=[],
            notes=[],
            closed_at=closed_at,
            updated_at=meaningful_at,
            created_at=100.0,
        )

    def _save(self, state: WorkItemState) -> None:
        server._save_work_item_state(state)

    def _load(self) -> WorkItemState:
        return server._work_item_state("veridataops/platform#134")

    def _event_payload(self, *, state: str, updated_at: str, labels: list[str]) -> dict[str, object]:
        return {
            "object_kind": "issue",
            "project": {"path_with_namespace": "veridataops/platform"},
            "object_attributes": {
                "iid": 134,
                "title": "Permanent fix",
                "state": state,
                "updated_at": updated_at,
                "labels": labels,
            },
        }

    def _pipeline_payload(self, *, iid: int, status: str, updated_at: str, ref: str = "main") -> dict[str, object]:
        return {
            "object_kind": "pipeline",
            "project": {"path_with_namespace": "veridataops/platform"},
            "object_attributes": {
                "iid": iid,
                "ref": ref,
                "status": status,
                "updated_at": updated_at,
                "web_url": f"https://dev.veridataops.com/gitlab/veridataops/platform/-/pipelines/{iid}",
            },
        }

    def _issue_payload(self, *, state: str, updated_at: str, labels: list[str]) -> dict[str, object]:
        return {
            "references": {"full": "veridataops/platform#134"},
            "title": "Permanent fix",
            "state": state,
            "updated_at": updated_at,
            "labels": labels,
            "web_url": "https://dev.veridataops.com/gitlab/veridataops/platform/-/work_items/134",
        }

    def test_event_projection_preserves_validation_running_after_accepted_handoff(self) -> None:
        handoff = WorkItemHandoff(
            from_agent="carl",
            to_agent="release manager",
            requested_at=180.0,
            acknowledged_at=190.0,
            status="accepted",
        )
        self._save(
            self._state(
                stage="validation_running",
                owner="release manager",
                meaningful_at=20.0,
                gitlab_at=10.0,
                handoff=handoff,
                status_label="status::in progress",
                next_owner="release manager",
            )
        )

        server._upsert_work_item_state_from_gitlab_event(
            self._event_payload(
                state="opened",
                updated_at=_iso(30),
                labels=["owner::release manager", "status::in progress"],
            ),
            project_id="platform",
        )

        updated = self._load()
        self.assertEqual(updated.current_stage, "validation_running")
        self.assertEqual(updated.current_owner, "release manager")
        self.assertEqual(updated.handoff.status if updated.handoff else None, "accepted")

    def test_issue_projection_preserves_pending_validation_handoff(self) -> None:
        handoff = WorkItemHandoff(
            from_agent="carl",
            to_agent="quinn",
            requested_at=180.0,
            status="pending",
        )
        self._save(
            self._state(
                stage="ready_for_validation",
                owner="carl",
                meaningful_at=200.0,
                gitlab_at=190.0,
                handoff=handoff,
                status_label="status::awaiting confirmation",
                next_owner="quinn",
            )
        )

        server._upsert_work_item_state_from_gitlab_issue(
            self._issue_payload(
                state="opened",
                updated_at=_iso(31),
                labels=["owner::carl", "status::awaiting confirmation"],
            ),
            project_id="platform",
        )

        updated = self._load()
        self.assertEqual(updated.current_stage, "ready_for_validation")
        self.assertEqual(updated.current_owner, "carl")
        self.assertEqual(updated.next_owner, "quinn")
        self.assertEqual(updated.handoff.status if updated.handoff else None, "pending")

    def test_issue_projection_does_not_infer_handoff_from_gitlab_labels_alone(self) -> None:
        self._save(
            self._state(
                stage="implementation_active",
                owner="orchestrator",
                meaningful_at=200.0,
                gitlab_at=190.0,
                handoff=None,
                status_label="status::in progress",
                next_owner="release manager",
            )
        )

        server._upsert_work_item_state_from_gitlab_issue(
            self._issue_payload(
                state="opened",
                updated_at=_iso(34),
                labels=["owner::orchestrator", "status::awaiting confirmation"],
            ),
            project_id="platform",
        )

        updated = self._load()
        self.assertIsNone(updated.handoff)
        self.assertEqual(updated.current_owner, "orchestrator")
        self.assertEqual(updated.status_label, "status::in progress")
        self.assertEqual(updated.current_stage, "implementation_active")

    def test_event_projection_does_not_infer_handoff_from_gitlab_labels_alone(self) -> None:
        self._save(
            self._state(
                stage="implementation_active",
                owner="orchestrator",
                meaningful_at=200.0,
                gitlab_at=190.0,
                handoff=None,
                status_label="status::in progress",
                next_owner="release manager",
            )
        )

        server._upsert_work_item_state_from_gitlab_event(
            self._event_payload(
                state="opened",
                updated_at=_iso(35),
                labels=["owner::orchestrator", "status::awaiting confirmation"],
            ),
            project_id="platform",
        )

        updated = self._load()
        self.assertIsNone(updated.handoff)
        self.assertEqual(updated.current_owner, "orchestrator")
        self.assertEqual(updated.status_label, "status::in progress")
        self.assertEqual(updated.current_stage, "implementation_active")

    def test_close_projection_clears_handoff_and_blocker(self) -> None:
        handoff = WorkItemHandoff(
            from_agent="carl",
            to_agent="release manager",
            requested_at=180.0,
            acknowledged_at=190.0,
            status="accepted",
        )
        self._save(
            self._state(
                stage="ready_to_close",
                owner="release manager",
                meaningful_at=20.0,
                gitlab_at=19.0,
                handoff=handoff,
                status_label="status::in progress",
                next_owner="release manager",
                blocker="done pending close",
            )
        )

        server._upsert_work_item_state_from_gitlab_event(
            self._event_payload(
                state="closed",
                updated_at=_iso(32),
                labels=["owner::release manager", "status::in progress"],
            ),
            project_id="platform",
        )

        updated = self._load()
        self.assertEqual(updated.current_stage, "closed")
        self.assertIsNone(updated.handoff)
        self.assertIsNone(updated.blocker)
        self.assertIsNone(updated.status_label)
        self.assertIsNotNone(updated.closed_at)

    def test_close_projection_forces_gitlab_label_cleanup_when_closed_event_keeps_routing_labels(self) -> None:
        self._save(
            self._state(
                stage="ready_to_close",
                owner="release manager",
                meaningful_at=20.0,
                gitlab_at=19.0,
                status_label="status::in progress",
                next_owner="release manager",
            )
        )

        calls: list[tuple[str, list[str]]] = []

        def _capture_sync(state: WorkItemState) -> WorkItemState:
            calls.append((state.current_stage, list(state.labels)))
            state.labels = [label for label in state.labels if not label.startswith(("owner::", "status::"))]
            state.status_label = None
            return state

        with patch.object(server, "_sync_gitlab_issue_labels_from_work_item", side_effect=_capture_sync):
            server._upsert_work_item_state_from_gitlab_event(
                self._event_payload(
                    state="closed",
                    updated_at=_iso(33),
                    labels=["owner::orchestrator", "status::in progress", "priority::P1"],
                ),
                project_id="platform",
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "closed")
        self.assertIn("owner::orchestrator", calls[0][1])

    def test_reopen_projection_restores_non_closed_stage(self) -> None:
        self._save(
            self._state(
                stage="closed",
                owner="release manager",
                meaningful_at=20.0,
                gitlab_at=20.0,
                status_label=None,
                next_owner="release manager",
                closed_at=20.0,
            )
        )

        server._upsert_work_item_state_from_gitlab_event(
            self._event_payload(
                state="opened",
                updated_at=_iso(40),
                labels=["owner::james", "status::blocked"],
            ),
            project_id="platform",
        )

        updated = self._load()
        self.assertEqual(updated.current_stage, "failed_with_action_owner")
        self.assertEqual(updated.current_owner, "james")
        self.assertEqual(updated.status_label, "status::blocked")
        self.assertIsNone(updated.closed_at)

    def test_stale_conflicting_gitlab_event_is_ignored(self) -> None:
        handoff = WorkItemHandoff(
            from_agent="carl",
            to_agent="release manager",
            requested_at=180.0,
            acknowledged_at=190.0,
            status="accepted",
        )
        self._save(
            self._state(
                stage="validation_running",
                owner="release manager",
                meaningful_at=250.0,
                gitlab_at=240.0,
                handoff=handoff,
                status_label="status::in progress",
                next_owner="release manager",
            )
        )

        server._upsert_work_item_state_from_gitlab_event(
            self._event_payload(
                state="opened",
                updated_at=_iso(35),
                labels=["owner::carl", "status::awaiting confirmation"],
            ),
            project_id="platform",
        )

        updated = self._load()
        self.assertEqual(updated.current_stage, "validation_running")
        self.assertEqual(updated.current_owner, "release manager")
        self.assertEqual(updated.handoff.status if updated.handoff else None, "accepted")

        events = [json.loads(line) for line in self.events_file.read_text().splitlines() if line.strip()]
        self.assertIn("gitlab_event_stale_ignored", [event["event_type"] for event in events])

    def test_gitlab_event_ignores_sender_projection_after_accepted_release_handoff(self) -> None:
        handoff = WorkItemHandoff(
            from_agent="quinn",
            to_agent="release manager",
            requested_at=180.0,
            acknowledged_at=190.0,
            status="accepted",
            artifact_state="merged_main",
            stage="validation_running",
        )
        self._save(
            self._state(
                stage="validation_running",
                owner="release manager",
                meaningful_at=20.0,
                gitlab_at=10.0,
                handoff=handoff,
                status_label="status::in progress",
                next_owner="release manager",
            )
        )

        server._upsert_work_item_state_from_gitlab_event(
            self._event_payload(
                state="opened",
                updated_at=_iso(35),
                labels=["owner::quinn", "status::awaiting confirmation"],
            ),
            project_id="platform",
        )

        updated = self._load()
        self.assertEqual(updated.current_stage, "validation_running")
        self.assertEqual(updated.current_owner, "release manager")
        self.assertEqual(updated.status_label, "status::in progress")
        self.assertEqual(updated.handoff.status if updated.handoff else None, "accepted")

    def test_blocked_event_reconciles_actionable_owner_and_clears_pending_handoff(self) -> None:
        handoff = WorkItemHandoff(
            from_agent="carl",
            to_agent="release manager",
            requested_at=180.0,
            status="pending",
        )
        self._save(
            self._state(
                stage="ready_for_validation",
                owner="carl",
                meaningful_at=20.0,
                gitlab_at=19.0,
                handoff=handoff,
                status_label="status::awaiting confirmation",
                next_owner="release manager",
            )
        )

        server._upsert_work_item_state_from_gitlab_event(
            self._event_payload(
                state="opened",
                updated_at=_iso(50),
                labels=["owner::james", "status::blocked"],
            ),
            project_id="platform",
        )

        updated = self._load()
        self.assertEqual(updated.current_stage, "failed_with_action_owner")
        self.assertEqual(updated.current_owner, "james")
        self.assertEqual(updated.next_owner, "james")
        self.assertEqual(updated.status_label, "status::blocked")

    def test_pipeline_event_projects_as_closed_evidence_item(self) -> None:
        state = server._upsert_work_item_state_from_gitlab_event(
            self._pipeline_payload(iid=220, status="success", updated_at=_iso(61)),
            project_id="platform",
        )

        self.assertIsNotNone(state)
        self.assertEqual(state.ref, "veridataops/platform#220")
        self.assertEqual(state.kind, "pipeline")
        self.assertEqual(state.current_stage, "closed")
        self.assertIsNone(state.current_owner)
        self.assertIsNone(state.next_owner)
        self.assertIsNone(state.status_label)
        self.assertIsNotNone(state.closed_at)

    def test_pipeline_event_closes_existing_open_pipeline_state(self) -> None:
        pipeline_state = WorkItemState(
            ref="veridataops/platform#220",
            project_id="platform",
            project_path="veridataops/platform",
            title=None,
            url="https://dev.veridataops.com/gitlab/veridataops/platform/-/pipelines/220",
            kind="pipeline",
            priority=None,
            current_owner="quinn",
            current_stage="implementation_active",
            handoff=None,
            handoff_history=[],
            last_meaningful_update_at=20.0,
            last_gitlab_event_at=19.0,
            blocker=None,
            next_action=None,
            next_owner="quinn",
            release_gate=False,
            status_label=None,
            labels=[],
            mr_refs=[],
            notes=[],
            closed_at=None,
            updated_at=20.0,
            created_at=10.0,
        )
        server._save_work_item_state(pipeline_state)

        server._upsert_work_item_state_from_gitlab_event(
            self._pipeline_payload(iid=220, status="success", updated_at=_iso(62)),
            project_id="platform",
        )

        updated = server._work_item_state("veridataops/platform#220")
        self.assertEqual(updated.current_stage, "closed")
        self.assertIsNone(updated.current_owner)
        self.assertIsNone(updated.next_owner)
        self.assertIsNone(updated.status_label)
        self.assertIsNotNone(updated.closed_at)
        self.assertIsNone(updated.handoff)
        self.assertIsNone(updated.blocker)

    def test_event_target_agents_prefers_canonical_handoff_recipient(self) -> None:
        state = self._state(
            stage="ready_for_validation",
            owner="carl",
            meaningful_at=200.0,
            gitlab_at=190.0,
            handoff=WorkItemHandoff(
                from_agent="carl",
                to_agent="quinn",
                requested_at=180.0,
                status="pending",
            ),
            status_label="status::awaiting confirmation",
            next_owner="quinn",
        )
        state.labels = ["owner::carl", "status::awaiting confirmation"]

        agents = server._gitlab_event_target_agents(
            self._event_payload(
                state="opened",
                updated_at=_iso(251),
                labels=["owner::carl", "status::awaiting confirmation"],
            ),
            server.GitLabProjectRoutingSettings(),
            state,
        )

        self.assertEqual(agents, ["quinn"])

    def test_event_target_agents_routes_split_brain_to_orchestrator(self) -> None:
        state = self._state(
            stage="ready_for_validation",
            owner="carl",
            meaningful_at=200.0,
            gitlab_at=190.0,
            handoff=None,
            status_label="status::awaiting confirmation",
            next_owner="quinn",
        )
        state.labels = ["owner::carl", "status::awaiting confirmation"]

        agents = server._gitlab_event_target_agents(
            self._event_payload(
                state="opened",
                updated_at=_iso(252),
                labels=["owner::carl", "status::awaiting confirmation"],
            ),
            server.GitLabProjectRoutingSettings(),
            state,
        )

        self.assertEqual(agents, ["orchestrator"])

    def test_progress_explicit_null_clears_next_owner_and_blocker_on_active_lane(self) -> None:
        self._save(
            self._state(
                stage="implementation_active",
                owner="james",
                meaningful_at=20.0,
                gitlab_at=19.0,
                status_label="status::in progress",
                next_owner="orchestrator",
                blocker="stale blocker",
            )
        )

        server._structured_progress(
            "veridataops/platform#134",
            WorkItemProgressUpdate(
                actor="james",
                current_owner="james",
                current_stage="implementation_active",
                next_action="keep building",
                next_owner=None,
                blocker=None,
            ),
        )

        updated = self._load()
        self.assertIsNone(updated.next_owner)
        self.assertIsNone(updated.blocker)

    def test_progress_preserves_pending_handoff_while_sender_still_owns_pre_ack_lane(self) -> None:
        handoff = WorkItemHandoff(
            from_agent="james",
            to_agent="quinn",
            requested_at=180.0,
            status="pending",
        )
        self._save(
            self._state(
                stage="ready_for_validation",
                owner="james",
                meaningful_at=20.0,
                gitlab_at=19.0,
                handoff=handoff,
                status_label="status::awaiting confirmation",
                next_owner="quinn",
            )
        )

        server._structured_progress(
            "veridataops/platform#134",
            WorkItemProgressUpdate(
                actor="james",
                current_owner="james",
                current_stage="ready_for_validation",
                next_action="fresh validation evidence recorded",
                note="sender progress update while waiting for ack",
            ),
        )

        updated = self._load()
        self.assertEqual(updated.current_owner, "james")
        self.assertEqual(updated.current_stage, "ready_for_validation")
        self.assertEqual(updated.status_label, "status::awaiting confirmation")
        self.assertEqual(updated.next_owner, "quinn")
        self.assertEqual(updated.handoff.status if updated.handoff else None, "pending")

    def test_progress_clears_pending_handoff_on_owner_transfer_outside_sender_recipient_pair(self) -> None:
        handoff = WorkItemHandoff(
            from_agent="james",
            to_agent="quinn",
            requested_at=180.0,
            status="pending",
        )
        self._save(
            self._state(
                stage="ready_for_validation",
                owner="james",
                meaningful_at=20.0,
                gitlab_at=19.0,
                handoff=handoff,
                status_label="status::awaiting confirmation",
                next_owner="quinn",
            )
        )

        server._structured_progress(
            "veridataops/platform#134",
            WorkItemProgressUpdate(
                actor="orchestrator",
                current_owner="sally",
                current_stage="ready_for_validation",
                next_action="lane moved elsewhere",
            ),
        )

        updated = self._load()
        self.assertEqual(updated.current_owner, "sally")
        self.assertIsNone(updated.handoff)
        self.assertEqual(updated.status_label, "status::in progress")

    def test_gitlab_projection_clears_stale_next_owner_and_blocker_on_active_lane(self) -> None:
        self._save(
            self._state(
                stage="failed_with_action_owner",
                owner="carl",
                meaningful_at=20.0,
                gitlab_at=19.0,
                status_label="status::blocked",
                next_owner="orchestrator",
                blocker="stale blocker",
            )
        )

        server._upsert_work_item_state_from_gitlab_issue(
            self._issue_payload(
                state="opened",
                updated_at=_iso(60),
                labels=["owner::james", "status::in progress"],
            ),
            project_id="platform",
        )

        updated = self._load()
        self.assertEqual(updated.current_stage, "implementation_active")
        self.assertEqual(updated.current_owner, "james")
        self.assertIsNone(updated.next_owner)
        self.assertIsNone(updated.blocker)


if __name__ == "__main__":
    unittest.main()
