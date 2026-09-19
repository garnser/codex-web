from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from codex_web.action_intents import (
    ActionIntent,
    ActionIntentCreate,
    ActionIntentReconcileRequest,
    ActionIntentRollbackRequest,
)
from codex_web.action_providers import ActionRequest
from codex_web.autonomy import AutonomyCycleOutcome, AutonomyObservation, AutonomyReasoningResult
from codex_web.canonical_events import CanonicalEventType
from codex_web.identity import AuthenticationActor
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.action_providers import ActionExecutionService
from codex_web.services.autonomy_controller import AutonomyController
from codex_web.services.canonical_events import CanonicalEventIngestionService


class AutonomyService:
    """Own autonomous work-item/watchdog cycle decisions outside core.py."""

    def __init__(
        self,
        host: Any,
        action_execution: ActionExecutionService | None = None,
        action_intents: ActionIntentService | None = None,
        *,
        controller: AutonomyController | None = None,
        canonical_events: CanonicalEventIngestionService | None = None,
    ) -> None:
        self.host = host
        self.action_execution = action_execution
        self.action_intents = action_intents
        self.controller = controller
        self.canonical_events = canonical_events

    def _actions(self) -> ActionExecutionService:
        if self.action_execution is None:
            raise RuntimeError("action execution service is not configured")
        return self.action_execution

    async def prepare_external_action(
        self,
        binding_id: str,
        request: ActionRequest,
        *,
        actor: AuthenticationActor,
    ):
        return await self._actions().prepare(binding_id, request, actor=actor)

    def _intent_service(self) -> ActionIntentService:
        if self.action_intents is None:
            raise RuntimeError("action intent service is not configured")
        return self.action_intents

    async def _bounded_reasoning_dispatch(
        self,
        binding: Any,
        text: str,
        source: str,
        *,
        cycle_key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Route watchdog reasoning through canonical event + autonomy controls."""

        if self.controller is None or self.canonical_events is None:
            return await self.host._dispatch_event_to_binding(binding, text, source)

        normalized = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        delivery = await self.canonical_events.ingest(
            event_type=CanonicalEventType.WORK_TRANSITION,
            source=f"autonomy-watchdog:{source}",
            idempotency_key=f"{cycle_key}:{digest}",
            payload=payload,
        )
        if not delivery.inserted:
            return {
                "ok": True,
                "ignored": True,
                "reason": "canonical_duplicate",
                "canonicalEventId": delivery.event.event_id,
            }

        dispatched: dict[str, Any] = {}

        async def reasoner(*_args) -> AutonomyReasoningResult:
            dispatched.update(
                await self.host._dispatch_event_to_binding(binding, text, source)
            )
            return AutonomyReasoningResult(
                summary=f"{source} dispatched through bounded autonomy"
            )

        cycle = await self.controller.process(
            delivery.event,
            AutonomyObservation(
                deterministic_resolved=False,
                reasoning_score=1.0,
                reason=f"{source} deterministic safeguards require agent reasoning",
            ),
            cycle_key=cycle_key,
            reasoner=reasoner,
        )
        if cycle.outcome != AutonomyCycleOutcome.COMPLETED:
            return {
                "ok": False,
                "ignored": True,
                "reason": f"autonomy_{cycle.outcome.value}",
                "autonomyReason": cycle.reason,
                "autonomyCycleId": cycle.id,
                "canonicalEventId": delivery.event.event_id,
            }
        return {
            **dispatched,
            "autonomyCycleId": cycle.id,
            "canonicalEventId": delivery.event.event_id,
        }

    async def execute_external_action(
        self,
        binding_id: str,
        request: ActionRequest,
        *,
        actor: AuthenticationActor,
    ) -> ActionIntent:
        """Compatibility name: queue durable intent instead of executing provider."""

        return self._intent_service().create(
            ActionIntentCreate(binding_id=binding_id, request=request),
            actor=actor,
        )

    async def verify_external_action(
        self,
        intent_id: str,
        *,
        actor: AuthenticationActor,
    ) -> ActionIntent:
        return await self._intent_service().reconcile(
            intent_id,
            ActionIntentReconcileRequest(retry_if_idempotent=False),
            actor=actor,
        )

    async def rollback_external_action(
        self,
        intent_id: str,
        *,
        actor: AuthenticationActor,
    ) -> ActionIntent:
        return await self._intent_service().rollback(
            intent_id,
            ActionIntentRollbackRequest(reason="autonomy rollback"),
            actor=actor,
        )

    async def run_owner_work_cycle(self) -> None:
        h = self.host
        settings = h._load_gitlab_routing_settings()
        if not settings.enabled:
            return
        states = h._load_work_item_states()
        for project_id, project_settings in settings.projects.items():
            if not project_settings.enabled:
                continue
            token = h._gitlab_token_for_project(project_id)
            group = h._gitlab_group_path(project_settings)
            if not token or not group:
                h._append_bot_event(
                    {
                        "type": "owner_work_watchdog_skipped",
                        "project_id": project_id,
                        "reason": "missing_gitlab_token_or_group",
                    }
                )
                continue
            for owner in h.OWNER_QUEUE_AGENTS:
                binding = h._binding_for_agent(
                    owner,
                    project_id,
                    preferred_conversation_id=h.HANDOFF_COORDINATION_CHANNEL,
                )
                if not binding:
                    continue
                issues = h._gitlab_group_issues(project_id, project_settings, labels=[f"owner::{owner}"])
                if not issues:
                    continue
                missing_state_refs = [
                    issue.get("references", {}).get("full", "")
                    for issue in issues
                    if issue.get("references", {}).get("full")
                    and issue.get("state") == "opened"
                    and issue.get("references", {}).get("full") not in states
                ]
                if not missing_state_refs:
                    continue
                binding = await h._replace_nonperforming_thread_if_needed(binding, "owner-work-watchdog")
                dispatch_key = f"owner-work:{project_id}:{binding.thread_id}:{owner}"
                if not h._watchdog_dispatch_allowed(dispatch_key):
                    continue
                h._release_stale_active_turn(binding.thread_id, "owner-work-watchdog")
                if h._thread_is_active(binding.thread_id) or h._thread_queue_depth(binding.thread_id):
                    continue
                if h._thread_recently_active(binding.thread_id):
                    continue
                refs = ", ".join(missing_state_refs[:8])
                extra = f", plus {len(missing_state_refs) - 8} more" if len(missing_state_refs) > 8 else ""
                agent_name = h._binding_prefix(binding) or binding.thread_name or owner
                text = (
                    f"{agent_name}: bounded event-path fallback triggered. "
                    f"GitLab shows owned open items with no canonical codex-web state yet: {refs}{extra}. "
                    "Reconcile those items through codex-web now, post the exact item and next action in the "
                    "configured handoff coordination channel, and keep working until completion or one concrete escalation."
                )
                h._record_watchdog_dispatch(dispatch_key)
                result = await self._bounded_reasoning_dispatch(
                    binding,
                    text,
                    "owner-work-watchdog",
                    cycle_key=dispatch_key,
                    payload={
                        "project_id": project_id,
                        "agent": owner,
                        "missing_state_refs": missing_state_refs[:8],
                    },
                )
                h._append_bot_event(
                    {
                        "type": "owner_work_watchdog_dispatched",
                        "project_id": project_id,
                        "agent": owner,
                        "thread_id": binding.thread_id,
                        "issue_count": len(missing_state_refs),
                        "result": result,
                    }
                )

    async def run_release_gate_cycle(self) -> None:
        h = self.host
        settings = h._load_gitlab_routing_settings()
        if not settings.enabled:
            return
        states = h._load_work_item_states()
        now = time.time()
        for project_id, project_settings in settings.projects.items():
            if not project_settings.enabled:
                continue
            binding = h._binding_for_agent("release manager", project_id)
            if not binding:
                continue
            p1_issues = h._gitlab_group_issues(project_id, project_settings, labels=["priority::P1"])
            if p1_issues:
                continue
            stale_release_states = [
                state
                for state in states.values()
                if state.project_id == project_id
                and h._coerce_owner(state.current_owner or state.next_owner) == "release manager"
                and state.current_stage in {"ready_for_validation", "validation_running", "ready_to_close"}
                and (now - h._owner_activity_timestamp(state)) >= h._release_validation_sla_seconds()
            ]
            if not stale_release_states:
                continue
            binding = await h._replace_nonperforming_thread_if_needed(binding, "release-gate-watchdog")
            dispatch_key = f"release-gate:{project_id}:{binding.thread_id}"
            if not h._watchdog_dispatch_allowed(dispatch_key):
                continue
            h._release_stale_active_turn(binding.thread_id, "release-gate-watchdog")
            if h._thread_is_active(binding.thread_id) or h._thread_queue_depth(binding.thread_id):
                continue
            if h._thread_recently_active(binding.thread_id):
                continue
            refs = ", ".join(state.ref for state in stale_release_states[:8])
            extra = f", plus {len(stale_release_states) - 8} more" if len(stale_release_states) > 8 else ""
            text = (
                "Release Manager: bounded release-lane fallback triggered. "
                f"Open P1 count is zero, but release-ready items are stale: {refs}{extra}. "
                "Resume the exact deploy/verify/E2E next action or state one exact blocker in the configured "
                "handoff coordination channel. Reconcile the affected work item in codex-web before ending the turn."
            )
            h._record_watchdog_dispatch(dispatch_key)
            result = await self._bounded_reasoning_dispatch(
                binding,
                text,
                "release-gate-watchdog",
                cycle_key=dispatch_key,
                payload={
                    "project_id": project_id,
                    "stale_refs": [state.ref for state in stale_release_states[:8]],
                },
            )
            h._append_bot_event(
                {
                    "type": "release_gate_watchdog_dispatched",
                    "project_id": project_id,
                    "thread_id": binding.thread_id,
                    "result": result,
                }
            )

    async def run_orchestrator_cycle(self) -> None:
        h = self.host
        settings = h._load_gitlab_routing_settings()
        if not settings.enabled:
            return
        for project_id, project_settings in settings.projects.items():
            if not project_settings.enabled:
                continue
            binding = h._orchestrator_binding(project_id)
            if not binding:
                continue
            items = h._orchestrator_watchdog_candidates(project_id)
            if not items:
                continue
            binding = await h._replace_nonperforming_thread_if_needed(binding, "orchestrator-watchdog")
            if h._thread_queue_depth(binding.thread_id) >= 3:
                continue
            dispatch_key = f"orchestrator-watchdog:{project_id}:{binding.thread_id}"
            if not h._watchdog_dispatch_allowed(dispatch_key):
                continue
            h._record_watchdog_dispatch(dispatch_key)
            result = await self._bounded_reasoning_dispatch(
                binding,
                h._format_orchestrator_watchdog_prompt(project_id, items),
                "orchestrator-watchdog",
                cycle_key=dispatch_key,
                payload={
                    "project_id": project_id,
                    "item_refs": [state.ref for _, state, _ in items[:8]],
                },
            )
            h._append_bot_event(
                {
                    "type": "orchestrator_watchdog_dispatched",
                    "project_id": project_id,
                    "thread_id": binding.thread_id,
                    "item_refs": [state.ref for _, state, _ in items[:8]],
                    "count": len(items),
                    "result": result,
                }
            )

    async def run_split_brain_cycle(self) -> None:
        h = self.host
        settings = h._load_gitlab_routing_settings()
        if not settings.enabled:
            return
        for project_id, project_settings in settings.projects.items():
            if not project_settings.enabled:
                continue
            binding = h._orchestrator_binding(project_id)
            if not binding:
                continue
            items = h._split_brain_watchdog_candidates(project_id)
            if not items:
                continue
            binding = await h._replace_nonperforming_thread_if_needed(binding, "split-brain-watchdog")
            if h._thread_queue_depth(binding.thread_id) >= 3 or h._thread_is_active(binding.thread_id):
                continue
            dispatch_key = f"split-brain-watchdog:{project_id}:{binding.thread_id}"
            if not h._watchdog_dispatch_allowed(dispatch_key):
                continue
            h._record_watchdog_dispatch(dispatch_key)
            result = await self._bounded_reasoning_dispatch(
                binding,
                h._format_split_brain_watchdog_prompt(project_id, items),
                "split-brain-watchdog",
                cycle_key=dispatch_key,
                payload={
                    "project_id": project_id,
                    "item_refs": [state.ref for state, _ in items[:8]],
                },
            )
            h._append_bot_event(
                {
                    "type": "split_brain_watchdog_dispatched",
                    "project_id": project_id,
                    "thread_id": binding.thread_id,
                    "item_refs": [state.ref for state, _ in items[:8]],
                    "count": len(items),
                    "result": result,
                }
            )

    async def run_work_item_sla_cycle(self) -> None:
        h = self.host
        states = h._load_work_item_states()
        if not states:
            return
        now = time.time()
        changed = False
        for ref, state in list(states.items()):
            if state.current_stage == "closed" or state.closed_at:
                continue
            if not state.project_id:
                continue

            if state.handoff and state.handoff.status == "pending":
                handoff = state.handoff
                pending_age = now - handoff.requested_at
                recipient = h._coerce_owner(handoff.to_agent)
                if pending_age >= h._work_item_handoff_timeout_seconds():
                    sender = h._coerce_owner(handoff.from_agent)
                    state = h._archive_active_handoff(
                        state,
                        now=now,
                        status="superseded",
                        reason_code="handoff_expired",
                    )
                    state.current_owner = sender
                    state.current_stage = "implementation_active"
                    state.next_owner = sender
                    state.next_action = state.next_action or "Resume ownership or escalate one exact blocker."
                    state.blocker = "Structured handoff expired without acknowledgement."
                    state.updated_at = now
                    state.last_meaningful_update_at = now
                    states[ref] = state
                    h._append_work_item_event(
                        h._work_item_event(
                            ref,
                            "handoff_expired",
                            payload={
                                "from_agent": handoff.from_agent,
                                "to_agent": handoff.to_agent,
                            },
                        )
                    )
                    changed = True
                elif recipient:
                    binding = h._binding_for_agent(
                        recipient,
                        state.project_id,
                        preferred_conversation_id=h.HANDOFF_COORDINATION_CHANNEL,
                    )
                    if binding and not h._thread_is_active(binding.thread_id) and not h._thread_queue_depth(binding.thread_id):
                        dispatch_key = f"work-item-handoff:{ref}:{binding.thread_id}:{recipient}"
                        if h._watchdog_dispatch_allowed(dispatch_key) and not h._thread_recently_active(binding.thread_id):
                            binding = await h._replace_nonperforming_thread_if_needed(binding, "work-item-handoff")
                            h._record_watchdog_dispatch(dispatch_key)
                            result = await self._bounded_reasoning_dispatch(
                                binding,
                                h._work_item_dispatch_text(state),
                                "work-item-sla",
                                cycle_key=dispatch_key,
                                payload={
                                    "ref": ref,
                                    "agent": recipient,
                                    "stage": state.current_stage,
                                    "handoff_status": "pending",
                                },
                            )
                            h._append_bot_event(
                                {
                                    "type": "work_item_handoff_watchdog_dispatched",
                                    "ref": ref,
                                    "thread_id": binding.thread_id,
                                    "agent": recipient,
                                    "result": result,
                                }
                            )
                continue

            owner = h._coerce_owner(state.current_owner or state.next_owner)
            if not owner:
                continue
            age = now - h._owner_activity_timestamp(state)
            threshold = h._work_item_sla_threshold_seconds(state)
            if age < threshold:
                continue
            binding = h._binding_for_agent(
                owner,
                state.project_id,
                preferred_conversation_id=h.HANDOFF_COORDINATION_CHANNEL,
            )
            if not binding:
                continue
            if h._thread_is_active(binding.thread_id) or h._thread_queue_depth(binding.thread_id) or h._thread_recently_active(binding.thread_id):
                continue
            binding = await h._replace_nonperforming_thread_if_needed(binding, "work-item-sla")
            dispatch_key = f"work-item-sla:{ref}:{binding.thread_id}:{owner}:{state.current_stage}"
            if not h._watchdog_dispatch_allowed(dispatch_key):
                continue
            h._record_watchdog_dispatch(dispatch_key)
            result = await self._bounded_reasoning_dispatch(
                binding,
                h._work_item_dispatch_text(state),
                "work-item-sla",
                cycle_key=dispatch_key,
                payload={
                    "ref": ref,
                    "agent": owner,
                    "stage": state.current_stage,
                },
            )
            h._append_bot_event(
                {
                    "type": "work_item_sla_dispatched",
                    "ref": ref,
                    "thread_id": binding.thread_id,
                    "agent": owner,
                    "current_stage": state.current_stage,
                    "age_seconds": age,
                    "result": result,
                }
            )
        if changed:
            h._save_work_item_states(states)


def install_autonomy_service(
    app: Any,
    host: Any,
    action_execution: ActionExecutionService | None = None,
    action_intents: ActionIntentService | None = None,
    *,
    controller: AutonomyController | None = None,
    canonical_events: CanonicalEventIngestionService | None = None,
) -> AutonomyService:
    """Install extracted autonomy cycle ownership before worker supervision."""

    existing = getattr(app.state, "autonomy_service", None)
    if isinstance(existing, AutonomyService) and existing.host is host:
        service = existing
        if action_execution is not None:
            service.action_execution = action_execution
        if action_intents is not None:
            service.action_intents = action_intents
        if controller is not None:
            service.controller = controller
        if canonical_events is not None:
            service.canonical_events = canonical_events
    else:
        service = AutonomyService(
            host,
            action_execution=action_execution,
            action_intents=action_intents,
            controller=controller,
            canonical_events=canonical_events,
        )
        app.state.autonomy_service = service

    host._run_owner_work_watchdog_cycle = service.run_owner_work_cycle
    host._run_release_gate_watchdog_cycle = service.run_release_gate_cycle
    host._run_work_item_sla_cycle = service.run_work_item_sla_cycle
    host._run_orchestrator_watchdog_cycle = service.run_orchestrator_cycle
    host._run_split_brain_watchdog_cycle = service.run_split_brain_cycle
    host._prepare_external_action = service.prepare_external_action
    host._execute_external_action = service.execute_external_action
    host._verify_external_action = service.verify_external_action
    host._rollback_external_action = service.rollback_external_action
    return service
