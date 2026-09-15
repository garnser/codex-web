from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import server
from codex_web.models import BotBinding, WorkItemAckCreate, WorkItemHandoff, WorkItemHandoffCreate, WorkItemProgressUpdate, WorkItemState


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

    def test_structured_handoff_preserves_supporting_blocking_findings(self) -> None:
        self._save(self._state(owner="release manager", stage="failed_with_action_owner", artifact_state="merged_main"))

        updated = server._structured_handoff(
            "veridataops/connector#35",
            WorkItemHandoffCreate(
                from_agent="Release Manager",
                to_agent="Orchestrator",
                current_owner="release manager",
                current_stage="failed_with_action_owner",
                blocker="Release parent is blocked by unresolved child defects.",
                blocking_findings=[
                    "NetBox destination delivery is not proven on the shipped artifact.",
                    "Tenant job catalog on the shipped artifact exceeds the release latency budget.",
                ],
                next_action="Explode the findings into child implementation tickets and keep the parent release lane blocked on those children.",
                artifact_state="merged_main",
            ),
        )

        self.assertEqual(
            updated.blocking_findings,
            [
                "NetBox destination delivery is not proven on the shipped artifact.",
                "Tenant job catalog on the shipped artifact exceeds the release latency budget.",
            ],
        )

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

    def test_accepted_implementation_handoff_uses_short_owner_idle_sla(self) -> None:
        state = self._state(
            owner="james",
            stage="failed_with_action_owner",
            artifact_state="branch",
            handoff=WorkItemHandoff(
                from_agent="release manager",
                to_agent="james",
                requested_at=90.0,
                acknowledged_at=95.0,
                status="accepted",
                artifact_state="branch",
                stage="failed_with_action_owner",
            ),
        )

        self.assertEqual(
            server._work_item_sla_threshold_seconds(state),
            min(server._work_item_progress_sla_seconds(), server._accepted_handoff_owner_idle_seconds()),
        )

    def test_split_brain_findings_include_accepted_handoff_owner_and_status_drift(self) -> None:
        state = self._state(
            owner="release manager",
            stage="validation_running",
            artifact_state="merged_main",
            handoff=WorkItemHandoff(
                from_agent="quinn",
                to_agent="release manager",
                requested_at=90.0,
                acknowledged_at=95.0,
                status="accepted",
                artifact_state="merged_main",
                stage="validation_running",
            ),
        )
        state.current_owner = "james"
        state.next_owner = "quinn"
        state.status_label = "status::awaiting confirmation"

        findings = server._work_item_split_brain_findings(state)

        self.assertIn("accepted handoff still marked as awaiting confirmation", findings)
        self.assertIn(
            "accepted handoff owner drift: expected=release manager stored=james",
            findings,
        )
        self.assertIn(
            "accepted handoff next-owner drift: expected=release manager stored=quinn",
            findings,
        )

    def test_split_brain_findings_include_next_action_owner_cue_drift(self) -> None:
        state = self._state(
            owner="james",
            stage="implementation_active",
            artifact_state="merge_request",
        )
        state.next_owner = None
        state.next_action = (
            "Quinn to validate mergeable MR !247 at exact head 7118a7e11365ba73efb576ad5f78ae3627e4c29d "
            "and either merge it after MR pipeline 3268 reaches a clean terminal state or hand back one exact failing job."
        )

        findings = server._work_item_split_brain_findings(state)

        self.assertIn(
            "next-action owner cue drift: action=quinn current=james next=none",
            findings,
        )

    def test_progress_preserves_accepted_handoff_recipient_against_stale_sender_progress(self) -> None:
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
                owner="release manager",
                stage="validation_running",
                artifact_state="merged_main",
                handoff=handoff,
            )
        )
        state = self._load()
        state.status_label = "status::in progress"
        state.next_owner = "release manager"
        self._save(state)

        server._structured_progress(
            "veridataops/connector#35",
            WorkItemProgressUpdate(
                actor="quinn",
                current_owner="quinn",
                current_stage="validation_running",
                next_owner="quinn",
                next_action="stale sender-side progress",
            ),
        )

        updated = self._load()
        self.assertEqual(updated.current_owner, "release manager")
        self.assertEqual(updated.current_stage, "validation_running")
        self.assertEqual(updated.next_owner, "release manager")
        self.assertEqual(updated.handoff.status if updated.handoff else None, "accepted")

    def test_progress_records_supporting_blocking_findings(self) -> None:
        self._save(self._state(owner="release manager", stage="failed_with_action_owner", artifact_state="merged_main"))

        server._structured_progress(
            "veridataops/connector#35",
            WorkItemProgressUpdate(
                actor="release manager",
                current_owner="release manager",
                current_stage="failed_with_action_owner",
                blocker="Release parent is blocked by unresolved child defects.",
                blocking_findings=[
                    "Shipped ASD gate is timing out at /api/jobs for tenant helpmesee-inc.",
                    "Current release proof does not include destination delivery into NetBox.",
                    "Shipped ASD gate is timing out at /api/jobs for tenant helpmesee-inc.",
                ],
                next_action="Hand exact child defects to implementation owners and keep the release parent blocked on those child fixes.",
            ),
        )

        updated = self._load()
        self.assertEqual(
            updated.blocking_findings,
            [
                "Shipped ASD gate is timing out at /api/jobs for tenant helpmesee-inc.",
                "Current release proof does not include destination delivery into NetBox.",
            ],
        )

    def test_implementation_progress_clears_blocking_findings(self) -> None:
        state = self._state(owner="james", stage="failed_with_action_owner", artifact_state="branch")
        state.blocker = "Parent lane is blocked."
        state.blocking_findings = ["child defect A", "child defect B"]
        self._save(state)

        server._structured_progress(
            "veridataops/connector#35",
            WorkItemProgressUpdate(
                actor="james",
                current_owner="james",
                current_stage="implementation_active",
                next_action="Implement the exact child fix now.",
            ),
        )

        updated = self._load()
        self.assertEqual(updated.current_stage, "implementation_active")
        self.assertEqual(updated.blocking_findings, [])

    def test_actionable_owner_dispatch_wakes_responsible_thread_without_waiting_for_recent_activity_grace(self) -> None:
        state = self._state(
            owner="james",
            stage="failed_with_action_owner",
            artifact_state="branch",
        )
        binding = BotBinding(
            id="binding-1",
            provider="slack",
            external_conversation_id="C0B9M89AHCY",
            thread_id="thread-james",
            project_id="connector",
            thread_name="James",
            created_at=1,
            updated_at=1,
        )
        with (
            patch.object(server, "_binding_for_agent", return_value=binding),
            patch.object(server, "_thread_is_active", return_value=False),
            patch.object(server, "_thread_queue_depth", return_value=0),
            patch.object(server, "_replace_nonperforming_thread_if_needed", new=AsyncMock(return_value=binding)),
            patch.object(server, "_watchdog_dispatch_allowed", return_value=True),
            patch.object(server, "_record_watchdog_dispatch") as record_dispatch,
            patch.object(server, "_dispatch_event_to_binding", new=AsyncMock(return_value={"ok": True})) as dispatch,
            patch.object(server, "_append_bot_event") as append_event,
        ):
            asyncio.run(
                server._dispatch_actionable_owner_to_responsible_thread(
                    state,
                    source="work-item-progress",
                    actor="Orchestrator",
                )
            )

        record_dispatch.assert_called_once()
        dispatch.assert_awaited_once_with(binding, server._work_item_dispatch_text(state), "work-item-progress")
        self.assertEqual(append_event.call_args.args[0]["type"], "work_item_owner_progress_dispatched")

    def test_actionable_owner_dispatch_skips_when_actor_is_current_owner(self) -> None:
        state = self._state(
            owner="james",
            stage="implementation_active",
            artifact_state="branch",
        )
        with (
            patch.object(server, "_binding_for_agent") as binding_for_agent,
            patch.object(server, "_dispatch_event_to_binding", new=AsyncMock()) as dispatch,
        ):
            asyncio.run(
                server._dispatch_actionable_owner_to_responsible_thread(
                    state,
                    source="work-item-progress",
                    actor="James",
                )
            )

        binding_for_agent.assert_not_called()
        dispatch.assert_not_awaited()

    def test_actionable_owner_continuity_check_rearms_inactive_owner_thread(self) -> None:
        state = self._state(
            owner="release manager",
            stage="validation_running",
            artifact_state="merged_main",
        )
        state.ref = "veridataops/saas-app#271"
        state.project_id = "saas-app"
        state.project_path = "veridataops/saas-app"
        state.next_owner = "release manager"
        self._save(state)
        binding = BotBinding(
            id="binding-release",
            provider="slack",
            external_conversation_id="C0B9M89AHCY",
            thread_id="thread-release",
            project_id="saas-app",
            thread_name="Release Manager",
            created_at=1,
            updated_at=1,
        )
        with (
            patch.object(server, "_actionable_owner_continuity_delay_seconds", return_value=0),
            patch.object(server, "_binding_for_agent", return_value=binding),
            patch.object(server, "_thread_is_active", return_value=False),
            patch.object(server, "_thread_queue_depth", return_value=0),
            patch.object(server, "_replace_nonperforming_thread_if_needed", new=AsyncMock(return_value=binding)),
            patch.object(server, "_watchdog_dispatch_allowed", return_value=True),
            patch.object(server, "_record_watchdog_dispatch") as record_dispatch,
            patch.object(server, "_dispatch_event_to_binding", new=AsyncMock(return_value={"ok": True})) as dispatch,
            patch.object(server, "_append_bot_event") as append_event,
        ):
            asyncio.run(
                server._run_actionable_owner_continuity_check(
                    "veridataops/saas-app#271",
                    expected_owner="release manager",
                    expected_stage="validation_running",
                    source="work-item-progress-continuity",
                )
            )

        record_dispatch.assert_called_once()
        updated = server._work_item_state("veridataops/saas-app#271")
        dispatch.assert_awaited_once_with(binding, server._work_item_dispatch_text(updated), "work-item-progress-continuity")
        self.assertEqual(append_event.call_args.args[0]["type"], "work_item_owner_progress_dispatched")

    def test_progress_endpoint_schedules_actionable_owner_continuity_check(self) -> None:
        state = self._state(
            owner="release manager",
            stage="validation_running",
            artifact_state="merged_main",
        )
        state.ref = "veridataops/saas-app#271"
        state.project_id = "saas-app"
        state.project_path = "veridataops/saas-app"
        self._save(state)

        with (
            patch.object(server.hub, "publish", new=AsyncMock()),
            patch.object(server, "_schedule_actionable_owner_dispatch") as dispatch_schedule,
            patch.object(server, "_schedule_actionable_owner_continuity_check") as continuity_schedule,
        ):
            asyncio.run(
                server.update_work_item_progress(
                    "veridataops/saas-app#271",
                    WorkItemProgressUpdate(
                        actor="release manager",
                        current_owner="release manager",
                        current_stage="validation_running",
                        next_owner="release manager",
                        next_action="Keep the release lane moving.",
                    ),
                )
            )

        dispatch_schedule.assert_called_once()
        continuity_schedule.assert_called_once()

    def test_handoff_continuity_check_re_dispatches_pending_handoff_to_inactive_recipient(self) -> None:
        state = self._state(
            owner="james",
            stage="ready_for_validation",
            artifact_state="merge_request",
            handoff=WorkItemHandoff(
                from_agent="james",
                to_agent="quinn",
                requested_at=123.0,
                status="pending",
                artifact_state="merge_request",
                stage="ready_for_validation",
            ),
        )
        state.ref = "veridataops/saas-app#271"
        state.project_id = "saas-app"
        state.project_path = "veridataops/saas-app"
        state.next_owner = "quinn"
        self._save(state)
        binding = BotBinding(
            id="binding-quinn",
            provider="slack",
            external_conversation_id="C0B9M89AHCY",
            thread_id="thread-quinn",
            project_id="saas-app",
            thread_name="Quinn",
            created_at=1,
            updated_at=1,
        )
        with (
            patch.object(server, "_handoff_continuity_delay_seconds", return_value=0),
            patch.object(server, "_binding_for_agent", return_value=binding),
            patch.object(server, "_thread_is_active", return_value=False),
            patch.object(server, "_thread_queue_depth", return_value=0),
            patch.object(server, "_replace_nonperforming_thread_if_needed", new=AsyncMock(return_value=binding)),
            patch.object(server, "_watchdog_dispatch_allowed", return_value=True),
            patch.object(server, "_record_watchdog_dispatch") as record_dispatch,
            patch.object(server, "_dispatch_event_to_binding", new=AsyncMock(return_value={"ok": True})) as dispatch,
            patch.object(server, "_append_bot_event") as append_event,
        ):
            asyncio.run(
                server._run_handoff_continuity_check(
                    "veridataops/saas-app#271",
                    expected_recipient="quinn",
                    expected_requested_at=123.0,
                    source="work-item-handoff-continuity",
                )
            )

        record_dispatch.assert_called_once()
        updated = server._work_item_state("veridataops/saas-app#271")
        dispatch.assert_awaited_once_with(binding, server._work_item_dispatch_text(updated), "work-item-handoff-continuity")
        self.assertEqual(append_event.call_args.args[0]["type"], "work_item_handoff_continuity_dispatched")
