from __future__ import annotations

import time
from dataclasses import dataclass

from codex_web.goal_execution_bindings import GoalExecutionBindingStatus
from codex_web.identity import TenantScope
from codex_web.services.goal_continuation import (
    GoalContinuationDispatchResult,
    GoalContinuationService,
)
from codex_web.services.goal_execution_bindings import GoalExecutionBindingService


@dataclass(frozen=True)
class GoalContinuationRecoveryResult:
    scanned: int
    eligible: int
    dispatched: tuple[GoalContinuationDispatchResult, ...]


class GoalContinuationRecoveryService:
    """Restart-safe reconciliation for durable Goal continuation bindings."""

    _TERMINAL = {
        GoalExecutionBindingStatus.BLOCKED,
        GoalExecutionBindingStatus.COMPLETED,
        GoalExecutionBindingStatus.CANCELLED,
    }

    def __init__(
        self,
        bindings: GoalExecutionBindingService,
        continuation: GoalContinuationService,
    ) -> None:
        self.bindings = bindings
        self.continuation = continuation

    @classmethod
    def _eligible(cls, binding, *, now: float) -> bool:
        if binding.status in cls._TERMINAL:
            return False
        if (
            binding.retry_not_before_at is not None
            and binding.retry_not_before_at > now
        ):
            return False
        if (
            binding.lease_owner_id is not None
            and binding.lease_expires_at is not None
            and binding.lease_expires_at > now
        ):
            return False
        return True

    async def recover_scope(
        self,
        *,
        scope: TenantScope,
        now: float | None = None,
    ) -> GoalContinuationRecoveryResult:
        current_time = time.time() if now is None else float(now)
        bindings = self.bindings.list_all(scope=scope)
        eligible = tuple(
            item for item in bindings if self._eligible(item, now=current_time)
        )
        results: list[GoalContinuationDispatchResult] = []
        for binding in eligible:
            results.append(
                await self.continuation.dispatch_once(
                    binding.id,
                    scope=scope,
                    now=current_time,
                )
            )
        return GoalContinuationRecoveryResult(
            scanned=len(bindings),
            eligible=len(eligible),
            dispatched=tuple(results),
        )
