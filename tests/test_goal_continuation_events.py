from __future__ import annotations

import unittest
from types import SimpleNamespace

from codex_web.agent_runtime import AgentRuntimeEvent
from codex_web.goal_execution_bindings import GoalExecutionBindingStatus
from codex_web.identity import TenantScope
from codex_web.services.goal_continuation import GoalContinuationDispatchResult
from codex_web.services.goal_continuation_events import GoalContinuationEventService


class _Bindings:
    def __init__(self, binding):
        self.binding = binding
        self.releases = []

    def list_all(self, *, scope):
        return (self.binding,)

    def release_continuation(self, binding_id, **kwargs):
        self.releases.append((binding_id, kwargs))
        self.binding = SimpleNamespace(
            **{
                **self.binding.__dict__,
                "lease_owner_id": None,
                "status": kwargs["status"],
                "recovery_attempts": self.binding.recovery_attempts
                + (1 if kwargs.get("increment_recovery") else 0),
            }
        )
        return self.binding


class _Sessions:
    def __init__(self, session_id="session-a"):
        self.session_id = session_id

    def find_by_native_id(self, native_id, actor, **kwargs):
        if native_id != "native-session-a":
            return None
        return SimpleNamespace(id=self.session_id)


class _Continuation:
    def __init__(self, bindings):
        self.owner_id = "worker-a"
        self.max_recovery_attempts = 4
        self.retry_base_seconds = 5.0
        self.bindings = bindings
        self.resolved = []
        self.dispatched = []

    def recovery_attempt_limit_for(self, binding, *, scope):
        return self.max_recovery_attempts

    def resolve_turn(
        self,
        binding_id,
        *,
        scope,
        reason,
        terminal_status=None,
        now=None,
    ):
        self.resolved.append(
            (binding_id, scope, reason, terminal_status, now)
        )
        self.bindings.binding = SimpleNamespace(
            **{
                **self.bindings.binding.__dict__,
                "lease_owner_id": None,
                "status": terminal_status or GoalExecutionBindingStatus.IDLE,
            }
        )
        return self.bindings.binding

    async def dispatch_once(self, binding_id, *, scope, now=None):
        self.dispatched.append((binding_id, scope, now))
        return GoalContinuationDispatchResult(
            binding_id=binding_id,
            outcome="started",
            turn_id="turn-next",
        )


def _binding(**changes):
    values = {
        "id": "binding-a",
        "agent_session_id": "session-a",
        "last_turn_id": "turn-a",
        "status": GoalExecutionBindingStatus.ACTIVE,
        "lease_owner_id": "worker-a",
        "recovery_attempts": 0,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _event(event_type, *, turn_id="turn-a", session_id="native-session-a"):
    return AgentRuntimeEvent(
        event_type=event_type,
        provider_native_session_id=session_id,
        provider_native_turn_id=turn_id,
    )


class GoalContinuationEventServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scope = TenantScope(
            organization_id="org-a",
            workspace_id="ws-a",
        )

    async def test_completed_turn_releases_and_dispatches_next_bounded_turn(self):
        bindings = _Bindings(_binding())
        continuation = _Continuation(bindings)
        service = GoalContinuationEventService(
            bindings,
            _Sessions(),
            continuation,
        )

        result = await service.handle_event(
            _event("turn.completed"),
            scope=self.scope,
            now=100.0,
        )

        self.assertEqual(result.outcome, "continued")
        self.assertEqual(result.binding_id, "binding-a")
        self.assertEqual(result.continuation.turn_id, "turn-next")
        self.assertEqual(
            continuation.resolved[-1][3],
            GoalExecutionBindingStatus.IDLE,
        )
        self.assertEqual(
            continuation.dispatched,
            [("binding-a", self.scope, 100.0)],
        )

    async def test_failed_turn_releases_with_retry_backoff(self):
        bindings = _Bindings(_binding())
        continuation = _Continuation(bindings)
        service = GoalContinuationEventService(
            bindings,
            _Sessions(),
            continuation,
        )

        result = await service.handle_event(
            _event("turn/failed"),
            scope=self.scope,
            now=100.0,
        )

        self.assertEqual(result.outcome, "retry_scheduled")
        release = bindings.releases[-1][1]
        self.assertEqual(release["status"], GoalExecutionBindingStatus.FAILED)
        self.assertEqual(release["retry_after_seconds"], 5.0)
        self.assertTrue(release["increment_recovery"])

    async def test_failed_turn_blocks_when_retry_budget_is_exhausted(self):
        bindings = _Bindings(_binding(recovery_attempts=3))
        continuation = _Continuation(bindings)
        service = GoalContinuationEventService(
            bindings,
            _Sessions(),
            continuation,
        )

        result = await service.handle_event(
            _event("turn.failed"),
            scope=self.scope,
            now=100.0,
        )

        self.assertEqual(result.outcome, "blocked")
        release = bindings.releases[-1][1]
        self.assertEqual(release["status"], GoalExecutionBindingStatus.BLOCKED)
        self.assertIsNone(release["retry_after_seconds"])

    async def test_interrupted_turn_releases_without_automatic_redispatch(self):
        bindings = _Bindings(_binding())
        continuation = _Continuation(bindings)
        service = GoalContinuationEventService(
            bindings,
            _Sessions(),
            continuation,
        )

        result = await service.handle_event(
            _event("turn.interrupted"),
            scope=self.scope,
            now=100.0,
        )

        self.assertEqual(result.outcome, "interrupted")
        self.assertEqual(continuation.dispatched, [])
        self.assertEqual(
            continuation.resolved[-1][3],
            GoalExecutionBindingStatus.IDLE,
        )

    async def test_duplicate_or_foreign_owner_event_is_ignored(self):
        bindings = _Bindings(_binding(lease_owner_id=None))
        continuation = _Continuation(bindings)
        service = GoalContinuationEventService(
            bindings,
            _Sessions(),
            continuation,
        )

        result = await service.handle_event(
            _event("turn.completed"),
            scope=self.scope,
        )

        self.assertEqual(result.outcome, "ignored")
        self.assertEqual(result.reason, "continuation_lease_not_owned")
        self.assertEqual(continuation.resolved, [])
        self.assertEqual(continuation.dispatched, [])

    async def test_wrong_turn_and_non_terminal_events_are_ignored(self):
        bindings = _Bindings(_binding())
        continuation = _Continuation(bindings)
        service = GoalContinuationEventService(
            bindings,
            _Sessions(),
            continuation,
        )

        wrong_turn = await service.handle_event(
            _event("turn.completed", turn_id="turn-other"),
            scope=self.scope,
        )
        non_terminal = await service.handle_event(
            _event("item.started"),
            scope=self.scope,
        )

        self.assertEqual(wrong_turn.outcome, "ignored")
        self.assertEqual(non_terminal.outcome, "ignored")
        self.assertEqual(continuation.resolved, [])


if __name__ == "__main__":
    unittest.main()
