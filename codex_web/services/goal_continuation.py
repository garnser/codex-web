from __future__ import annotations

from dataclasses import dataclass

from codex_web.agent_runtime import AgentRuntimeTurnRequest
from codex_web.goal_execution_bindings import (
    GoalExecutionBindingStatus,
    GoalExecutionBindingUpdate,
)
from codex_web.autonomy import AutonomyMode
from codex_web.goals import GoalHealth, GoalStatus
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    PrincipalKind,
    TenantScope,
)
from codex_web.services.agent_runtime import AgentSessionService
from codex_web.services.autonomy_controller import AutonomyController
from codex_web.services.goal_execution_bindings import GoalExecutionBindingService
from codex_web.services.goals import GoalService


@dataclass(frozen=True)
class GoalContinuationDispatchResult:
    binding_id: str
    outcome: str
    turn_id: str | None = None
    retry_after_seconds: float | None = None
    reason: str | None = None


class GoalContinuationService:
    """Lease-owned, bounded continuation dispatcher for canonical Goal bindings."""

    def __init__(
        self,
        bindings: GoalExecutionBindingService,
        goals: GoalService,
        agent_sessions: AgentSessionService,
        *,
        owner_id: str,
        autonomy: AutonomyController | None = None,
        lease_seconds: float = 120.0,
        max_recovery_attempts: int = 4,
        retry_base_seconds: float = 5.0,
    ) -> None:
        self.bindings = bindings
        self.goals = goals
        self.agent_sessions = agent_sessions
        self.autonomy = autonomy
        self.owner_id = owner_id
        self.lease_seconds = max(1.0, float(lease_seconds))
        self.max_recovery_attempts = max(1, int(max_recovery_attempts))
        self.retry_base_seconds = max(0.0, float(retry_base_seconds))

    @staticmethod
    def _actor(scope: TenantScope) -> AuthenticationActor:
        return AuthenticationActor(
            identity_id="goal-continuation-service",
            principal_kind=PrincipalKind.SERVICE,
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("goals:continue",),
        )

    @staticmethod
    def _prompt(binding) -> str:
        roots = ", ".join(binding.work_item_refs) if binding.work_item_refs else "the bound Project graph"
        return (
            f"Continue canonical Goal {binding.goal_id} revision {binding.goal_revision} "
            f"within Project {binding.project_id}, limited to {roots}. "
            "Canonical Goal and Work Graph state remain authoritative. "
            "Do not broaden scope. Stop when completed, blocked, an approval is required, "
            "or another canonical stop condition is reached."
        )

    def recovery_attempt_limit(self, goal) -> int:
        limit = self.max_recovery_attempts
        if goal.budget.max_retries is not None:
            limit = min(limit, int(goal.budget.max_retries) + 1)
        return max(1, limit)

    def recovery_attempt_limit_for(
        self,
        binding,
        *,
        scope: TenantScope,
    ) -> int:
        goal = self.goals.get(binding.goal_id, scope=scope)
        return self.recovery_attempt_limit(goal)

    def _policy_stop_reason(self, binding) -> str | None:
        if self.autonomy is None:
            return None
        control = self.autonomy.store.load().control
        if control.mode != AutonomyMode.ACTIVE:
            return f"global_autonomy_{control.mode.value}"
        exclusive = control.exclusive_goal_scope
        if exclusive is None:
            return "exclusive_goal_scope_required"
        if exclusive.goal_id != binding.goal_id:
            return f"exclusive_goal_scope_goal:{exclusive.goal_id}"
        if exclusive.project_id != binding.project_id:
            return f"exclusive_goal_scope_project:{exclusive.project_id}"
        if exclusive.root_work_item_refs:
            allowed = set(exclusive.root_work_item_refs)
            if not binding.work_item_refs:
                return f"exclusive_goal_scope_work_graph:{exclusive.goal_id}"
            if any(ref not in allowed for ref in binding.work_item_refs):
                return f"exclusive_goal_scope_work_graph:{exclusive.goal_id}"
        return None

    def _finish_binding(
        self,
        binding,
        *,
        scope: TenantScope,
        status: GoalExecutionBindingStatus,
        reason: str,
    ) -> GoalContinuationDispatchResult:
        self.bindings.update(
            binding.id,
            GoalExecutionBindingUpdate(
                status=status,
                stop_reason=reason,
                reason=reason,
            ),
            scope=scope,
            actor_id=self.owner_id,
        )
        return GoalContinuationDispatchResult(
            binding_id=binding.id,
            outcome=(
                "completed"
                if status == GoalExecutionBindingStatus.COMPLETED
                else "blocked"
            ),
            reason=reason,
        )

    async def dispatch_once(
        self,
        binding_id: str,
        *,
        scope: TenantScope,
        now: float | None = None,
    ) -> GoalContinuationDispatchResult:
        binding = self.bindings.get(binding_id, scope=scope)
        goal = self.goals.get(binding.goal_id, scope=scope)

        if goal.status != GoalStatus.ACTIVE:
            return GoalContinuationDispatchResult(
                binding_id=binding_id,
                outcome="not_dispatched",
                reason=f"canonical_goal_{goal.status.value}",
            )
        if goal.revision != binding.goal_revision:
            self.bindings.update(
                binding.id,
                GoalExecutionBindingUpdate(
                    status=GoalExecutionBindingStatus.BLOCKED,
                    stop_reason=(
                        f"canonical Goal revision changed from "
                        f"{binding.goal_revision} to {goal.revision}"
                    ),
                    reason="continuation blocked by canonical Goal revision change",
                ),
                scope=scope,
                actor_id=self.owner_id,
            )
            return GoalContinuationDispatchResult(
                binding_id=binding_id,
                outcome="blocked",
                reason="goal_revision_changed",
            )

        policy_stop = self._policy_stop_reason(binding)
        if policy_stop is not None:
            return self._finish_binding(
                binding,
                scope=scope,
                status=GoalExecutionBindingStatus.BLOCKED,
                reason=policy_stop,
            )

        snapshot = self.goals.snapshot(binding.goal_id, scope=scope)
        evaluation = self.goals.completion_evaluation(
            binding.goal_id,
            scope=scope,
        )
        if evaluation is not None and evaluation.eligible:
            return self._finish_binding(
                binding,
                scope=scope,
                status=GoalExecutionBindingStatus.COMPLETED,
                reason=f"deterministic_completion:{evaluation.id}",
            )
        if snapshot.progress.failed or snapshot.progress.cancelled:
            return self._finish_binding(
                binding,
                scope=scope,
                status=GoalExecutionBindingStatus.BLOCKED,
                reason="bound_work_has_terminal_failure",
            )
        if (
            snapshot.progress.work_item_count > 0
            and snapshot.progress.active == 0
        ):
            return self._finish_binding(
                binding,
                scope=scope,
                status=GoalExecutionBindingStatus.BLOCKED,
                reason="deterministic_completion_verification_required",
            )
        if snapshot.health.health == GoalHealth.BLOCKED:
            return self._finish_binding(
                binding,
                scope=scope,
                status=GoalExecutionBindingStatus.BLOCKED,
                reason="canonical_goal_work_graph_blocked",
            )

        claimed = self.bindings.claim_continuation(
            binding.id,
            scope=scope,
            owner_id=self.owner_id,
            lease_seconds=self.lease_seconds,
            now=now,
        )
        if claimed is None:
            return GoalContinuationDispatchResult(
                binding_id=binding_id,
                outcome="not_claimed",
                reason="lease_or_retry_gate_unavailable",
            )

        try:
            result = await self.agent_sessions.start_turn(
                claimed.agent_session_id,
                AgentRuntimeTurnRequest(
                    message=self._prompt(claimed),
                ),
                actor=self._actor(scope),
            )
        except Exception as exc:
            attempt = claimed.recovery_attempts + 1
            terminal = attempt >= self.recovery_attempt_limit(goal)
            retry_after = (
                None
                if terminal
                else self.retry_base_seconds * (2 ** max(0, attempt - 1))
            )
            self.bindings.release_continuation(
                claimed.id,
                scope=scope,
                owner_id=self.owner_id,
                status=(
                    GoalExecutionBindingStatus.BLOCKED
                    if terminal
                    else GoalExecutionBindingStatus.FAILED
                ),
                reason=(
                    f"continuation retry budget exhausted: {exc}"
                    if terminal
                    else f"continuation turn failed: {exc}"
                ),
                retry_after_seconds=retry_after,
                increment_recovery=True,
                now=now,
            )
            return GoalContinuationDispatchResult(
                binding_id=binding_id,
                outcome="blocked" if terminal else "retry_scheduled",
                retry_after_seconds=retry_after,
                reason=str(exc),
            )

        turn_id = getattr(result, "provider_native_turn_id", None)
        self.bindings.update(
            claimed.id,
            GoalExecutionBindingUpdate(
                status=GoalExecutionBindingStatus.ACTIVE,
                last_turn_id=turn_id,
                reason="continuation turn started",
            ),
            scope=scope,
            actor_id=self.owner_id,
        )
        return GoalContinuationDispatchResult(
            binding_id=binding_id,
            outcome="started",
            turn_id=turn_id,
        )

    def resolve_turn(
        self,
        binding_id: str,
        *,
        scope: TenantScope,
        reason: str,
        terminal_status: GoalExecutionBindingStatus | None = None,
        now: float | None = None,
    ):
        binding = self.bindings.get(binding_id, scope=scope)
        if binding.lease_owner_id != self.owner_id:
            return binding
        return self.bindings.release_continuation(
            binding_id,
            scope=scope,
            owner_id=self.owner_id,
            status=terminal_status or GoalExecutionBindingStatus.IDLE,
            reason=reason,
            now=now,
        )
