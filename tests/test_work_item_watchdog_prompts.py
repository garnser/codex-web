from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.services.work_item_watchdog_prompts import (
    WorkItemWatchdogPromptPolicy,
    install_work_item_watchdog_prompt_policy,
)


class WorkItemWatchdogPromptPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = SimpleNamespace(name="Platform", path="/workspace/platform")
        self.host = SimpleNamespace(_project=lambda project_id: self.project)
        self.policy = WorkItemWatchdogPromptPolicy(self.host)

    @staticmethod
    def state(
        ref: str,
        *,
        owner: str | None = "james",
        next_owner: str | None = None,
        stage: str = "implementation_active",
        next_action: str | None = "continue implementation",
        handoff: object | None = None,
        blocker: str | None = None,
        blocking_findings: list[str] | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            ref=ref,
            current_owner=owner,
            next_owner=next_owner,
            current_stage=stage,
            next_action=next_action,
            handoff=handoff,
            blocker=blocker,
            blocking_findings=blocking_findings or [],
        )

    def test_human_duration_matches_legacy_boundaries(self) -> None:
        self.assertEqual(self.policy.human_duration(-1), "0s")
        self.assertEqual(self.policy.human_duration(59.9), "59s")
        self.assertEqual(self.policy.human_duration(60), "1m")
        self.assertEqual(self.policy.human_duration(61), "1m 1s")
        self.assertEqual(self.policy.human_duration(3600), "1h")
        self.assertEqual(self.policy.human_duration(3660), "1h 1m")
        self.assertEqual(self.policy.human_duration(86400), "1d")
        self.assertEqual(self.policy.human_duration(90000), "1d 1h")

    def test_orchestrator_prompt_preserves_work_item_details(self) -> None:
        handoff = SimpleNamespace(
            status="pending",
            from_agent="james",
            to_agent="quinn",
            requested_at=700.0,
        )
        state = self.state(
            "group/project#1",
            handoff=handoff,
            blocker="awaiting test fixture",
            blocking_findings=["unit tests failing", "review pending", "security gate", "ignored"],
        )

        with patch("codex_web.services.work_item_watchdog_prompts.time.time", return_value=1000.0):
            prompt = self.policy.format_orchestrator_prompt(
                "project-1",
                [("pending_handoff", state, 301.0)],
            )

        self.assertIn("Project: Platform (/workspace/platform)", prompt)
        self.assertIn("trigger=pending_handoff", prompt)
        self.assertIn("age=5m 1s", prompt)
        self.assertIn("pending_handoff=james->quinn for 5m", prompt)
        self.assertIn("blocker=awaiting test fixture", prompt)
        self.assertIn("blocking_findings=unit tests failing | review pending | security gate", prompt)
        self.assertNotIn("ignored", prompt)

    def test_orchestrator_prompt_caps_visible_items_at_eight(self) -> None:
        items = [
            ("stale_lane", self.state(f"group/project#{index}"), float(index))
            for index in range(1, 10)
        ]
        prompt = self.policy.format_orchestrator_prompt("project-1", items)

        self.assertIn("8. group/project#8", prompt)
        self.assertNotIn("9. group/project#9", prompt)
        self.assertIn("... plus 1 more stale items.", prompt)

    def test_split_brain_prompt_preserves_findings_and_limits_items(self) -> None:
        items = [
            (
                self.state(
                    f"group/project#{index}",
                    owner=None,
                    next_owner="quinn",
                    next_action="reconcile owner",
                    blocking_findings=["canonical mismatch"],
                ),
                ["owner label disagrees", "handoff state disagrees"],
            )
            for index in range(1, 10)
        ]

        prompt = self.policy.format_split_brain_prompt("project-1", items)

        self.assertIn("owner=unassigned", prompt)
        self.assertIn("next_owner=quinn", prompt)
        self.assertIn("   - owner label disagrees", prompt)
        self.assertIn("next_action=reconcile owner", prompt)
        self.assertIn("blocking_findings=canonical mismatch", prompt)
        self.assertIn("8. group/project#8", prompt)
        self.assertNotIn("9. group/project#9", prompt)
        self.assertIn("... plus 1 more split-brain items.", prompt)

    def test_installer_rebinds_historical_prompt_names(self) -> None:
        app = SimpleNamespace(state=SimpleNamespace())
        host = SimpleNamespace(_project=lambda project_id: self.project)

        policy = install_work_item_watchdog_prompt_policy(app, host)

        self.assertIs(app.state.work_item_watchdog_prompt_policy, policy)
        self.assertIs(host._format_orchestrator_watchdog_prompt.__self__, policy)
        self.assertIs(host._format_split_brain_watchdog_prompt.__self__, policy)
        self.assertIs(host._human_duration.__self__, policy)


if __name__ == "__main__":
    unittest.main()
