from __future__ import annotations

from dataclasses import dataclass
import time

from codex_web.agent_runtime import (
    AgentRuntimeTurnRequest,
    AgentSessionStatus,
)
from codex_web.agent_runtime_usage import RuntimeTerminalOutcome
from codex_web.autonomy import AutonomyMode
from codex_web.goal_execution_bindings import (
    GoalExecutionBindingStatus,
    GoalExecutionBindingUpdate,
)
from codex_web.goals import GoalStatus
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
        lease_seconds: float = 120.0,
        max_recovery_attempts: int = 4,
        retry_base_seconds: float = 5.0,
        autonomy: AutonomyController | None = None,
    ) -> None:
        self.bindings = bindings
        self.goals = goals
        self.agent_sessions = agent_sessions
        self.owner_id = owner_id
        self.lease_seconds = max(1.0, float(lease_seconds))
        self.max_recovery_attempts = max(1, int(max_recovery_attempts))
        self.retry_base_seconds = max(0.0, float(retry_base_seconds))
        self.autonomy = autonomy

    def _continuation_policy_reason(self, binding) -> str | None:
        if self.autonomy is None:
            return "autonomy_control_unavailable"
        control = self.autonomy.store.load().control
        if control.mode != AutonomyMode.ACTIVE:
            return f"autonomy_{control.mode.value}"
        exclusive = control.exclusive_goal_scope
        if exclusive is None:
            return "exclusive_goal_scope_not_enabled"
        if exclusive.goal_id != binding.goal_id:
            return f"exclusive_goal_scope_goal:{exclusive.goal_id}"
        if exclusive.project_id != binding.project_id:
            return f"exclusive_goal_scope_project:{exclusive.project_id}"
        if exclusive.root_work_item_refs and not set(binding.work_item_refs).issubset(
            set(exclusive.root_work_item_refs)
        ):
            return f"exclusive_goal_scope_work_graph:{exclusive.goal_id}"
        return None

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

    async def dispatch_once(
        self,
        binding_id: str,
        *,
        scope: TenantScope,
        now: float | None = None,
    ) -> GoalContinuationDispatchResult:
        binding = self.bindings.get(binding_id, scope=scope)
        policy_reason = self._continuation_policy_reason(binding)
        if policy_reason is not None:
            return GoalContinuationDispatchResult(
                binding_id=binding_id,
                outcome="not_dispatched",
                reason=policy_reason,
            )
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
            terminal = attempt >= self.max_recovery_attempts
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

    async def observe_runtime_usage(self, record) -> tuple[GoalContinuationDispatchResult, ...]:
        if record.terminal_outcome == RuntimeTerminalOutcome.UNKNOWN:
            return ()
        scope = TenantScope(
            organization_id=record.organization_id,
            workspace_id=record.workspace_id,
        )
        candidates = [
            item
            for item in self.bindings.list_all(scope=scope)
            if item.agent_session_id == record.agent_session_id
            and item.status
            not in {
                GoalExecutionBindingStatus.COMPLETED,
                GoalExecutionBindingStatus.CANCELLED,
                GoalExecutionBindingStatus.BLOCKED,
            }
            and (
                not item.last_turn_id
                or not record.provider_native_turn_id
                or item.last_turn_id == record.provider_native_turn_id
            )
        ]
        results: list[GoalContinuationDispatchResult] = []
        for binding in candidates:
            current = self.bindings.get(binding.id, scope=scope)
            owner_id = current.lease_owner_id
            if record.terminal_outcome == RuntimeTerminalOutcome.SUCCEEDED:
                if owner_id is not None:
                    self.bindings.release_continuation(
                        current.id,
                        scope=scope,
                        owner_id=owner_id,
                        status=GoalExecutionBindingStatus.IDLE,
                        reason="provider runtime turn completed",
                    )
                else:
                    self.bindings.update(
                        current.id,
                        GoalExecutionBindingUpdate(
                            status=GoalExecutionBindingStatus.IDLE,
                            reason="provider runtime turn completed after lease expiry",
                        ),
                        scope=scope,
                        actor_id=self.owner_id,
                    )
                results.append(
                    await self.dispatch_once(current.id, scope=scope)
                )
                continue

            if owner_id is None:
                claimed = self.bindings.claim_continuation(
                    current.id,
                    scope=scope,
                    owner_id=self.owner_id,
                    lease_seconds=self.lease_seconds,
                )
                if claimed is None:
                    continue
                current = claimed
                owner_id = self.owner_id
            attempt = current.recovery_attempts + 1
            terminal = attempt >= self.max_recovery_attempts
            retry_after = (
                None
                if terminal
                else self.retry_base_seconds * (2 ** max(0, attempt - 1))
            )
            self.bindings.release_continuation(
                current.id,
                scope=scope,
                owner_id=owner_id,
                status=(
                    GoalExecutionBindingStatus.BLOCKED
                    if terminal
                    else GoalExecutionBindingStatus.FAILED
                ),
                reason=(
                    "provider runtime terminal failure exhausted recovery budget"
                    if terminal
                    else "provider runtime terminal failure"
                ),
                retry_after_seconds=retry_after,
                increment_recovery=True,
            )
            results.append(
                GoalContinuationDispatchResult(
                    binding_id=current.id,
                    outcome="blocked" if terminal else "retry_scheduled",
                    retry_after_seconds=retry_after,
                    reason=record.terminal_outcome.value,
                )
            )
        return tuple(results)

    async def recover_due(
        self,
        *,
        now: float | None = None,
        max_dispatches: int = 100,
    ) -> tuple[GoalContinuationDispatchResult, ...]:
        current_time = time.time() if now is None else float(now)
        results: list[GoalContinuationDispatchResult] = []
        rows = sorted(
            self.bindings.store.load().bindings,
            key=lambda item: (item.updated_at, item.id),
        )
        for binding in rows:
            if len(results) >= max(0, int(max_dispatches)):
                break
            if binding.status in {
                GoalExecutionBindingStatus.COMPLETED,
                GoalExecutionBindingStatus.CANCELLED,
                GoalExecutionBindingStatus.BLOCKED,
            }:
                continue
            if (
                binding.retry_not_before_at is not None
                and binding.retry_not_before_at > current_time
            ):
                continue
            if (
                binding.lease_owner_id is not None
                and binding.lease_expires_at is not None
                and binding.lease_expires_at > current_time
            ):
                continue
            scope = TenantScope(
                organization_id=binding.organization_id,
                workspace_id=binding.workspace_id,
            )
            if binding.status == GoalExecutionBindingStatus.ACTIVE:
                try:
                    session = self.agent_sessions.get(
                        binding.agent_session_id,
                        self._actor(scope),
                    )
                except Exception:
                    session = None
                if session is not None and session.status == AgentSessionStatus.RUNNING:
                    continue
            results.append(
                await self.dispatch_once(
                    binding.id,
                    scope=scope,
                    now=current_time,
                )
            )
        return tuple(results)

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
        owner_id = binding.lease_owner_id
        if owner_id is None:
            return binding
        return self.bindings.release_continuation(
            binding_id,
            scope=scope,
            owner_id=owner_id,
            status=terminal_status or GoalExecutionBindingStatus.IDLE,
            reason=reason,
            now=now,
        )
