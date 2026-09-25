from __future__ import annotations

import unittest
from types import SimpleNamespace

from codex_web.goal_execution_bindings import GoalExecutionBindingStatus
from codex_web.identity import TenantScope
from codex_web.services.goal_continuation import GoalContinuationDispatchResult
from codex_web.services.goal_continuation_recovery import (
    GoalContinuationRecoveryService,
)


class _Bindings:
    def __init__(self, rows):
        self.rows = tuple(rows)
        self.scopes = []

    def list_all(self, *, scope):
        self.scopes.append(scope)
        return self.rows


class _Continuation:
    def __init__(self):
        self.calls = []

    async def dispatch_once(self, binding_id, *, scope, now=None):
        self.calls.append((binding_id, scope, now))
        return GoalContinuationDispatchResult(
            binding_id=binding_id,
            outcome="started",
            turn_id=f"turn-{binding_id}",
        )


def _binding(
    binding_id,
    *,
    status=GoalExecutionBindingStatus.IDLE,
    lease_owner_id=None,
    lease_expires_at=None,
    retry_not_before_at=None,
):
    return SimpleNamespace(
        id=binding_id,
        status=status,
        lease_owner_id=lease_owner_id,
        lease_expires_at=lease_expires_at,
        retry_not_before_at=retry_not_before_at,
    )


class GoalContinuationRecoveryServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scope = TenantScope(
            organization_id="org-a",
            workspace_id="ws-a",
        )

    async def test_recovery_dispatches_idle_failed_requested_and_expired_leases(self):
        rows = [
            _binding("idle"),
            _binding("failed", status=GoalExecutionBindingStatus.FAILED),
            _binding("requested", status=GoalExecutionBindingStatus.REQUESTED),
            _binding(
                "expired-active",
                status=GoalExecutionBindingStatus.ACTIVE,
                lease_owner_id="old-worker",
                lease_expires_at=99.0,
            ),
        ]
        bindings = _Bindings(rows)
        continuation = _Continuation()
        service = GoalContinuationRecoveryService(bindings, continuation)

        result = await service.recover_scope(scope=self.scope, now=100.0)

        self.assertEqual(result.scanned, 4)
        self.assertEqual(result.eligible, 4)
        self.assertEqual(
            [item.binding_id for item in result.dispatched],
            ["idle", "failed", "requested", "expired-active"],
        )
        self.assertEqual(bindings.scopes, [self.scope])
        self.assertEqual(
            [call[0] for call in continuation.calls],
            ["idle", "failed", "requested", "expired-active"],
        )

    async def test_recovery_skips_terminal_backoff_and_live_leases(self):
        rows = [
            _binding("blocked", status=GoalExecutionBindingStatus.BLOCKED),
            _binding("completed", status=GoalExecutionBindingStatus.COMPLETED),
            _binding("cancelled", status=GoalExecutionBindingStatus.CANCELLED),
            _binding("backoff", retry_not_before_at=101.0),
            _binding(
                "live",
                status=GoalExecutionBindingStatus.ACTIVE,
                lease_owner_id="worker-a",
                lease_expires_at=101.0,
            ),
        ]
        continuation = _Continuation()
        service = GoalContinuationRecoveryService(_Bindings(rows), continuation)

        result = await service.recover_scope(scope=self.scope, now=100.0)

        self.assertEqual(result.scanned, 5)
        self.assertEqual(result.eligible, 0)
        self.assertEqual(result.dispatched, ())
        self.assertEqual(continuation.calls, [])

    async def test_recovery_treats_exact_retry_and_lease_expiry_as_eligible(self):
        rows = [
            _binding("retry-now", retry_not_before_at=100.0),
            _binding(
                "lease-now",
                status=GoalExecutionBindingStatus.ACTIVE,
                lease_owner_id="worker-a",
                lease_expires_at=100.0,
            ),
        ]
        continuation = _Continuation()
        service = GoalContinuationRecoveryService(_Bindings(rows), continuation)

        result = await service.recover_scope(scope=self.scope, now=100.0)

        self.assertEqual(result.eligible, 2)
        self.assertEqual(
            [call[0] for call in continuation.calls],
            ["retry-now", "lease-now"],
        )


if __name__ == "__main__":
    unittest.main()
