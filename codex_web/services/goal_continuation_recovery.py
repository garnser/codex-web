from __future__ import annotations

import time
from dataclasses import dataclass

from codex_web.agent_runtime import AgentSessionStatus
from codex_web.goal_execution_bindings import (
    GoalExecutionBindingStatus,
    GoalExecutionBindingUpdate,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    PrincipalKind,
    TenantScope,
)
from codex_web.services.agent_runtime import AgentSessionService
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
        GoalExecutionBindingStatus.UNKNOWN,
    }

    def __init__(
        self,
        bindings: GoalExecutionBindingService,
        continuation: GoalContinuationService,
        agent_sessions: AgentSessionService,
    ) -> None:
        self.bindings = bindings
        self.continuation = continuation
        self.agent_sessions = agent_sessions

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

    @staticmethod
    def _actor(scope: TenantScope) -> AuthenticationActor:
        return AuthenticationActor(
            identity_id="goal-continuation-recovery",
            principal_kind=PrincipalKind.SERVICE,
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("goals:continue",),
        )

    def _expired_active_outcome_known(self, binding, *, scope: TenantScope) -> bool:
        if binding.status != GoalExecutionBindingStatus.ACTIVE:
            return True
        try:
            session = self.agent_sessions.get(
                binding.agent_session_id,
                self._actor(scope),
            )
        except Exception:
            return False
        return session.status != AgentSessionStatus.RUNNING

    async def recover_scope(
        self,
        *,
        scope: TenantScope,
        now: float | None = None,
    ) -> GoalContinuationRecoveryResult:
        current_time = time.time() if now is None else float(now)
        bindings = self.bindings.list_all(scope=scope)
        candidates = tuple(
            item for item in bindings if self._eligible(item, now=current_time)
        )
        eligible = []
        for binding in candidates:
            if self._expired_active_outcome_known(binding, scope=scope):
                eligible.append(binding)
                continue
            self.bindings.update(
                binding.id,
                GoalExecutionBindingUpdate(
                    status=GoalExecutionBindingStatus.UNKNOWN,
                    stop_reason=(
                        "expired continuation lease with unresolved provider turn outcome"
                    ),
                    reason="continuation recovery requires provider outcome reconciliation",
                ),
                scope=scope,
                actor_id="goal-continuation-recovery",
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
