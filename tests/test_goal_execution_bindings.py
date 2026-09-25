from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.goal_execution_bindings import (
    GoalExecutionBindingCreate,
    GoalExecutionBindingReconcile,
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

    def test_list_all_is_tenant_scoped(self) -> None:
        item = self.service.create(
            "goal-a",
            self.payload(),
            scope=self.scope,
            actor_id="admin",
        )
        self.assertEqual(
            [row.id for row in self.service.list_all(scope=self.scope)],
            [item.id],
        )
        self.assertEqual(
            self.service.list_all(
                scope=TenantScope(
                    organization_id="org-b",
                    workspace_id="ws-b",
                )
            ),
            (),
        )

    def test_continuation_lease_prevents_concurrent_owners_and_allows_expired_takeover(self) -> None:
        item = self.service.create(
            "goal-a",
            self.payload(),
            scope=self.scope,
            actor_id="admin",
        )

        first = self.service.claim_continuation(
            item.id,
            scope=self.scope,
            owner_id="worker-a",
            lease_seconds=30,
            now=100.0,
        )
        self.assertIsNotNone(first)
        self.assertEqual(first.lease_owner_id, "worker-a")
        self.assertEqual(first.lease_expires_at, 130.0)

        denied = self.service.claim_continuation(
            item.id,
            scope=self.scope,
            owner_id="worker-b",
            lease_seconds=30,
            now=120.0,
        )
        self.assertIsNone(denied)

        takeover = self.service.claim_continuation(
            item.id,
            scope=self.scope,
            owner_id="worker-b",
            lease_seconds=30,
            now=131.0,
        )
        self.assertIsNotNone(takeover)
        self.assertEqual(takeover.lease_owner_id, "worker-b")
        self.assertEqual(takeover.lease_expires_at, 161.0)

    def test_continuation_heartbeat_requires_current_unexpired_owner(self) -> None:
        item = self.service.create(
            "goal-a",
            self.payload(),
            scope=self.scope,
            actor_id="admin",
        )
        self.service.claim_continuation(
            item.id,
            scope=self.scope,
            owner_id="worker-a",
            lease_seconds=30,
            now=100.0,
        )

        heartbeat = self.service.heartbeat_continuation(
            item.id,
            scope=self.scope,
            owner_id="worker-a",
            lease_seconds=30,
            now=110.0,
        )
        self.assertEqual(heartbeat.heartbeat_at, 110.0)
        self.assertEqual(heartbeat.lease_expires_at, 140.0)

        with self.assertRaises(GoalExecutionBindingConflictError):
            self.service.heartbeat_continuation(
                item.id,
                scope=self.scope,
                owner_id="worker-b",
                now=111.0,
            )

        with self.assertRaises(GoalExecutionBindingConflictError):
            self.service.heartbeat_continuation(
                item.id,
                scope=self.scope,
                owner_id="worker-a",
                now=141.0,
            )

    def test_failed_attempt_releases_lease_and_enforces_retry_backoff(self) -> None:
        item = self.service.create(
            "goal-a",
            self.payload(),
            scope=self.scope,
            actor_id="admin",
        )
        self.service.claim_continuation(
            item.id,
            scope=self.scope,
            owner_id="worker-a",
            now=100.0,
        )

        failed = self.service.release_continuation(
            item.id,
            scope=self.scope,
            owner_id="worker-a",
            status=GoalExecutionBindingStatus.FAILED,
            reason="runtime turn failed",
            retry_after_seconds=20,
            increment_recovery=True,
            now=105.0,
        )
        self.assertIsNone(failed.lease_owner_id)
        self.assertIsNone(failed.lease_expires_at)
        self.assertEqual(failed.retry_not_before_at, 125.0)
        self.assertEqual(failed.recovery_attempts, 1)

        self.assertIsNone(
            self.service.claim_continuation(
                item.id,
                scope=self.scope,
                owner_id="worker-b",
                now=124.0,
            )
        )
        retry = self.service.claim_continuation(
            item.id,
            scope=self.scope,
            owner_id="worker-b",
            now=125.0,
        )
        self.assertIsNotNone(retry)
        self.assertEqual(retry.lease_owner_id, "worker-b")

    def test_terminal_and_blocked_bindings_cannot_be_claimed(self) -> None:
        for status in (
            GoalExecutionBindingStatus.COMPLETED,
            GoalExecutionBindingStatus.CANCELLED,
            GoalExecutionBindingStatus.BLOCKED,
            GoalExecutionBindingStatus.UNKNOWN,
        ):
            item = self.service.create(
                "goal-a",
                self.payload(agent_session_id=f"session-{status.value}"),
                scope=self.scope,
                actor_id="admin",
            )
            item = self.service.update(
                item.id,
                GoalExecutionBindingUpdate(
                    status=status,
                    reason=f"set {status.value}",
                ),
                scope=self.scope,
                actor_id="admin",
            )
            self.assertIsNone(
                self.service.claim_continuation(
                    item.id,
                    scope=self.scope,
                    owner_id="worker-a",
                    now=100.0,
                )
            )

    def test_operator_pause_clears_lease_and_requires_explicit_resume(self) -> None:
        item = self.service.create(
            "goal-a",
            self.payload(),
            scope=self.scope,
            actor_id="admin",
        )
        item = self.service.claim_continuation(
            item.id,
            scope=self.scope,
            owner_id="worker-a",
            now=100.0,
        )
        paused = self.service.operator_stop(
            item.id,
            scope=self.scope,
            actor_id="admin",
            cancelled=False,
            reason="maintenance window",
        )
        self.assertEqual(paused.status, GoalExecutionBindingStatus.BLOCKED)
        self.assertTrue(paused.stop_reason.startswith("operator paused:"))
        self.assertIsNone(paused.lease_owner_id)
        self.assertIsNone(paused.lease_expires_at)
        self.assertIsNone(
            self.service.claim_continuation(
                item.id,
                scope=self.scope,
                owner_id="worker-b",
                now=101.0,
            )
        )

        resumed = self.service.resume_operator_pause(
            item.id,
            scope=self.scope,
            actor_id="admin",
            reason="maintenance complete",
        )
        self.assertEqual(resumed.status, GoalExecutionBindingStatus.IDLE)
        self.assertIsNone(resumed.stop_reason)
        events = self.service.events("goal-a", scope=self.scope)
        self.assertEqual(
            [events[-2].event_type, events[-1].event_type],
            ["binding_operator_paused", "binding_operator_resumed"],
        )

    def test_operator_cancel_is_terminal_and_unknown_requires_reconciliation_first(self) -> None:
        item = self.service.create(
            "goal-a",
            self.payload(),
            scope=self.scope,
            actor_id="admin",
        )
        cancelled = self.service.operator_stop(
            item.id,
            scope=self.scope,
            actor_id="admin",
            cancelled=True,
            reason="execution no longer authorized",
        )
        self.assertEqual(cancelled.status, GoalExecutionBindingStatus.CANCELLED)
        self.assertIsNone(
            self.service.claim_continuation(
                item.id,
                scope=self.scope,
                owner_id="worker-a",
                now=100.0,
            )
        )

        unknown = self.service.create(
            "goal-a",
            self.payload(agent_session_id="session-unknown"),
            scope=self.scope,
            actor_id="admin",
        )
        unknown = self.service.update(
            unknown.id,
            GoalExecutionBindingUpdate(
                status=GoalExecutionBindingStatus.UNKNOWN,
                reason="provider outcome unresolved",
            ),
            scope=self.scope,
            actor_id="recovery",
        )
        with self.assertRaises(GoalExecutionBindingConflictError):
            self.service.operator_stop(
                unknown.id,
                scope=self.scope,
                actor_id="admin",
                cancelled=True,
                reason="cannot bypass reconciliation",
            )

    def test_unknown_binding_requires_explicit_reconciliation(self) -> None:
        item = self.service.create(
            "goal-a",
            self.payload(),
            scope=self.scope,
            actor_id="admin",
        )
        item = self.service.update(
            item.id,
            GoalExecutionBindingUpdate(
                status=GoalExecutionBindingStatus.UNKNOWN,
                lease_expires_at=99.0,
                heartbeat_at=98.0,
                reason="provider outcome unresolved",
            ),
            scope=self.scope,
            actor_id="recovery",
        )

        reconciled = self.service.reconcile_unknown(
            item.id,
            GoalExecutionBindingReconcile(
                outcome=GoalExecutionBindingStatus.IDLE,
                reason="operator verified provider turn is no longer running",
            ),
            scope=self.scope,
            actor_id="admin",
        )

        self.assertEqual(reconciled.status, GoalExecutionBindingStatus.IDLE)
        self.assertIsNone(reconciled.lease_owner_id)
        self.assertIsNone(reconciled.lease_expires_at)
        self.assertIsNone(reconciled.retry_not_before_at)
        events = self.service.events("goal-a", scope=self.scope)
        self.assertEqual(events[-1].event_type, "binding_reconciled")
        self.assertIn("as idle", events[-1].reason)

    def test_reconciliation_rejects_non_unknown_binding(self) -> None:
        item = self.service.create(
            "goal-a",
            self.payload(),
            scope=self.scope,
            actor_id="admin",
        )
        with self.assertRaises(GoalExecutionBindingConflictError):
            self.service.reconcile_unknown(
                item.id,
                GoalExecutionBindingReconcile(
                    outcome=GoalExecutionBindingStatus.IDLE,
                    reason="not actually unknown",
                ),
                scope=self.scope,
                actor_id="admin",
            )

    def test_reconciliation_cannot_assert_canonical_completion(self) -> None:
        with self.assertRaises(ValueError):
            GoalExecutionBindingReconcile(
                outcome=GoalExecutionBindingStatus.COMPLETED,
                reason="provider said complete",
            )

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
