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
    AutonomyExclusiveGoalScope,
    AutonomyObservation,
    AutonomyPauseScope,
    AutonomyReasoningResult,
    AutonomyScopedPause,
    AutonomyScopedPauseCreate,
)
from codex_web.autonomy_policy import (
    AutonomyCycleBudgetUsage,
    AutonomyLevel,
)
from codex_web.compatibility import CanonicalEventEnvelope
from codex_web.identity import AuthenticationActor
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.autonomy_policy import AutonomyPolicyService
from codex_web.services.autonomy_audit import AutonomyAuditService
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
        audit: AutonomyAuditService | None = None,
    ) -> None:
        self.store = store
        self.action_intents = action_intents
        self.policy = policy
        self.audit = audit

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
            "break_glass_grants": [
                item.model_dump(mode="json")
                for item in reversed(state.break_glass_grants[-100:])
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

    def add_scoped_pause(
        self,
        payload: AutonomyScopedPauseCreate,
        *,
        actor_id: str,
    ) -> AutonomyScopedPause:
        current = self.store.load().control
        now = time.time()
        active = tuple(
            item
            for item in current.scoped_pauses
            if item.active(now)
            and not (
                item.scope == payload.scope
                and item.scope_id == payload.scope_id
            )
        )
        pause = AutonomyScopedPause(
            scope=payload.scope,
            scope_id=payload.scope_id,
            reason=payload.reason,
            created_by=actor_id,
            created_at=now,
            expires_at=payload.expires_at,
        )
        control = current.model_copy(
            update={"scoped_pauses": (*active, pause)}
        )
        self.store.set_control(control, actor_id=actor_id)
        return pause

    def remove_scoped_pause(
        self,
        pause_id: str,
        *,
        actor_id: str,
    ) -> bool:
        current = self.store.load().control
        remaining = tuple(
            item for item in current.scoped_pauses if item.id != pause_id
        )
        if len(remaining) == len(current.scoped_pauses):
            return False
        self.store.set_control(
            current.model_copy(update={"scoped_pauses": remaining}),
            actor_id=actor_id,
        )
        return True

    @staticmethod
    def _scoped_pause_reason(
        control: AutonomyControl,
        *,
        actor: AuthenticationActor | None,
        project_id: str | None,
        resource_ids: tuple[str, ...] = (),
        now: float | None = None,
    ) -> str | None:
        current = time.time() if now is None else float(now)
        identity_id = getattr(actor, "identity_id", None)
        resources = set(resource_ids)
        for item in control.scoped_pauses:
            if not item.active(current):
                continue
            if item.scope == AutonomyPauseScope.IDENTITY and identity_id == item.scope_id:
                return f"autonomy_scoped_pause:identity:{item.scope_id}"
            if item.scope == AutonomyPauseScope.PROJECT and project_id == item.scope_id:
                return f"autonomy_scoped_pause:project:{item.scope_id}"
            if item.scope == AutonomyPauseScope.RESOURCE and item.scope_id in resources:
                return f"autonomy_scoped_pause:resource:{item.scope_id}"
        return None

    def set_exclusive_goal_scope(
        self,
        scope: AutonomyExclusiveGoalScope,
        *,
        actor_id: str,
    ) -> AutonomyControl:
        current = self.store.load().control
        control = current.model_copy(update={"exclusive_goal_scope": scope})
        self.store.set_control(control, actor_id=actor_id)
        return control

    def clear_exclusive_goal_scope(
        self,
        *,
        actor_id: str,
    ) -> AutonomyControl:
        current = self.store.load().control
        control = current.model_copy(update={"exclusive_goal_scope": None})
        self.store.set_control(control, actor_id=actor_id)
        return control

    @staticmethod
    def _exclusive_goal_scope_reason(
        control: AutonomyControl,
        event: CanonicalEventEnvelope,
    ) -> str | None:
        scope = control.exclusive_goal_scope
        if scope is None:
            return None
        payload = event.payload if isinstance(event.payload, dict) else {}
        if payload.get("goal_id") != scope.goal_id:
            return f"autonomy_exclusive_goal_scope:goal:{scope.goal_id}"
        if payload.get("project_id") != scope.project_id:
            return f"autonomy_exclusive_goal_scope:project:{scope.project_id}"
        if not scope.root_work_item_refs:
            return None
        refs: list[str] = []
        raw_refs = payload.get("work_item_refs")
        if isinstance(raw_refs, (list, tuple)):
            refs.extend(str(item) for item in raw_refs if item)
        for key in ("work_item_ref", "ref"):
            value = payload.get(key)
            if value:
                refs.append(str(value))
        if not refs:
            return f"autonomy_exclusive_goal_scope:work_graph:{scope.goal_id}"
        allowed = set(scope.root_work_item_refs)
        if any(ref not in allowed for ref in refs):
            return f"autonomy_exclusive_goal_scope:work_graph:{scope.goal_id}"
        return None

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

    def _persist_cycle(
        self,
        cycle: AutonomyCycleRecord,
        event: CanonicalEventEnvelope,
        actor: AuthenticationActor | None,
        *,
        reasoning_result: AutonomyReasoningResult | None = None,
    ) -> AutonomyCycleRecord:
        if reasoning_result is not None:
            cycle.model_provider_id = reasoning_result.model_provider_id
            cycle.model_id = reasoning_result.model_id
            cycle.model_revision = reasoning_result.model_revision
            cycle.prompt_template_id = reasoning_result.prompt_template_id
            cycle.model_routing_reason = reasoning_result.model_routing_reason
            cycle.model_invocation_ids = reasoning_result.model_invocation_ids
        self.store.append_cycle(cycle)
        if self.audit is not None:
            self.audit.record_cycle(
                cycle,
                event,
                actor=actor,
            )
        return cycle

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

        event_resource_ids = ()
        if isinstance(event.payload, dict):
            raw_resources = event.payload.get("resource_ids")
            if isinstance(raw_resources, (list, tuple)):
                event_resource_ids = tuple(str(item) for item in raw_resources if item)
            elif event.payload.get("resource_id"):
                event_resource_ids = (str(event.payload.get("resource_id")),)
        scoped_pause_reason = self._scoped_pause_reason(
            control,
            actor=actor,
            project_id=event_project_id,
            resource_ids=event_resource_ids,
            now=started_at,
        )
        if scoped_pause_reason is not None:
            cycle = self._cycle(
                event,
                cycle_key=key,
                observation=observation,
                depth=depth,
                outcome=AutonomyCycleOutcome.SKIPPED,
                reason=scoped_pause_reason,
                started_at=started_at,
            )
            self._persist_cycle(cycle, event, actor)
            return cycle

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
            self._persist_cycle(cycle, event, actor)
            return cycle

        exclusive_scope_reason = self._exclusive_goal_scope_reason(
            control,
            event,
        )
        if exclusive_scope_reason is not None:
            cycle = self._cycle(
                event,
                cycle_key=key,
                observation=observation,
                depth=depth,
                outcome=AutonomyCycleOutcome.SKIPPED,
                reason=exclusive_scope_reason,
                started_at=started_at,
            )
            self._persist_cycle(cycle, event, actor)
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
            self._persist_cycle(cycle, event, actor)
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
            self._persist_cycle(cycle, event, actor)
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
            self._persist_cycle(cycle, event, actor)
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
            self._persist_cycle(cycle, event, actor)
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
            self._persist_cycle(cycle, event, actor)
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
            self._persist_cycle(cycle, event, actor)
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
            self._persist_cycle(cycle, event, actor)
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
            self._persist_cycle(cycle, event, actor)
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
            self._persist_cycle(cycle, event, actor)
            self._record_dead_letter(
                cycle,
                reason=cycle.reason,
                error=cycle.last_error,
            )
            return cycle

        base_usage = AutonomyCycleBudgetUsage(
            actions=len(result.actions),
            model_tokens=result.model_tokens,
            model_cost_usd=result.model_cost_usd,
        )
        if self.policy is not None:
            violations = self.policy.budget_violations(
                (),
                base_usage,
                hard_action_limit=control.max_actions_per_cycle,
                default_limits=event_budget,
            )
        else:
            violations = (
                ("maximum_actions_per_cycle_exceeded",)
                if len(result.actions) > control.max_actions_per_cycle
                else ()
            )
        if violations:
            cycle = self._cycle(
                event,
                cycle_key=key,
                observation=observation,
                depth=depth,
                outcome=AutonomyCycleOutcome.FAILED,
                reason=violations[0],
                reasoning_invoked=True,
                reasoning_attempts=attempts,
                action_count=len(result.actions),
                autonomy_level=event_level,
                policy_fingerprint=event_policy_fingerprint,
                budget_usage=base_usage,
                started_at=started_at,
            )
            self._persist_cycle(cycle, event, actor, reasoning_result=result)
            self._record_dead_letter(cycle, reason=cycle.reason)
            return cycle

        if event_level == AutonomyLevel.RECOMMEND:
            cycle = self._cycle(
                event,
                cycle_key=key,
                observation=observation,
                depth=depth,
                outcome=AutonomyCycleOutcome.RECOMMENDED,
                reason=result.summary or "recommendation_completed",
                reasoning_invoked=True,
                reasoning_attempts=attempts,
                action_count=len(result.actions),
                autonomy_level=event_level,
                policy_fingerprint=event_policy_fingerprint,
                budget_usage=base_usage,
                started_at=started_at,
            )
            self._persist_cycle(cycle, event, actor, reasoning_result=result)
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
                autonomy_level=event_level,
                policy_fingerprint=event_policy_fingerprint,
                budget_usage=base_usage,
                started_at=started_at,
            )
            self._persist_cycle(cycle, event, actor, reasoning_result=result)
            self._record_dead_letter(cycle, reason=cycle.reason)
            return cycle

        if event_level == AutonomyLevel.PREPARE:
            if self.action_intents is None or actor is None:
                cycle = self._cycle(
                    event,
                    cycle_key=key,
                    observation=observation,
                    depth=depth,
                    outcome=AutonomyCycleOutcome.FAILED,
                    reason="action_prepare_boundary_unavailable",
                    reasoning_invoked=True,
                    reasoning_attempts=attempts,
                    action_count=len(result.actions),
                    autonomy_level=event_level,
                    policy_fingerprint=event_policy_fingerprint,
                    budget_usage=base_usage,
                    started_at=started_at,
                )
                self._persist_cycle(cycle, event, actor, reasoning_result=result)
                self._record_dead_letter(cycle, reason=cycle.reason)
                return cycle
            try:
                for action in result.actions:
                    await self.action_intents.execution.prepare(
                        action.binding_id,
                        action.request.model_copy(update={"dry_run": True})
                        if action.request.dry_run
                        else action.request,
                        actor=actor,
                    )
            except Exception as exc:
                cycle = self._cycle(
                    event,
                    cycle_key=key,
                    observation=observation,
                    depth=depth,
                    outcome=AutonomyCycleOutcome.BLOCKED,
                    reason=f"action_preflight_failed:{type(exc).__name__}",
                    reasoning_invoked=True,
                    reasoning_attempts=attempts,
                    action_count=len(result.actions),
                    autonomy_level=event_level,
                    policy_fingerprint=event_policy_fingerprint,
                    budget_usage=base_usage,
                    started_at=started_at,
                    last_error=str(exc)[:1000],
                )
                self._persist_cycle(cycle, event, actor, reasoning_result=result)
                return cycle
            cycle = self._cycle(
                event,
                cycle_key=key,
                observation=observation,
                depth=depth,
                outcome=AutonomyCycleOutcome.PREPARED,
                reason=result.summary or "action_preflight_completed",
                reasoning_invoked=True,
                reasoning_attempts=attempts,
                action_count=len(result.actions),
                autonomy_level=event_level,
                policy_fingerprint=event_policy_fingerprint,
                budget_usage=base_usage,
                started_at=started_at,
            )
            self._persist_cycle(cycle, event, actor, reasoning_result=result)
            return cycle

        # Standalone/legacy controller usage keeps the historical ActionIntent
        # boundary. The composed application always supplies AutonomyPolicyService.
        if self.policy is None:
            intents = []
            for action in result.actions:
                intents.append(self.action_intents.create(action, actor=actor))
            denied = any(
                getattr(item.status, "value", item.status)
                == ActionIntentStatus.CANCELLED.value
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
                autonomy_level=event_level,
                policy_fingerprint=event_policy_fingerprint,
                budget_usage=base_usage,
                started_at=started_at,
            )
            self._persist_cycle(cycle, event, actor, reasoning_result=result)
            return cycle

        decisions = []
        normalized_actions = []
        try:
            for action in result.actions:
                _binding, _provider, definition, request = (
                    self.action_intents.execution.resolve_contract(
                        action.binding_id,
                        action.request,
                        actor=actor,
                    )
                )
                decision = self.policy.evaluate_action(
                    definition,
                    request,
                    actor=actor,
                    now=started_at,
                )
                decisions.append(decision)
                normalized_actions.append(
                    action.model_copy(update={"request": request})
                )
        except Exception as exc:
            cycle = self._cycle(
                event,
                cycle_key=key,
                observation=observation,
                depth=depth,
                outcome=AutonomyCycleOutcome.BLOCKED,
                reason=f"autonomy_policy_evaluation_failed:{type(exc).__name__}",
                reasoning_invoked=True,
                reasoning_attempts=attempts,
                action_count=len(result.actions),
                autonomy_level=event_level,
                policy_fingerprint=event_policy_fingerprint,
                budget_usage=base_usage,
                started_at=started_at,
                last_error=str(exc)[:1000],
            )
            self._persist_cycle(cycle, event, actor, reasoning_result=result)
            return cycle

        decisions_tuple = tuple(decisions)
        usage = self.policy.cycle_budget_usage(decisions_tuple, result)
        violations = self.policy.budget_violations(
            decisions_tuple,
            usage,
            hard_action_limit=control.max_actions_per_cycle,
            default_limits=event_budget,
        )
        if violations:
            cycle = self._cycle(
                event,
                cycle_key=key,
                observation=observation,
                depth=depth,
                outcome=AutonomyCycleOutcome.BLOCKED,
                reason=violations[0],
                reasoning_invoked=True,
                reasoning_attempts=attempts,
                action_count=len(result.actions),
                autonomy_level=event_level,
                policy_fingerprint=event_policy_fingerprint,
                budget_usage=usage,
                started_at=started_at,
            )
            self._persist_cycle(cycle, event, actor, reasoning_result=result)
            return cycle

        denied_decisions = [
            item for item in decisions_tuple if not item.allowed
        ]
        if denied_decisions:
            denied = denied_decisions[0]
            cycle = self._cycle(
                event,
                cycle_key=key,
                observation=observation,
                depth=depth,
                outcome=AutonomyCycleOutcome.BLOCKED,
                reason=(
                    denied.reasons[-1]
                    if denied.reasons
                    else "autonomy_policy_denied_action"
                ),
                reasoning_invoked=True,
                reasoning_attempts=attempts,
                action_count=len(result.actions),
                autonomy_level=denied.effective_level,
                policy_fingerprint=denied.policy_fingerprint,
                break_glass_grant_id=denied.break_glass_grant_id,
                budget_usage=usage,
                started_at=started_at,
            )
            self._persist_cycle(cycle, event, actor, reasoning_result=result)
            return cycle

        approval_ids = []
        approvals = []
        for action, decision in zip(normalized_actions, decisions_tuple):
            approval = await self.policy.ensure_action_approval(
                action,
                decision,
                actor=actor,
            )
            approvals.append(approval)
            if approval is not None:
                approval_ids.append(approval.id)

        pending_approvals = [
            approval
            for approval in approvals
            if approval is not None
            and approval.status != ApprovalRequestStatus.APPROVED
        ]
        if pending_approvals:
            cycle = self._cycle(
                event,
                cycle_key=key,
                observation=observation,
                depth=depth,
                outcome=AutonomyCycleOutcome.BLOCKED,
                reason="canonical_approval_required",
                reasoning_invoked=True,
                reasoning_attempts=attempts,
                action_count=len(result.actions),
                approval_request_ids=tuple(approval_ids),
                autonomy_level=event_level,
                policy_fingerprint=event_policy_fingerprint,
                break_glass_grant_id=next(
                    (
                        item.break_glass_grant_id
                        for item in decisions_tuple
                        if item.break_glass_grant_id is not None
                    ),
                    None,
                ),
                budget_usage=usage,
                started_at=started_at,
            )
            self._persist_cycle(cycle, event, actor, reasoning_result=result)
            return cycle

        prepared_actions = []
        try:
            for action, decision in zip(normalized_actions, decisions_tuple):
                updated = action.model_copy(
                    update={
                        "policy_decision": self.policy.action_policy_snapshot(
                            decision
                        ),
                        "rollback_required": (
                            action.rollback_required
                            or decision.rollback_required
                        ),
                        "verification_required": (
                            True
                            if decision.verification_required
                            else action.verification_required
                        ),
                    }
                )
                if decision.preflight_required:
                    await self.action_intents.execution.prepare(
                        updated.binding_id,
                        updated.request,
                        actor=actor,
                    )
                prepared_actions.append(updated)
        except Exception as exc:
            cycle = self._cycle(
                event,
                cycle_key=key,
                observation=observation,
                depth=depth,
                outcome=AutonomyCycleOutcome.BLOCKED,
                reason=f"required_preflight_failed:{type(exc).__name__}",
                reasoning_invoked=True,
                reasoning_attempts=attempts,
                action_count=len(result.actions),
                approval_request_ids=tuple(approval_ids),
                autonomy_level=event_level,
                policy_fingerprint=event_policy_fingerprint,
                budget_usage=usage,
                started_at=started_at,
                last_error=str(exc)[:1000],
            )
            self._persist_cycle(cycle, event, actor, reasoning_result=result)
            return cycle

        # Consume exact approved targets before exposing executable intents to
        # workers. A later creation failure requires a new approval rather than
        # permitting an intent to race ahead of canonical approval consumption.
        for action, approval in zip(prepared_actions, approvals):
            if approval is not None:
                await self.policy.consume_action_approval(
                    approval,
                    action,
                    actor=actor,
                    cycle_id=f"{key}:{event.event_id}",
                )

        intents = [
            self.action_intents.create(action, actor=actor)
            for action in prepared_actions
        ]
        authority_denied = any(
            getattr(item.status, "value", item.status)
            == ActionIntentStatus.CANCELLED.value
            for item in intents
        )
        primary_decision = decisions_tuple[0] if decisions_tuple else None
        cycle = self._cycle(
            event,
            cycle_key=key,
            observation=observation,
            depth=depth,
            outcome=(
                AutonomyCycleOutcome.BLOCKED
                if authority_denied
                else AutonomyCycleOutcome.COMPLETED
            ),
            reason=(
                "one_or_more_actions_denied_by_canonical_authority"
                if authority_denied
                else result.summary or "reasoning_completed"
            ),
            reasoning_invoked=True,
            reasoning_attempts=attempts,
            action_count=len(result.actions),
            action_intent_ids=tuple(item.id for item in intents),
            approval_request_ids=tuple(approval_ids),
            autonomy_level=(
                primary_decision.effective_level
                if primary_decision is not None
                else event_level
            ),
            policy_fingerprint=(
                primary_decision.policy_fingerprint
                if primary_decision is not None
                else event_policy_fingerprint
            ),
            break_glass_grant_id=next(
                (
                    item.break_glass_grant_id
                    for item in decisions_tuple
                    if item.break_glass_grant_id is not None
                ),
                None,
            ),
            budget_usage=usage,
            started_at=started_at,
        )
        self._persist_cycle(cycle, event, actor, reasoning_result=result)
        return cycle

