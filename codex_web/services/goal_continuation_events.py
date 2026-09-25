from __future__ import annotations

from dataclasses import dataclass

from codex_web.agent_runtime import AgentRuntimeEvent
from codex_web.goal_execution_bindings import GoalExecutionBindingStatus
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
class GoalContinuationEventResult:
    outcome: str
    binding_id: str | None = None
    continuation: GoalContinuationDispatchResult | None = None
    reason: str | None = None


class GoalContinuationEventService:
    """Project provider terminal events back onto lease-owned Goal continuation."""

    _COMPLETED = {"turn/completed"}
    _FAILED = {"turn/failed"}
    _INTERRUPTED = {"turn/interrupted"}

    def __init__(
        self,
        bindings: GoalExecutionBindingService,
        agent_sessions: AgentSessionService,
        continuation: GoalContinuationService,
    ) -> None:
        self.bindings = bindings
        self.agent_sessions = agent_sessions
        self.continuation = continuation

    @staticmethod
    def _event_type(value: str) -> str:
        return str(value or "").strip().lower().replace(".", "/")

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

    def _binding_for_event(
        self,
        event: AgentRuntimeEvent,
        *,
        scope: TenantScope,
    ):
        native_session_id = event.provider_native_session_id
        if not native_session_id:
            return None
        session = self.agent_sessions.find_by_native_id(
            native_session_id,
            self._actor(scope),
        )
        if session is None:
            return None
        rows = [
            item
            for item in self.bindings.list_all(scope=scope)
            if item.agent_session_id == session.id
            and item.status
            not in {
                GoalExecutionBindingStatus.BLOCKED,
                GoalExecutionBindingStatus.COMPLETED,
                GoalExecutionBindingStatus.CANCELLED,
            }
        ]
        if event.provider_native_turn_id:
            rows = [
                item
                for item in rows
                if item.last_turn_id == event.provider_native_turn_id
            ]
        if len(rows) != 1:
            return None
        return rows[0]

    async def handle_event(
        self,
        event: AgentRuntimeEvent,
        *,
        scope: TenantScope,
        now: float | None = None,
    ) -> GoalContinuationEventResult:
        event_type = self._event_type(event.event_type)
        if event_type not in self._COMPLETED | self._FAILED | self._INTERRUPTED:
            return GoalContinuationEventResult(
                outcome="ignored",
                reason="non_terminal_event",
            )

        binding = self._binding_for_event(event, scope=scope)
        if binding is None:
            return GoalContinuationEventResult(
                outcome="ignored",
                reason="binding_not_found_or_ambiguous",
            )
        if binding.lease_owner_id != self.continuation.owner_id:
            return GoalContinuationEventResult(
                outcome="ignored",
                binding_id=binding.id,
                reason="continuation_lease_not_owned",
            )

        if event_type in self._COMPLETED:
            self.continuation.resolve_turn(
                binding.id,
                scope=scope,
                reason="provider turn completed",
                terminal_status=GoalExecutionBindingStatus.IDLE,
                now=now,
            )
            next_turn = await self.continuation.dispatch_once(
                binding.id,
                scope=scope,
                now=now,
            )
            return GoalContinuationEventResult(
                outcome="continued",
                binding_id=binding.id,
                continuation=next_turn,
            )

        if event_type in self._INTERRUPTED:
            self.continuation.resolve_turn(
                binding.id,
                scope=scope,
                reason="provider turn interrupted",
                terminal_status=GoalExecutionBindingStatus.IDLE,
                now=now,
            )
            return GoalContinuationEventResult(
                outcome="interrupted",
                binding_id=binding.id,
            )

        attempt = binding.recovery_attempts + 1
        terminal = attempt >= self.continuation.recovery_attempt_limit_for(
            binding,
            scope=scope,
        )
        retry_after = (
            None
            if terminal
            else self.continuation.retry_base_seconds * (2 ** max(0, attempt - 1))
        )
        self.bindings.release_continuation(
            binding.id,
            scope=scope,
            owner_id=self.continuation.owner_id,
            status=(
                GoalExecutionBindingStatus.BLOCKED
                if terminal
                else GoalExecutionBindingStatus.FAILED
            ),
            reason=(
                "provider turn failure exhausted continuation retry budget"
                if terminal
                else "provider turn failed"
            ),
            retry_after_seconds=retry_after,
            increment_recovery=True,
            now=now,
        )
        return GoalContinuationEventResult(
            outcome="blocked" if terminal else "retry_scheduled",
            binding_id=binding.id,
            reason=event_type,
        )
