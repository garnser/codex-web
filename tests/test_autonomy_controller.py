from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.action_intents import ActionIntentCreate, ActionIntentStatus
from codex_web.action_providers import ActionRequest
from codex_web.autonomy import (
    AutonomyControlUpdate,
    AutonomyCycleOutcome,
    AutonomyExclusiveGoalScope,
    AutonomyObservation,
    AutonomyReasoningResult,
    AutonomyScopedPauseCreate,
)
from codex_web.compatibility import CanonicalEventEnvelope
from codex_web.services.autonomy_controller import AutonomyController
from codex_web.storage.autonomy import AutonomyStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _IntentService:
    def __init__(self, *, denied: bool = False) -> None:
        self.denied = denied
        self.calls = []

    def create(self, payload, *, actor):
        self.calls.append((payload, actor))
        return SimpleNamespace(
            id=f"intent-{len(self.calls)}",
            status=(
                ActionIntentStatus.CANCELLED
                if self.denied
                else ActionIntentStatus.PENDING
            ),
        )


class AutonomyControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = AutonomyStateStore(SQLiteStateStore(self.path))
        self.controller = AutonomyController(self.store)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def event(
        event_id: str = "evt-1",
        event_type: str = "work.transition",
    ) -> CanonicalEventEnvelope:
        return CanonicalEventEnvelope(
            event_id=event_id,
            event_type=event_type,
            occurred_at=1.0,
            source="test",
            correlation_id="corr-1",
            tenant_id="org-a",
            workspace_id="ws-a",
            payload={"ref": "TASK-1"},
        )

    async def test_deterministic_resolution_never_invokes_reasoner(self) -> None:
        calls = 0

        async def reasoner(*_args):
            nonlocal calls
            calls += 1
            return AutonomyReasoningResult(summary="should not run")

        cycle = await self.controller.process(
            self.event(),
            AutonomyObservation(
                deterministic_resolved=True,
                reasoning_score=1.0,
                reason="known state transition handled in code",
            ),
            reasoner=reasoner,
        )

        self.assertEqual(cycle.outcome, AutonomyCycleOutcome.DETERMINISTIC)
        self.assertFalse(cycle.reasoning_invoked)
        self.assertEqual(calls, 0)

    async def test_threshold_blocks_reasoning_below_configured_score(self) -> None:
        self.controller.update_control(
            AutonomyControlUpdate(reasoning_threshold=0.8),
            actor_id="admin",
        )
        calls = 0

        async def reasoner(*_args):
            nonlocal calls
            calls += 1
            return AutonomyReasoningResult()

        cycle = await self.controller.process(
            self.event(),
            AutonomyObservation(
                deterministic_resolved=False,
                reasoning_score=0.5,
                reason="ambiguous but below configured threshold",
            ),
            reasoner=reasoner,
        )

        self.assertEqual(cycle.outcome, AutonomyCycleOutcome.SKIPPED)
        self.assertEqual(cycle.reason, "reasoning_threshold_not_met")
        self.assertEqual(calls, 0)

    async def test_duplicate_cycle_is_idempotent_and_does_not_reason_twice(self) -> None:
        self.controller.update_control(
            AutonomyControlUpdate(cooldown_seconds=0),
            actor_id="admin",
        )
        calls = 0

        async def reasoner(*_args):
            nonlocal calls
            calls += 1
            return AutonomyReasoningResult(summary="done")

        observation = AutonomyObservation(
            deterministic_resolved=False,
            reasoning_score=1.0,
            reason="requires reasoning",
        )
        first = await self.controller.process(
            self.event(),
            observation,
            cycle_key="route-a",
            reasoner=reasoner,
        )
        second = await self.controller.process(
            self.event(),
            observation,
            cycle_key="route-a",
            reasoner=reasoner,
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(first.outcome, AutonomyCycleOutcome.COMPLETED)
        self.assertEqual(calls, 1)

    async def test_repeated_event_class_is_suppressed_during_cooldown(self) -> None:
        self.controller.update_control(
            AutonomyControlUpdate(cooldown_seconds=60),
            actor_id="admin",
        )
        calls = 0

        async def reasoner(*_args):
            nonlocal calls
            calls += 1
            return AutonomyReasoningResult(summary="done")

        observation = AutonomyObservation(
            deterministic_resolved=False,
            reasoning_score=1.0,
            reason="requires reasoning",
        )
        first = await self.controller.process(
            self.event("evt-1"),
            observation,
            cycle_key="same-class",
            reasoner=reasoner,
        )
        second = await self.controller.process(
            self.event("evt-2"),
            observation,
            cycle_key="same-class",
            reasoner=reasoner,
        )

        self.assertEqual(first.outcome, AutonomyCycleOutcome.COMPLETED)
        self.assertEqual(second.outcome, AutonomyCycleOutcome.SKIPPED)
        self.assertEqual(second.reason, "reasoning_cooldown_active")
        self.assertEqual(calls, 1)

    async def test_pause_kill_dry_run_and_simulation_prevent_reasoning(self) -> None:
        calls = 0

        async def reasoner(*_args):
            nonlocal calls
            calls += 1
            return AutonomyReasoningResult()

        observation = AutonomyObservation(
            deterministic_resolved=False,
            reasoning_score=1.0,
            reason="requires reasoning",
        )

        self.controller.pause(actor_id="admin")
        paused = await self.controller.process(
            self.event("paused"),
            observation,
            reasoner=reasoner,
        )
        self.assertEqual(paused.reason, "autonomy_paused")

        self.controller.kill(actor_id="admin")
        killed = await self.controller.process(
            self.event("killed"),
            observation,
            reasoner=reasoner,
        )
        self.assertEqual(killed.reason, "autonomy_killed")

        self.controller.update_control(
            AutonomyControlUpdate(mode="active", dry_run=True),
            actor_id="admin",
        )
        dry = await self.controller.process(
            self.event("dry"),
            observation,
            reasoner=reasoner,
        )
        self.assertEqual(dry.outcome, AutonomyCycleOutcome.DRY_RUN)

        self.controller.update_control(
            AutonomyControlUpdate(dry_run=False, simulation=True),
            actor_id="admin",
        )
        simulated = await self.controller.process(
            self.event("sim"),
            observation,
            reasoner=reasoner,
        )
        self.assertEqual(simulated.outcome, AutonomyCycleOutcome.SIMULATED)
        self.assertEqual(calls, 0)

    async def test_exclusive_goal_scope_allows_only_matching_goal_project_and_roots(self) -> None:
        self.controller.update_control(
            AutonomyControlUpdate(cooldown_seconds=0),
            actor_id="admin",
        )
        self.controller.set_exclusive_goal_scope(
            AutonomyExclusiveGoalScope(
                goal_id="goal-a",
                project_id="project-a",
                root_work_item_refs=("root-a",),
                reason="continue only bounded release validation",
            ),
            actor_id="admin",
        )
        calls = 0

        async def reasoner(*_args):
            nonlocal calls
            calls += 1
            return AutonomyReasoningResult(summary="continued")

        observation = AutonomyObservation(
            deterministic_resolved=False,
            reasoning_score=1.0,
            reason="continuation required",
        )

        def scoped_event(event_id, **payload):
            return CanonicalEventEnvelope(
                event_id=event_id,
                event_type="goal.continuation",
                occurred_at=1.0,
                source="test",
                correlation_id="corr-1",
                tenant_id="org-a",
                workspace_id="ws-a",
                payload=payload,
            )

        allowed = await self.controller.process(
            scoped_event(
                "allowed",
                goal_id="goal-a",
                project_id="project-a",
                work_item_ref="root-a",
            ),
            observation,
            reasoner=reasoner,
        )
        wrong_goal = await self.controller.process(
            scoped_event(
                "wrong-goal",
                goal_id="goal-b",
                project_id="project-a",
                work_item_ref="root-a",
            ),
            observation,
            reasoner=reasoner,
        )
        wrong_project = await self.controller.process(
            scoped_event(
                "wrong-project",
                goal_id="goal-a",
                project_id="project-b",
                work_item_ref="root-a",
            ),
            observation,
            reasoner=reasoner,
        )
        wrong_root = await self.controller.process(
            scoped_event(
                "wrong-root",
                goal_id="goal-a",
                project_id="project-a",
                work_item_ref="root-b",
            ),
            observation,
            reasoner=reasoner,
        )

        self.assertEqual(allowed.outcome, AutonomyCycleOutcome.COMPLETED)
        self.assertEqual(wrong_goal.outcome, AutonomyCycleOutcome.SKIPPED)
        self.assertEqual(
            wrong_goal.reason,
            "autonomy_exclusive_goal_scope:goal:goal-a",
        )
        self.assertEqual(
            wrong_project.reason,
            "autonomy_exclusive_goal_scope:project:project-a",
        )
        self.assertEqual(
            wrong_root.reason,
            "autonomy_exclusive_goal_scope:work_graph:goal-a",
        )
        self.assertEqual(calls, 1)

    async def test_global_kill_precedes_exclusive_goal_scope(self) -> None:
        self.controller.set_exclusive_goal_scope(
            AutonomyExclusiveGoalScope(
                goal_id="goal-a",
                project_id="project-a",
                reason="exclusive bounded continuation",
            ),
            actor_id="admin",
        )
        self.controller.kill(actor_id="admin")

        cycle = await self.controller.process(
            CanonicalEventEnvelope(
                event_id="killed-exclusive",
                event_type="goal.continuation",
                occurred_at=1.0,
                source="test",
                tenant_id="org-a",
                workspace_id="ws-a",
                payload={
                    "goal_id": "unrelated-goal",
                    "project_id": "other-project",
                },
            ),
            AutonomyObservation(
                deterministic_resolved=False,
                reasoning_score=1.0,
                reason="would otherwise continue",
            ),
        )

        self.assertEqual(cycle.outcome, AutonomyCycleOutcome.SKIPPED)
        self.assertEqual(cycle.reason, "autonomy_killed")

    async def test_exclusive_goal_scope_persists_and_can_be_cleared(self) -> None:
        scope = AutonomyExclusiveGoalScope(
            goal_id="goal-a",
            project_id="project-a",
            root_work_item_refs=("root-a", "root-a"),
            reason="bounded continuation",
        )
        self.controller.set_exclusive_goal_scope(scope, actor_id="operator")

        restarted = AutonomyController(
            AutonomyStateStore(SQLiteStateStore(self.path))
        )
        stored = restarted.store.load().control.exclusive_goal_scope
        self.assertIsNotNone(stored)
        self.assertEqual(stored.goal_id, "goal-a")
        self.assertEqual(stored.root_work_item_refs, ("root-a",))

        restarted.clear_exclusive_goal_scope(actor_id="operator")
        self.assertIsNone(
            restarted.store.load().control.exclusive_goal_scope
        )

    async def test_reasoning_retries_are_bounded_then_dead_lettered(self) -> None:
        self.controller.update_control(
            AutonomyControlUpdate(
                max_reasoning_attempts=3,
                reasoning_backoff_seconds=0,
                cooldown_seconds=0,
            ),
            actor_id="admin",
        )
        calls = 0

        async def reasoner(*_args):
            nonlocal calls
            calls += 1
            raise RuntimeError("model unavailable")

        cycle = await self.controller.process(
            self.event(),
            AutonomyObservation(
                deterministic_resolved=False,
                reasoning_score=1.0,
                reason="requires reasoning",
            ),
            reasoner=reasoner,
        )

        self.assertEqual(cycle.outcome, AutonomyCycleOutcome.FAILED)
        self.assertEqual(cycle.reasoning_attempts, 3)
        self.assertEqual(calls, 3)
        state = self.store.load()
        self.assertEqual(len(state.dead_letters), 1)
        self.assertEqual(state.dead_letters[0].reason, "reasoning_attempts_exhausted")

    async def test_recursion_and_action_budgets_fail_closed(self) -> None:
        self.controller.update_control(
            AutonomyControlUpdate(
                max_recursion_depth=1,
                max_actions_per_cycle=1,
                cooldown_seconds=0,
            ),
            actor_id="admin",
        )
        observation = AutonomyObservation(
            deterministic_resolved=False,
            reasoning_score=1.0,
            reason="requires reasoning",
        )

        depth = await self.controller.process(
            self.event("depth"),
            observation,
            depth=2,
            reasoner=lambda *_args: None,
        )
        self.assertEqual(depth.reason, "maximum_recursion_depth_exceeded")

        action = ActionIntentCreate(
            binding_id="binding-a",
            request=ActionRequest(
                action_id="reference.set",
                organization_id="org-a",
                workspace_id="ws-a",
            ),
        )

        async def too_many(*_args):
            return AutonomyReasoningResult(actions=(action, action))

        budget = await self.controller.process(
            self.event("budget"),
            observation,
            reasoner=too_many,
        )
        self.assertEqual(budget.reason, "maximum_actions_per_cycle_exceeded")
        self.assertEqual(budget.action_count, 2)
        self.assertEqual(len(self.store.load().dead_letters), 2)

    async def test_actions_only_cross_action_intent_authority_boundary(self) -> None:
        intents = _IntentService()
        controller = AutonomyController(self.store, action_intents=intents)
        controller.update_control(
            AutonomyControlUpdate(cooldown_seconds=0),
            actor_id="admin",
        )
        action = ActionIntentCreate(
            binding_id="binding-a",
            request=ActionRequest(
                action_id="reference.set",
                organization_id="org-a",
                workspace_id="ws-a",
            ),
        )

        async def reasoner(*_args):
            return AutonomyReasoningResult(
                summary="create canonical intent",
                actions=(action,),
            )

        actor = object()
        cycle = await controller.process(
            self.event(),
            AutonomyObservation(
                deterministic_resolved=False,
                reasoning_score=1.0,
                reason="requires side effect",
            ),
            reasoner=reasoner,
            actor=actor,
        )

        self.assertEqual(cycle.outcome, AutonomyCycleOutcome.COMPLETED)
        self.assertEqual(cycle.action_intent_ids, ("intent-1",))
        self.assertEqual(len(intents.calls), 1)
        self.assertIs(intents.calls[0][1], actor)

    async def test_authority_denial_blocks_cycle_without_provider_execution(self) -> None:
        intents = _IntentService(denied=True)
        controller = AutonomyController(self.store, action_intents=intents)
        controller.update_control(
            AutonomyControlUpdate(cooldown_seconds=0),
            actor_id="admin",
        )
        action = ActionIntentCreate(
            binding_id="binding-a",
            request=ActionRequest(
                action_id="reference.set",
                organization_id="org-a",
                workspace_id="ws-a",
            ),
        )

        async def reasoner(*_args):
            return AutonomyReasoningResult(actions=(action,))

        cycle = await controller.process(
            self.event(),
            AutonomyObservation(
                deterministic_resolved=False,
                reasoning_score=1.0,
                reason="requires side effect",
            ),
            reasoner=reasoner,
            actor=object(),
        )

        self.assertEqual(cycle.outcome, AutonomyCycleOutcome.BLOCKED)
        self.assertIn("denied", cycle.reason)
        self.assertEqual(len(intents.calls), 1)

    async def test_controls_and_cycle_history_survive_restart(self) -> None:
        self.controller.update_control(
            AutonomyControlUpdate(mode="paused", max_actions_per_cycle=2),
            actor_id="operator",
        )
        await self.controller.process(
            self.event(),
            AutonomyObservation(
                deterministic_resolved=False,
                reasoning_score=1.0,
                reason="paused",
            ),
        )

        restarted = AutonomyController(
            AutonomyStateStore(SQLiteStateStore(self.path))
        )
        status = restarted.status()

        self.assertEqual(status["control"]["mode"], "paused")
        self.assertEqual(status["control"]["max_actions_per_cycle"], 2)
        self.assertEqual(len(status["recent_cycles"]), 1)
        self.assertEqual(status["updated_by"], "operator")


    async def test_project_scoped_pause_skips_reasoning_and_can_be_removed(self) -> None:
        pause = self.controller.add_scoped_pause(
            AutonomyScopedPauseCreate(
                scope="project",
                scope_id="project-a",
                reason="operator maintenance",
            ),
            actor_id="admin",
        )
        event = CanonicalEventEnvelope(
            event_id="evt-scoped",
            event_type="work.transition",
            occurred_at=1.0,
            source="test",
            correlation_id="corr-scoped",
            tenant_id="org-a",
            workspace_id="ws-a",
            payload={"project_id": "project-a", "resource_ids": ["resource-a"]},
        )
        calls = 0

        async def reasoner(*_args):
            nonlocal calls
            calls += 1
            return AutonomyReasoningResult(summary="must not run")

        cycle = await self.controller.process(
            event,
            AutonomyObservation(
                deterministic_resolved=False,
                reasoning_score=1.0,
                reason="needs reasoning",
            ),
            reasoner=reasoner,
        )
        self.assertEqual(cycle.outcome, AutonomyCycleOutcome.SKIPPED)
        self.assertEqual(
            cycle.reason,
            "autonomy_scoped_pause:project:project-a",
        )
        self.assertEqual(calls, 0)
        self.assertTrue(
            any(item.id == pause.id for item in self.store.load().control.scoped_pauses)
        )
        self.assertTrue(
            self.controller.remove_scoped_pause(pause.id, actor_id="admin")
        )
        self.assertEqual(self.store.load().control.scoped_pauses, ())


if __name__ == "__main__":
    unittest.main()
