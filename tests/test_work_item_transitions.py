from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from codex_web.models import WorkItemProgressUpdate, WorkItemState
from codex_web.services.work_item_state import WorkItemStateMachine
from codex_web.services.work_item_transitions import (
    WORK_ITEM_STAGES,
    WORK_ITEM_STAGE_TRANSITIONS,
    WorkItemTransitionPolicy,
    WorkItemTransitionService,
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


class WorkItemTransitionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = WorkItemTransitionService()
        self.state = WorkItemState(
            ref="group/project#1",
            current_stage="implementation_active",
            last_meaningful_update_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )

    def test_valid_manual_transition_mutates_stage(self) -> None:
        result = self.service.transition(
            self.state,
            "ready_for_validation",
            source="test",
        )
        self.assertIs(result, self.state)
        self.assertEqual(self.state.current_stage, "ready_for_validation")

    def test_invalid_manual_transition_rejects_without_mutating_stage(self) -> None:
        with self.assertRaises(HTTPException):
            self.service.transition(self.state, "closed", source="test")
        self.assertEqual(self.state.current_stage, "implementation_active")

    def test_external_projection_uses_same_mutator_but_can_reopen_closed_lane(self) -> None:
        self.state.current_stage = "closed"
        result = self.service.transition(
            self.state,
            "implementation_active",
            source="gitlab-test",
            external_projection=True,
        )
        self.assertIs(result, self.state)
        self.assertEqual(self.state.current_stage, "implementation_active")


class _StateHost:
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

    def _leading_owner_cue_in_action(self, value):
        return None


class WorkItemStateMachineTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.host = _StateHost(Path(self.temp.name))
        self.machine = WorkItemStateMachine(self.host)
        self.state = WorkItemState(
            ref="group/project#1",
            current_owner="dana",
            current_stage="implementation_active",
            artifact_state="branch",
            last_meaningful_update_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )
        self.host.states[self.state.ref] = self.state.model_copy(deep=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_direct_state_machine_progress_rejects_illegal_transition(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            self.machine._structured_progress(
                self.state.ref,
                WorkItemProgressUpdate(actor="dana", current_stage="closed"),
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "invalid_stage_transition")
        self.assertEqual(self.host.states[self.state.ref].current_stage, "implementation_active")

    def test_direct_state_machine_progress_applies_legal_transition(self) -> None:
        result = self.machine._structured_progress(
            self.state.ref,
            WorkItemProgressUpdate(actor="dana", current_stage="ready_for_validation"),
        )
        self.assertEqual(result.current_stage, "ready_for_validation")
        self.assertEqual(self.host.states[self.state.ref].current_stage, "ready_for_validation")


class WorkItemServiceCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_lightweight_override_keeps_historical_mutation_path(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
