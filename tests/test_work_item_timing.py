from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.models import WorkItemHandoff, WorkItemState
from codex_web.services.work_item_timing import WorkItemTimingPolicy, install_work_item_timing_policy


class WorkItemTimingPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = SimpleNamespace(_coerce_owner=lambda value: (value or "").strip().lower() or None)
        self.policy = WorkItemTimingPolicy(self.host)

    def state(self, *, stage: str = "implementation_active", accepted: bool = False) -> WorkItemState:
        handoff = None
        if accepted:
            handoff = WorkItemHandoff(
                from_agent="james",
                to_agent="quinn",
                requested_at=1.0,
                acknowledged_at=2.0,
                status="accepted",
            )
        return WorkItemState(
            ref="group/project#1",
            project_id="project-1",
            current_owner="quinn" if accepted else "james",
            current_stage=stage,
            handoff=handoff,
            last_meaningful_update_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )

    def test_defaults_and_invalid_values(self) -> None:
        keys = {
            "CODEX_WEB_WORK_ITEM_HANDOFF_TIMEOUT_SECONDS": None,
            "CODEX_WEB_WORK_ITEM_PROGRESS_SLA_SECONDS": None,
            "CODEX_WEB_RELEASE_VALIDATION_SLA_SECONDS": None,
            "CODEX_WEB_ACCEPTED_HANDOFF_OWNER_IDLE_SECONDS": None,
        }
        with patch.dict(os.environ, {key: "" for key in keys}, clear=False):
            self.assertEqual(self.policy.handoff_timeout_seconds(), 900.0)
            self.assertEqual(self.policy.progress_sla_seconds(), 3600.0)
            self.assertEqual(self.policy.release_validation_sla_seconds(), 1800.0)
            self.assertEqual(self.policy.accepted_handoff_owner_idle_seconds(), 300.0)

        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_WORK_ITEM_HANDOFF_TIMEOUT_SECONDS": "bad",
                "CODEX_WEB_WORK_ITEM_PROGRESS_SLA_SECONDS": "bad",
                "CODEX_WEB_RELEASE_VALIDATION_SLA_SECONDS": "bad",
                "CODEX_WEB_ACCEPTED_HANDOFF_OWNER_IDLE_SECONDS": "bad",
            },
            clear=False,
        ):
            self.assertEqual(self.policy.handoff_timeout_seconds(), 900.0)
            self.assertEqual(self.policy.progress_sla_seconds(), 3600.0)
            self.assertEqual(self.policy.release_validation_sla_seconds(), 1800.0)
            self.assertEqual(self.policy.accepted_handoff_owner_idle_seconds(), 300.0)

    def test_configured_values_are_clamped_to_existing_minimums(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_WORK_ITEM_HANDOFF_TIMEOUT_SECONDS": "5",
                "CODEX_WEB_WORK_ITEM_PROGRESS_SLA_SECONDS": "5",
                "CODEX_WEB_RELEASE_VALIDATION_SLA_SECONDS": "5",
                "CODEX_WEB_ACCEPTED_HANDOFF_OWNER_IDLE_SECONDS": "5",
            },
            clear=False,
        ):
            self.assertEqual(self.policy.handoff_timeout_seconds(), 60.0)
            self.assertEqual(self.policy.progress_sla_seconds(), 300.0)
            self.assertEqual(self.policy.release_validation_sla_seconds(), 300.0)
            self.assertEqual(self.policy.accepted_handoff_owner_idle_seconds(), 60.0)

    def test_sla_threshold_preserves_stage_and_accepted_handoff_policy(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_WORK_ITEM_PROGRESS_SLA_SECONDS": "3600",
                "CODEX_WEB_RELEASE_VALIDATION_SLA_SECONDS": "1800",
                "CODEX_WEB_ACCEPTED_HANDOFF_OWNER_IDLE_SECONDS": "450",
            },
            clear=False,
        ):
            self.assertEqual(self.policy.sla_threshold_seconds(self.state()), 3600.0)
            self.assertEqual(
                self.policy.sla_threshold_seconds(self.state(stage="ready_for_validation")),
                1800.0,
            )
            self.assertEqual(self.policy.sla_threshold_seconds(self.state(accepted=True)), 450.0)
            self.assertEqual(
                self.policy.sla_threshold_seconds(
                    self.state(stage="validation_running", accepted=True)
                ),
                450.0,
            )

    def test_accepted_handoff_only_uses_idle_limit_for_the_recipient(self) -> None:
        state = self.state(accepted=True)
        state.current_owner = "james"
        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_WORK_ITEM_PROGRESS_SLA_SECONDS": "3600",
                "CODEX_WEB_ACCEPTED_HANDOFF_OWNER_IDLE_SECONDS": "450",
            },
            clear=False,
        ):
            self.assertEqual(self.policy.sla_threshold_seconds(state), 3600.0)

    def test_installer_rebinds_all_historical_timing_names(self) -> None:
        app = SimpleNamespace(state=SimpleNamespace())
        host = SimpleNamespace(_coerce_owner=self.host._coerce_owner)
        policy = install_work_item_timing_policy(app, host)

        self.assertIs(app.state.work_item_timing_policy, policy)
        self.assertIs(host._work_item_sla_threshold_seconds.__self__, policy)
        self.assertIs(host._work_item_handoff_timeout_seconds, policy.handoff_timeout_seconds)
        self.assertIs(host._work_item_progress_sla_seconds, policy.progress_sla_seconds)
        self.assertIs(host._release_validation_sla_seconds, policy.release_validation_sla_seconds)
        self.assertIs(
            host._accepted_handoff_owner_idle_seconds,
            policy.accepted_handoff_owner_idle_seconds,
        )


if __name__ == "__main__":
    unittest.main()
