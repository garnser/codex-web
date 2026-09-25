from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.goal_execution_bindings import (
    GoalExecutionBindingCreate,
    GoalExecutionBindingStatus,
    GoalExecutionBindingUpdate,
)
from codex_web.goals import GoalRecord, GoalWorkGraphBinding
from codex_web.identity import TenantScope
from codex_web.services.goal_execution_bindings import (
    GoalExecutionBindingConflictError,
    GoalExecutionBindingNotFoundError,
    GoalExecutionBindingService,
)
from codex_web.storage.goal_execution_bindings import GoalExecutionBindingStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _Goals:
    def __init__(self) -> None:
        self.goal = GoalRecord(
            id="goal-a",
            organization_id="org-a",
            workspace_id="ws-a",
            title="Bounded release validation",
            description="Canonical authority remains in Goal state.",
            owner_identity_id="owner-a",
            work_graph_bindings=(
                GoalWorkGraphBinding(
                    project_id="project-a",
                    root_work_item_refs=("root-a",),
                ),
            ),
            revision=4,
            created_by="creator",
            updated_by="creator",
        )

    def get(self, goal_id, *, scope):
        if (
            goal_id == self.goal.id
            and scope.organization_id == self.goal.organization_id
            and scope.workspace_id == self.goal.workspace_id
        ):
            return self.goal
        from codex_web.services.goals import GoalNotFoundError
        raise GoalNotFoundError("goal not found")


class GoalExecutionBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.service = GoalExecutionBindingService(
            GoalExecutionBindingStore(sqlite),
            _Goals(),
        )
        self.scope = TenantScope(organization_id="org-a", workspace_id="ws-a")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def payload(**changes):
        values = {
            "project_id": "project-a",
            "goal_revision": 4,
            "work_item_refs": ("root-a",),
            "provider_id": "openai",
            "runtime_id": "codex",
            "agent_session_id": "session-a",
            "thread_id": "thread-a",
            "execution_owner_id": "validator",
            "provider_native_objective_id": "native-goal-a",
            "native_objective_supported": True,
            "capability_snapshot": ("persistent_sessions", "native_execution_objectives"),
            "cursor_ref": "cursor-1",
            "checkpoint_ref": "checkpoint-1",
            "reason": "bind approved validation scope",
        }
        values.update(changes)
        return GoalExecutionBindingCreate(**values)

    def test_binding_pins_canonical_revision_and_runtime_provenance(self) -> None:
        item = self.service.create(
            "goal-a",
            self.payload(),
            scope=self.scope,
            actor_id="admin",
        )

        self.assertEqual(item.goal_revision, 4)
        self.assertEqual(item.project_id, "project-a")
        self.assertEqual(item.work_item_refs, ("root-a",))
        self.assertEqual(item.provider_native_objective_id, "native-goal-a")
        self.assertTrue(item.native_objective_supported)
        self.assertEqual(item.status, GoalExecutionBindingStatus.REQUESTED)

        changed = self.service.update(
            item.id,
            GoalExecutionBindingUpdate(
                status=GoalExecutionBindingStatus.ACTIVE,
                cursor_ref="cursor-2",
                last_turn_id="turn-7",
                heartbeat_at=123.0,
                reason="bounded continuation turn completed",
            ),
            scope=self.scope,
            actor_id="runtime-supervisor",
        )
        self.assertEqual(changed.status, GoalExecutionBindingStatus.ACTIVE)
        self.assertEqual(changed.cursor_ref, "cursor-2")
        self.assertEqual(changed.last_turn_id, "turn-7")

        events = self.service.events("goal-a", scope=self.scope)
        self.assertEqual(
            [event.event_type for event in events],
            ["binding_created", "binding_active"],
        )
        self.assertEqual(events[-1].reason, "bounded continuation turn completed")

    def test_stale_goal_revision_and_broader_scope_fail_closed(self) -> None:
        with self.assertRaises(GoalExecutionBindingConflictError):
            self.service.create(
                "goal-a",
                self.payload(goal_revision=3),
                scope=self.scope,
                actor_id="admin",
            )

        with self.assertRaises(GoalExecutionBindingConflictError):
            self.service.create(
                "goal-a",
                self.payload(project_id="project-b"),
                scope=self.scope,
                actor_id="admin",
            )

        with self.assertRaises(GoalExecutionBindingConflictError):
            self.service.create(
                "goal-a",
                self.payload(work_item_refs=("other-root",)),
                scope=self.scope,
                actor_id="admin",
            )

    def test_native_objective_is_capability_gated_and_does_not_change_goal(self) -> None:
        with self.assertRaises(ValueError):
            self.payload(native_objective_supported=False)

        binding = self.service.create(
            "goal-a",
            self.payload(
                provider_native_objective_id=None,
                native_objective_supported=False,
                capability_snapshot=("persistent_sessions",),
            ),
            scope=self.scope,
            actor_id="admin",
        )
        self.assertFalse(binding.native_objective_supported)
        self.assertIsNone(binding.provider_native_objective_id)
        self.assertEqual(self.service.goals.goal.status.value, "draft")
        self.assertEqual(self.service.goals.goal.revision, 4)

    def test_tenant_isolation_and_duplicate_active_session_binding(self) -> None:
        item = self.service.create(
            "goal-a",
            self.payload(),
            scope=self.scope,
            actor_id="admin",
        )
        with self.assertRaises(GoalExecutionBindingConflictError):
            self.service.create(
                "goal-a",
                self.payload(reason="duplicate"),
                scope=self.scope,
                actor_id="admin",
            )
        with self.assertRaises(GoalExecutionBindingNotFoundError):
            self.service.get(
                item.id,
                scope=TenantScope(organization_id="org-b", workspace_id="ws-b"),
            )


if __name__ == "__main__":
    unittest.main()
