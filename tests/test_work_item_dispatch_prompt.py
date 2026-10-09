from __future__ import annotations

import unittest

from codex_web.models import WorkItemHandoff, WorkItemState
from codex_web.services.work_item_dispatch_prompt import WorkItemDispatchPromptPolicy


class WorkItemDispatchPromptTests(unittest.TestCase):
    def setUp(self):
        self.policy = WorkItemDispatchPromptPolicy(
            coerce_owner=lambda value: (value or "").strip().lower(),
            coordination_channel="coordination")
        self.state = WorkItemState(ref="example/gtm!16", project_id="project-a",
            current_owner="Sally", next_owner="Carl", implementation_owner="Sally",
            validation_owner="Quinn", current_stage="failed_with_action_owner",
            next_action="Carl: provide the renderer prerequisite",
            created_at=1.0, updated_at=2.0, last_meaningful_update_at=2.0)

    def test_failed_addressee_normal_owner_and_absent_next_owner(self):
        for stage, next_owner, expected in [
            ("failed_with_action_owner", "Carl", "Carl"),
            ("failed_with_action_owner", None, "Sally"),
            ("implementation_active", "Carl", "Sally"),
            ("validation_running", "Carl", "Sally"),
        ]:
            with self.subTest(stage=stage, next_owner=next_owner):
                state = self.state.model_copy(update={"current_stage": stage,
                    "next_owner": next_owner})
                before = state.model_dump()
                self.assertTrue(self.policy.render(state).startswith(expected + ": "))
                self.assertEqual(state.model_dump(), before)

    def test_historical_accepted_handoff_is_not_claimed_for_a_different_action_owner(self):
        state = self.state.model_copy(update={"handoff": WorkItemHandoff(
            from_agent="James", to_agent="Sally", status="accepted",
            requested_at=1.0, acknowledged_at=2.0)})
        before = state.model_dump()
        message = self.policy.render(state)
        self.assertTrue(message.startswith("Carl: owned-work SLA"))
        self.assertNotIn("accepted handoff is live", message)
        self.assertEqual(state.model_dump(), before)

    def test_matching_accepted_action_owner_remains_live(self):
        state = self.state.model_copy(update={"ref": "example/gtm#3",
            "current_owner": "Orchestrator", "next_owner": "Orchestrator",
            "handoff": WorkItemHandoff(from_agent="Sally", to_agent="Orchestrator",
                status="accepted", requested_at=1.0, acknowledged_at=2.0)})
        before = state.model_dump()
        self.assertTrue(self.policy.render(state).startswith("Orchestrator: accepted handoff"))
        self.assertEqual(state.model_dump(), before)

    def test_pending_handoff_addresses_its_recipient_before_next_owner(self):
        state = self.state.model_copy(update={"handoff": WorkItemHandoff(
            from_agent="Sally", to_agent="Dana", requested_at=1.0)})
        before = state.model_dump()
        message = self.policy.render(state)
        self.assertTrue(message.startswith("Dana: structured handoff pending"))
        self.assertIn("/api/work-items/example%2Fgtm%2116/ack", message)
        self.assertEqual(state.model_dump(), before)
