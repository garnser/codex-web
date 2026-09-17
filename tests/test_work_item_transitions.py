from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import HTTPException

from codex_web.models import WorkItemProgressUpdate, WorkItemState
from codex_web.services.work_item_transitions import (
    WORK_ITEM_STAGES,
    WORK_ITEM_STAGE_TRANSITIONS,
    WorkItemTransitionPolicy,
)
from codex_web.services.work_items import WorkItemService


class WorkItemTransitionPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = WorkItemTransitionPolicy()

    def test_every_stage_has_an_explicit_transition_set(self) -> None:
        self.assertEqual(set(WORK_ITEM_STAGE_TRANSITIONS), set(WORK_ITEM_STAGES))
        for stage in WORK_ITEM_STAGES:
            self.assertIn(stage, WORK_ITEM_STAGE_TRANSITIONS[stage])

    def test_every_stage_pair_matches_the_canonical_matrix(self) -> None:
        for current_stage in WORK_ITEM_STAGES:
            for target_stage in WORK_ITEM_STAGES:
                with self.subTest(current=current_stage, target=target_stage):
                    if target_stage in WORK_ITEM_STAGE_TRANSITIONS[current_stage]:
                        self.assertEqual(
                            self.policy.validate(
                                current_stage,
                                target_stage,
                                source="test",
                            ),
                            target_stage,
                        )
                    else:
                        with self.assertRaises(HTTPException) as raised:
                            self.policy.validate(
                                current_stage,
                                target_stage,
                                source="test",
                            )
                        self.assertEqual(raised.exception.status_code, 409)
                        self.assertEqual(raised.exception.detail["code"], "invalid_stage_transition")
                        self.assertEqual(raised.exception.detail["from_stage"], current_stage)
                        self.assertEqual(raised.exception.detail["to_stage"], target_stage)

    def test_manual_closed_lane_is_terminal(self) -> None:
        for target_stage in WORK_ITEM_STAGES:
            if target_stage == "closed":
                continue
            with self.subTest(target=target_stage):
                with self.assertRaises(HTTPException) as raised:
                    self.policy.validate("closed", target_stage, source="manual-progress")
                self.assertEqual(raised.exception.detail["allowed_targets"], ["closed"])

    def test_implementation_cannot_skip_directly_to_validation_running_or_close(self) -> None:
        for target_stage in ("validation_running", "ready_to_close", "closed"):
            with self.subTest(target=target_stage):
                with self.assertRaises(HTTPException):
                    self.policy.validate(
                        "implementation_active",
                        target_stage,
                        source="manual-progress",
                    )

    def test_blocked_lane_can_resume_any_non_terminal_lane(self) -> None:
        expected = set(WORK_ITEM_STAGES) - {"closed"}
        self.assertEqual(
            set(self.policy.allowed_targets("failed_with_action_owner")),
            expected,
        )


class WorkItemServiceTransitionGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_illegal_progress_transition_is_rejected_before_mutation(self) -> None:
        state = WorkItemState(
            ref="group/project#1",
            current_owner="dana",
            current_stage="implementation_active",
            artifact_state="branch",
            last_meaningful_update_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )

        class StateMachine:
            mutation_called = False

            def _work_item_state(self, ref):
                return state

            def _structured_progress(self, ref, payload):
                self.mutation_called = True
                return state

        machine = StateMachine()
        host = SimpleNamespace()
        service = WorkItemService(host, state_machine=machine)

        with self.assertRaises(HTTPException) as raised:
            await service.progress(
                state.ref,
                WorkItemProgressUpdate(
                    actor="dana",
                    current_stage="closed",
                ),
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "invalid_stage_transition")
        self.assertFalse(machine.mutation_called)

    async def test_compatibility_double_without_state_lookup_keeps_historical_path(self) -> None:
        class StateMachine:
            mutation_called = False

            def _structured_progress(self, ref, payload):
                self.mutation_called = True
                return SimpleNamespace(ref=ref)

            async def sync_gitlab_issue_labels(self, state):
                return state

            def _work_item_state_public(self, state):
                return {"ref": state.ref}

            def _work_item_split_brain_findings(self, state):
                return []

        class Hub:
            events = []

            async def publish(self, event):
                self.events.append(event)

        class Host:
            hub = Hub()

            def _schedule_actionable_owner_dispatch(self, state, *, source, actor):
                pass

            def _schedule_actionable_owner_continuity_check(self, state, *, source):
                pass

            def _schedule_native_recovery_cycles(self, *, reason):
                pass

        machine = StateMachine()
        service = WorkItemService(Host(), state_machine=machine)

        result = await service.progress(
            "group/project#1",
            WorkItemProgressUpdate(actor="dana", current_stage="closed"),
        )

        self.assertTrue(machine.mutation_called)
        self.assertEqual(result["item"]["ref"], "group/project#1")

    async def test_lookup_only_404_defers_to_compatibility_mutation_path(self) -> None:
        class StateMachine:
            mutation_called = False

            def _work_item_state(self, ref):
                raise HTTPException(status_code=404, detail="Work item state not found")

            def _structured_progress(self, ref, payload):
                self.mutation_called = True
                return SimpleNamespace(ref=ref)

            async def sync_gitlab_issue_labels(self, state):
                return state

            def _work_item_state_public(self, state):
                return {"ref": state.ref}

            def _work_item_split_brain_findings(self, state):
                return []

        class Hub:
            events = []

            async def publish(self, event):
                self.events.append(event)

        class Host:
            hub = Hub()

            def _schedule_actionable_owner_dispatch(self, state, *, source, actor):
                pass

            def _schedule_actionable_owner_continuity_check(self, state, *, source):
                pass

            def _schedule_native_recovery_cycles(self, *, reason):
                pass

        machine = StateMachine()
        service = WorkItemService(Host(), state_machine=machine)

        result = await service.progress(
            "group/project#1",
            WorkItemProgressUpdate(actor="dana", current_stage="closed"),
        )

        self.assertTrue(machine.mutation_called)
        self.assertEqual(result["item"]["ref"], "group/project#1")


if __name__ == "__main__":
    unittest.main()
