from __future__ import annotations

import unittest
from types import SimpleNamespace

from codex_web.agent_runtime import (
    AgentRuntimeResult,
    AgentSessionStatus,
)
from codex_web.agent_runtime_usage import RuntimeTerminalOutcome
from codex_web.autonomy import AutonomyMode
from codex_web.goal_execution_bindings import GoalExecutionBindingStatus
from codex_web.goals import GoalStatus
from codex_web.identity import TenantScope
from codex_web.services.goal_continuation import GoalContinuationService


class _Autonomy:
    def __init__(self, *, mode=AutonomyMode.ACTIVE, scope=True) -> None:
        exclusive = (
            SimpleNamespace(
                goal_id="goal-a",
                project_id="project-a",
                root_work_item_refs=("root-a",),
            )
            if scope
            else None
        )
        control = SimpleNamespace(
            mode=mode,
            exclusive_goal_scope=exclusive,
        )
        self.store = SimpleNamespace(
            load=lambda: SimpleNamespace(control=control)
        )


class _Bindings:
    def __init__(self) -> None:
        self.binding = SimpleNamespace(
            id="binding-a",
            goal_id="goal-a",
            goal_revision=4,
            project_id="project-a",
            work_item_refs=("root-a",),
            agent_session_id="session-a",
            recovery_attempts=0,
            lease_owner_id=None,
        )
        self.claimed = False
        self.updated = []
        self.released = []

    def get(self, binding_id, *, scope):
        return self.binding

    def claim_continuation(
        self,
        binding_id,
        *,
        scope,
        owner_id,
        lease_seconds,
        now=None,
    ):
        if self.claimed:
            return None
        self.claimed = True
        self.binding = SimpleNamespace(
            **{
                **self.binding.__dict__,
                "lease_owner_id": owner_id,
            }
        )
        return self.binding

    def update(self, binding_id, payload, *, scope, actor_id):
        self.updated.append((binding_id, payload, actor_id))
        return self.binding

    def release_continuation(self, binding_id, **kwargs):
        self.released.append((binding_id, kwargs))
        self.binding = SimpleNamespace(
            **{
                **self.binding.__dict__,
                "lease_owner_id": None,
                "recovery_attempts": (
                    self.binding.recovery_attempts
                    + (1 if kwargs.get("increment_recovery") else 0)
                ),
            }
        )
        return self.binding


class _Goals:
    def __init__(self, *, status=GoalStatus.ACTIVE, revision=4) -> None:
        self.goal = SimpleNamespace(status=status, revision=revision)

    def get(self, goal_id, *, scope):
        return self.goal


class _AgentSessions:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls = []

    async def start_turn(self, session_id, request, *, actor):
        self.calls.append((session_id, request, actor))
        if self.error is not None:
            raise self.error
        return AgentRuntimeResult(
            provider_native_session_id="thread-a",
            provider_native_turn_id="turn-7",
        )


class GoalContinuationServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.scope = TenantScope(
            organization_id="org-a",
            workspace_id="ws-a",
        )

    async def test_dispatch_claims_before_starting_turn_and_records_turn_id(self) -> None:
        bindings = _Bindings()
        sessions = _AgentSessions()
        service = GoalContinuationService(
            bindings,
            _Goals(),
            sessions,
            owner_id="worker-a",
            autonomy=_Autonomy(),
            lease_seconds=90,
        )

        result = await service.dispatch_once(
            "binding-a",
            scope=self.scope,
            now=100.0,
        )

        self.assertEqual(result.outcome, "started")
        self.assertEqual(result.turn_id, "turn-7")
        self.assertTrue(bindings.claimed)
        self.assertEqual(len(sessions.calls), 1)
        session_id, request, actor = sessions.calls[0]
        self.assertEqual(session_id, "session-a")
        self.assertIn("canonical Goal goal-a revision 4", request.message)
        self.assertIn("root-a", request.message)
        self.assertEqual(actor.organization_id, "org-a")
        self.assertEqual(actor.workspace_id, "ws-a")
        self.assertEqual(bindings.updated[-1][1].last_turn_id, "turn-7")

    async def test_non_active_goal_never_claims_or_dispatches(self) -> None:
        bindings = _Bindings()
        sessions = _AgentSessions()
        service = GoalContinuationService(
            bindings,
            _Goals(status=GoalStatus.PAUSED),
            sessions,
            owner_id="worker-a",
        autonomy=_Autonomy(),
        )

        result = await service.dispatch_once(
            "binding-a",
            scope=self.scope,
        )

        self.assertEqual(result.outcome, "not_dispatched")
        self.assertEqual(result.reason, "canonical_goal_paused")
        self.assertFalse(bindings.claimed)
        self.assertEqual(sessions.calls, [])

    async def test_goal_revision_change_blocks_without_dispatch(self) -> None:
        bindings = _Bindings()
        sessions = _AgentSessions()
        service = GoalContinuationService(
            bindings,
            _Goals(revision=5),
            sessions,
            owner_id="worker-a",
        autonomy=_Autonomy(),
        )

        result = await service.dispatch_once(
            "binding-a",
            scope=self.scope,
        )

        self.assertEqual(result.outcome, "blocked")
        self.assertEqual(result.reason, "goal_revision_changed")
        self.assertFalse(bindings.claimed)
        self.assertEqual(sessions.calls, [])
        self.assertEqual(
            bindings.updated[-1][1].status,
            GoalExecutionBindingStatus.BLOCKED,
        )

    async def test_turn_failure_releases_with_exponential_backoff(self) -> None:
        bindings = _Bindings()
        sessions = _AgentSessions(error=RuntimeError("runtime unavailable"))
        service = GoalContinuationService(
            bindings,
            _Goals(),
            sessions,
            owner_id="worker-a",
            autonomy=_Autonomy(),
            max_recovery_attempts=4,
            retry_base_seconds=5,
        )

        result = await service.dispatch_once(
            "binding-a",
            scope=self.scope,
            now=100.0,
        )

        self.assertEqual(result.outcome, "retry_scheduled")
        self.assertEqual(result.retry_after_seconds, 5)
        release = bindings.released[-1][1]
        self.assertEqual(release["status"], GoalExecutionBindingStatus.FAILED)
        self.assertEqual(release["retry_after_seconds"], 5)
        self.assertTrue(release["increment_recovery"])

    async def test_retry_budget_exhaustion_blocks_binding(self) -> None:
        bindings = _Bindings()
        bindings.binding = SimpleNamespace(
            **{
                **bindings.binding.__dict__,
                "recovery_attempts": 3,
            }
        )
        sessions = _AgentSessions(error=RuntimeError("still unavailable"))
        service = GoalContinuationService(
            bindings,
            _Goals(),
            sessions,
            owner_id="worker-a",
            autonomy=_Autonomy(),
            max_recovery_attempts=4,
            retry_base_seconds=5,
        )

        result = await service.dispatch_once(
            "binding-a",
            scope=self.scope,
            now=100.0,
        )

        self.assertEqual(result.outcome, "blocked")
        self.assertIsNone(result.retry_after_seconds)
        release = bindings.released[-1][1]
        self.assertEqual(release["status"], GoalExecutionBindingStatus.BLOCKED)
        self.assertIsNone(release["retry_after_seconds"])

    async def test_global_kill_and_missing_exclusive_scope_fail_closed(self) -> None:
        for autonomy, expected in (
            (_Autonomy(mode=AutonomyMode.KILLED), "autonomy_killed"),
            (_Autonomy(scope=False), "exclusive_goal_scope_not_enabled"),
        ):
            bindings = _Bindings()
            sessions = _AgentSessions()
            service = GoalContinuationService(
                bindings,
                _Goals(),
                sessions,
                owner_id="worker-a",
                autonomy=autonomy,
            )
            result = await service.dispatch_once(
                "binding-a",
                scope=self.scope,
            )
            self.assertEqual(result.outcome, "not_dispatched")
            self.assertEqual(result.reason, expected)
            self.assertFalse(bindings.claimed)
            self.assertEqual(sessions.calls, [])

    async def test_existing_lease_or_backoff_prevents_duplicate_dispatch(self) -> None:
        bindings = _Bindings()
        bindings.claimed = True
        sessions = _AgentSessions()
        service = GoalContinuationService(
            bindings,
            _Goals(),
            sessions,
            owner_id="worker-b",
        autonomy=_Autonomy(),
        )

        result = await service.dispatch_once(
            "binding-a",
            scope=self.scope,
        )

        self.assertEqual(result.outcome, "not_claimed")
        self.assertEqual(sessions.calls, [])


if __name__ == "__main__":
    unittest.main()
