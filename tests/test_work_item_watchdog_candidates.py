from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.models import WorkItemHandoff, WorkItemState
from codex_web.services.work_item_watchdog_candidates import (
    WorkItemWatchdogCandidatePolicy,
    install_work_item_watchdog_candidate_policy,
)


class WorkItemWatchdogCandidatePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.states: dict[str, WorkItemState] = {}
        self.split_findings: dict[str, list[str]] = {}
        self.host = SimpleNamespace(
            _load_work_item_states=lambda: self.states,
            _work_item_split_brain_findings=lambda state: self.split_findings.get(state.ref, []),
            _work_item_handoff_timeout_seconds=lambda: 900.0,
            _coerce_owner=lambda value: (str(value).strip().lower() or None) if value else None,
            _work_item_sla_threshold_seconds=lambda state: 100.0,
        )
        self.policy = WorkItemWatchdogCandidatePolicy(self.host)

    @staticmethod
    def state(
        ref: str,
        *,
        project_id: str = "project-1",
        owner: str | None = "james",
        stage: str = "implementation_active",
        updated_at: float = 900.0,
        last_meaningful_update_at: float = 900.0,
        release_gate: bool = False,
        handoff: WorkItemHandoff | None = None,
        closed_at: float | None = None,
    ) -> WorkItemState:
        return WorkItemState(
            ref=ref,
            project_id=project_id,
            current_owner=owner,
            current_stage=stage,
            next_owner=None,
            handoff=handoff,
            last_meaningful_update_at=last_meaningful_update_at,
            release_gate=release_gate,
            closed_at=closed_at,
            updated_at=updated_at,
            created_at=800.0,
        )

    def test_orchestrator_reason_ignores_closed_items(self) -> None:
        state = self.state("group/project#1", stage="closed", closed_at=950.0)
        self.assertIsNone(self.policy.orchestrator_reason(state, now=1000.0))

    def test_orchestrator_reason_prioritizes_split_brain(self) -> None:
        state = self.state("group/project#1", owner=None, updated_at=925.0)
        self.split_findings[state.ref] = ["owner mismatch"]
        self.assertEqual(
            self.policy.orchestrator_reason(state, now=1000.0),
            ("split_brain", 75.0),
        )

    def test_orchestrator_reason_detects_old_pending_handoff(self) -> None:
        handoff = WorkItemHandoff(
            from_agent="james",
            to_agent="quinn",
            requested_at=699.0,
            status="pending",
        )
        state = self.state("group/project#1", handoff=handoff)
        self.assertEqual(
            self.policy.orchestrator_reason(state, now=1000.0),
            ("pending_handoff", 301.0),
        )

        handoff.requested_at = 701.0
        self.assertNotEqual(
            self.policy.orchestrator_reason(state, now=1000.0),
            ("pending_handoff", 299.0),
        )

    def test_orchestrator_reason_classifies_unowned_blocked_and_stale_lanes(self) -> None:
        unowned = self.state("group/project#1", owner=None, updated_at=950.0)
        blocked = self.state(
            "group/project#2",
            stage="failed_with_action_owner",
            last_meaningful_update_at=850.0,
        )
        stale = self.state("group/project#3", last_meaningful_update_at=899.0)
        active = self.state("group/project#4", last_meaningful_update_at=950.0)

        self.assertEqual(self.policy.orchestrator_reason(unowned, now=1000.0), ("unowned", 50.0))
        self.assertEqual(self.policy.orchestrator_reason(blocked, now=1000.0), ("blocked_lane", 150.0))
        self.assertEqual(self.policy.orchestrator_reason(stale, now=1000.0), ("stale_lane", 101.0))
        self.assertIsNone(self.policy.orchestrator_reason(active, now=1000.0))

    def test_orchestrator_candidates_filter_project_and_preserve_legacy_ordering(self) -> None:
        release_stale = self.state(
            "group/project#3",
            release_gate=True,
            last_meaningful_update_at=700.0,
        )
        unowned = self.state("group/project#2", owner=None, updated_at=950.0)
        old_stale = self.state("group/project#1", last_meaningful_update_at=600.0)
        other_project = self.state(
            "other/project#1",
            project_id="project-2",
            owner=None,
        )
        self.states = {state.ref: state for state in (release_stale, unowned, old_stale, other_project)}

        with patch("codex_web.services.work_item_watchdog_candidates.time.time", return_value=1000.0):
            candidates = self.policy.orchestrator_candidates("project-1")

        self.assertEqual(
            [(reason, state.ref) for reason, state, _ in candidates],
            [
                ("stale_lane", "group/project#3"),
                ("unowned", "group/project#2"),
                ("stale_lane", "group/project#1"),
            ],
        )

    def test_split_brain_candidates_filter_closed_items_and_sort_release_gate_first(self) -> None:
        normal = self.state("group/project#2")
        release = self.state("group/project#3", release_gate=True)
        clean = self.state("group/project#1")
        closed = self.state("group/project#4", stage="closed", closed_at=950.0)
        self.states = {state.ref: state for state in (normal, release, clean, closed)}
        self.split_findings = {
            normal.ref: ["normal mismatch"],
            release.ref: ["release mismatch"],
            closed.ref: ["closed mismatch"],
        }

        candidates = self.policy.split_brain_candidates("project-1")

        self.assertEqual(
            [(state.ref, findings) for state, findings in candidates],
            [
                ("group/project#3", ["release mismatch"]),
                ("group/project#2", ["normal mismatch"]),
            ],
        )

    def test_installer_rebinds_historical_candidate_names(self) -> None:
        app = SimpleNamespace(state=SimpleNamespace())
        host = SimpleNamespace(
            _load_work_item_states=lambda: {},
            _work_item_split_brain_findings=lambda state: [],
            _work_item_handoff_timeout_seconds=lambda: 900.0,
            _coerce_owner=lambda value: value,
            _work_item_sla_threshold_seconds=lambda state: 100.0,
        )

        policy = install_work_item_watchdog_candidate_policy(app, host)

        self.assertIs(app.state.work_item_watchdog_candidate_policy, policy)
        self.assertIs(host._orchestrator_watch_reason.__self__, policy)
        self.assertIs(host._orchestrator_watchdog_candidates.__self__, policy)
        self.assertIs(host._split_brain_watchdog_candidates.__self__, policy)


if __name__ == "__main__":
    unittest.main()
