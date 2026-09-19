from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from codex_web.action_intents import ActionDecisionOutcome, ActionIntentStatus
from codex_web.approval_requests import ApprovalRequestStatus
from codex_web.autonomy import (
    AutonomyControl,
    AutonomyControlUpdate,
    AutonomyCycleOutcome,
    AutonomyCycleRecord,
    AutonomyDeadLetter,
    AutonomyObservation,
    AutonomyReasoningResult,
)
from codex_web.autonomy_policy import (
    AutonomyCycleBudgetUsage,
    AutonomyLevel,
)
from codex_web.compatibility import CanonicalEventEnvelope
from codex_web.identity import AuthenticationActor
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.autonomy_policy import AutonomyPolicyService
from codex_web.storage.autonomy import AutonomyStateStore


Reasoner = Callable[
    [CanonicalEventEnvelope, AutonomyObservation, AutonomyCycleRecord],
    Awaitable[AutonomyReasoningResult],
]


class AutonomyController:
    """Bounded observation -> reasoning -> ActionIntent control plane."""

    def __init__(
        self,
        store: AutonomyStateStore,
        *,
        action_intents: ActionIntentService | None = None,
        policy: AutonomyPolicyService | None = None,
    ) -> None:
        self.store = store
        self.action_intents = action_intents
        self.policy = policy

    def status(self) -> dict[str, Any]:
        state = self.store.load()
        return {
            "control": state.control.model_dump(mode="json"),
            "recent_cycles": [
                item.model_dump(mode="json") for item in reversed(state.cycles[-100:])
            ],
            "dead_letters": [
                item.model_dump(mode="json")
                for item in reversed(state.dead_letters[-100:])
            ],
            "updated_at": state.updated_at,
            "updated_by": state.updated_by,
        }

    def update_control(
        self,
        payload: AutonomyControlUpdate,
        *,
        actor_id: str,
    ) -> AutonomyControl:
        current = self.store.load().control
        updates = {
            key: value
            for key, value in payload.model_dump(mode="python").items()
            if value is not None
        }
        control = AutonomyControl.model_validate(
            {**current.model_dump(mode="python"), **updates}
        )
        self.store.set_control(control, actor_id=actor_id)
        return control

    def pause(self, *, actor_id: str) -> AutonomyControl:
        return self.update_control(
            AutonomyControlUpdate(mode="paused"),
            actor_id=actor_id,
        )

    def resume(self, *, actor_id: str) -> AutonomyControl:
        return self.update_control(
            AutonomyControlUpdate(mode="active"),
            actor_id=actor_id,
        )

    def kill(self, *, actor_id: str) -> AutonomyControl:
        return self.update_control(
            AutonomyControlUpdate(mode="killed"),
            actor_id=actor_id,
        )

    @staticmethod
    def _cycle(
        event: CanonicalEventEnvelope,
        *,
        cycle_key: str,
        observation: AutonomyObservation,
        depth: int,
        outcome: AutonomyCycleOutcome,
        reason: str,
        reasoning_invoked: bool = False,
        reasoning_attempts: int = 0,
        action_count: int = 0,
        action_intent_ids: tuple[str, ...] = (),
        approval_request_ids: tuple[str, ...] = (),
        autonomy_level: AutonomyLevel | None = None,
        policy_fingerprint: str | None = None,
        break_glass_grant_id: str | None = None,
        budget_usage: AutonomyCycleBudgetUsage | None = None,
        started_at: float | None = None,
        last_error: str | None = None,
    ) -> AutonomyCycleRecord:
        now = time.time()
        return AutonomyCycleRecord(
            cycle_key=cycle_key,
            event_id=event.event_id,
            event_type=event.event_type,
            source=event.source,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            organization_id=event.tenant_id,
            workspace_id=event.workspace_id,
            recursion_depth=depth,
            reasoning_score=observation.reasoning_score,
            reasoning_invoked=reasoning_invoked,
            reasoning_attempts=reasoning_attempts,
            action_count=action_count,
            action_intent_ids=action_intent_ids,
            approval_request_ids=approval_request_ids,
            autonomy_level=autonomy_level,
            policy_fingerprint=policy_fingerprint,
            break_glass_grant_id=break_glass_grant_id,
            budget_usage=budget_usage or AutonomyCycleBudgetUsage(),
            outcome=outcome,
            reason=reason,
            started_at=started_at or now,
            completed_at=now,
            last_error=last_error,
        )

    def _existing(self, cycle_key: str, event_id: str) -> AutonomyCycleRecord | None:
        return next(
            (
                item
                for item in reversed(self.store.load().cycles)
                if item.cycle_key == cycle_key and item.event_id == event_id
            ),
            None,
        )

    def _cooldown_active(
        self,
        event: CanonicalEventEnvelope,
        *,
        cycle_key: str,
        control: AutonomyControl,
        now: float,
    ) -> bool:
        if control.cooldown_seconds <= 0:
            return False
        return any(
            item.cycle_key == cycle_key
            and item.event_type == event.event_type
            and item.reasoning_invoked
            and now - item.completed_at < control.cooldown_seconds
            for item in reversed(self.store.load().cycles[-500:])
        )

    def _record_dead_letter(
        self,
        cycle: AutonomyCycleRecord,
        *,
        reason: str,
        error: str | None = None,
    ) -> None:
        self.store.append_dead_letter(
            AutonomyDeadLetter(
                cycle_id=cycle.id,
                cycle_key=cycle.cycle_key,
                event_id=cycle.event_id,
                event_type=cycle.event_type,
                reason=reason,
                attempts=cycle.reasoning_attempts,
                error=error,
            )
        )

    async def process(
        self,
        event: CanonicalEventEnvelope,
        observation: AutonomyObservation,
        *,
        cycle_key: str | None = None,
        depth: int = 0,
        reasoner: Reasoner | None = None,
        actor: AuthenticationActor | None = None,
    ) -> AutonomyCycleRecord:
        key = str(cycle_key or event.event_id).strip()
        if not key:
            raise ValueError("autonomy cycle key must not be empty")

        existing = self._existing(key, event.event_id)
        if existing is not None:
            return existing

        control = self.store.load().control
        started_at = time.time()
        project_value = event.payload.get("project_id") if isinstance(event.payload, dict) else None
        event_project_id = (
            project_value.strip()
            if isinstance(project_value, str) and project_value.strip()
            else None
        )
        event_level = control.policy.level
        event_policy_fingerprint = control.policy.fingerprint()
        event_budget = control.policy.budget
        if self.policy is not None and actor is not None:
            effective_event_policy, _event_roles = self.policy.effective(
                actor=actor,
                project_id=event_project_id,
                action_id=None,
                now=started_at,
            )
            event_level = effective_event_policy.level
            event_policy_fingerprint = effective_event_policy.policy_fingerprint
            event_budget = effective_event_policy.budget

        if control.mode != "active":
            cycle = self._cycle(
                event,
                cycle_key=key,
                observation=observation,
                depth=depth,
                outcome=AutonomyCycleOutcome.SKIPPED,
                reason=f"autonomy_{control.mode.value}",
                started_at=started_at,
            )
            self.store.append_cycle(cycle)
            return cycle

        if depth > control.max_recursion_depth:
            cycle = self._cycle(
                event,
                cycle_key=key,
                observation=observation,
                depth=depth,
                outcome=AutonomyCycleOutcome.FAILED,
                reason="maximum_recursion_depth_exceeded",
                started_at=started_at,
            )
            self.store.append_cycle(cycle)
            self._record_dead_letter(cycle, reason=cycle.reason)
            return cycle

        if observation.deterministic_resolved:
            cycle = self._cycle(
                event,
                cycle_key=key,
                observation=observation,
                depth=depth,
                outcome=AutonomyCycleOutcome.DETERMINISTIC,
                reason=observation.reason,
                started_at=started_at,
            )
            self.store.append_cycle(cycle)
            return cycle

        if event_level == AutonomyLevel.OBSERVE:
            cycle = self._cycle(
                event,
                cycle_key=key,
                observation=observation,
                depth=depth,
                outcome=AutonomyCycleOutcome.SKIPPED,
                reason="autonomy_level_observe",
                autonomy_level=event_level,
                policy_fingerprint=event_policy_fingerprint,
                started_at=started_at,
            )
            self.store.append_cycle(cycle)
            return cycle

        if (
            control.trigger_event_types
            and event.event_type not in set(control.trigger_event_types)
        ):
            cycle = self._cycle(
                event,
                cycle_key=key,
                observation=observation,
                depth=depth,
                outcome=AutonomyCycleOutcome.SKIPPED,
                reason="event_type_not_enabled_for_reasoning",
                started_at=started_at,
            )
            self.store.append_cycle(cycle)
            return cycle

        if observation.reasoning_score < control.reasoning_threshold:
            cycle = self._cycle(
                event,
                cycle_key=key,
                observation=observation,
                depth=depth,
                outcome=AutonomyCycleOutcome.SKIPPED,
                reason="reasoning_threshold_not_met",
                started_at=started_at,
            )
            self.store.append_cycle(cycle)
            return cycle

        if self._cooldown_active(event, cycle_key=key, control=control, now=started_at):
            cycle = self._cycle(
                event,
                cycle_key=key,
                observation=observation,
                depth=depth,
                outcome=AutonomyCycleOutcome.SKIPPED,
                reason="reasoning_cooldown_active",
                started_at=started_at,
            )
            self.store.append_cycle(cycle)
            return cycle

        if control.dry_run or control.simulation:
            cycle = self._cycle(
                event,
                cycle_key=key,
                observation=observation,
                depth=depth,
                outcome=(
                    AutonomyCycleOutcome.SIMULATED
                    if control.simulation
                    else AutonomyCycleOutcome.DRY_RUN
                ),
                reason="reasoning_would_run",
                started_at=started_at,
            )
            self.store.append_cycle(cycle)
            return cycle

        if reasoner is None:
            cycle = self._cycle(
                event,
                cycle_key=key,
                observation=observation,
                depth=depth,
                outcome=AutonomyCycleOutcome.FAILED,
                reason="reasoner_unconfigured",
                started_at=started_at,
            )
            self.store.append_cycle(cycle)
            self._record_dead_letter(cycle, reason=cycle.reason)
            return cycle

        attempts = 0
        result: AutonomyReasoningResult | None = None
        error: Exception | None = None
        while attempts < control.max_reasoning_attempts:
            attempts += 1
            provisional = self._cycle(
                event,
                cycle_key=key,
                observation=observation,
                depth=depth,
                outcome=AutonomyCycleOutcome.SKIPPED,
                reason="reasoning_in_progress",
                reasoning_invoked=True,
                reasoning_attempts=attempts,
                started_at=started_at,
            )
            try:
                result = await reasoner(event, observation, provisional)
                error = None
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = exc
                if attempts < control.max_reasoning_attempts:
                    delay = min(
                        60.0,
                        control.reasoning_backoff_seconds * (2 ** (attempts - 1)),
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)

        if result is None:
            cycle = self._cycle(
                event,
                cycle_key=key,
                observation=observation,
                depth=depth,
                outcome=AutonomyCycleOutcome.FAILED,
                reason="reasoning_attempts_exhausted",
                reasoning_invoked=True,
                reasoning_attempts=attempts,
                started_at=started_at,
                last_error=str(error)[:1000] if error else None,
            )
            self.store.append_cycle(cycle)
            self._record_dead_letter(
                cycle,
                reason=cycle.reason,
                error=cycle.last_error,
            )
            return cycle

        if len(result.actions) > control.max_actions_per_cycle:
            cycle = self._cycle(
                event,
                cycle_key=key,
                observation=observation,
                depth=depth,
                outcome=AutonomyCycleOutcome.FAILED,
                reason="maximum_actions_per_cycle_exceeded",
                reasoning_invoked=True,
                reasoning_attempts=attempts,
                action_count=len(result.actions),
                started_at=started_at,
            )
            self.store.append_cycle(cycle)
            self._record_dead_letter(cycle, reason=cycle.reason)
            return cycle

        if result.actions and (self.action_intents is None or actor is None):
            cycle = self._cycle(
                event,
                cycle_key=key,
                observation=observation,
                depth=depth,
                outcome=AutonomyCycleOutcome.FAILED,
                reason="action_intent_boundary_unavailable",
                reasoning_invoked=True,
                reasoning_attempts=attempts,
                action_count=len(result.actions),
                started_at=started_at,
            )
            self.store.append_cycle(cycle)
            self._record_dead_letter(cycle, reason=cycle.reason)
            return cycle

        intents = []
        for action in result.actions:
            # ActionIntentService.create performs canonical authority, policy,
            # tenant and security evaluation before any worker may execute.
            intents.append(self.action_intents.create(action, actor=actor))

        denied = any(
            getattr(item.status, "value", item.status) == ActionIntentStatus.CANCELLED.value
            for item in intents
        )
        cycle = self._cycle(
            event,
            cycle_key=key,
            observation=observation,
            depth=depth,
            outcome=(
                AutonomyCycleOutcome.BLOCKED
                if denied
                else AutonomyCycleOutcome.COMPLETED
            ),
            reason=(
                "one_or_more_actions_denied_by_canonical_authority"
                if denied
                else result.summary or "reasoning_completed"
            ),
            reasoning_invoked=True,
            reasoning_attempts=attempts,
            action_count=len(result.actions),
            action_intent_ids=tuple(item.id for item in intents),
            started_at=started_at,
        )
        self.store.append_cycle(cycle)
        return cycle
