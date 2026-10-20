from __future__ import annotations

import asyncio
import hashlib
import inspect
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
from codex_web.services.work_item_dependencies import actionable_owner_value, work_item_repository_scope
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.action_providers import ActionExecutionService
from codex_web.services.autonomy_controller import AutonomyController
from codex_web.services.watchdog_dispatch import WatchdogDispatchPolicy
from codex_web.services.autonomy_dependencies import AutonomyRuntimeDependencies
from codex_web.services.canonical_events import CanonicalEventIngestionService


class AutonomyService:
    """Own autonomous work-item/watchdog cycle decisions outside core.py."""

    ACTIONABLE_OWNER_STAGES = frozenset(
        {
            "implementation_active",
            "failed_with_action_owner",
            "ready_for_validation",
            "validation_running",
            "ready_to_close",
        }
    )
    OWNER_DISPATCH_WINDOW_SECONDS = 300

    def __init__(
        self,
        host: Any | None = None,
        action_execution: ActionExecutionService | None = None,
        action_intents: ActionIntentService | None = None,
        *,
        controller: AutonomyController | None = None,
        canonical_events: CanonicalEventIngestionService | None = None,
        runtime: AutonomyRuntimeDependencies | None = None,
    ) -> None:
        if runtime is None:
            if host is None:
                raise TypeError(
                    "AutonomyService requires explicit runtime dependencies"
                )
            runtime = AutonomyRuntimeDependencies.from_host(host)
        self.runtime = runtime
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
        work_item: Any | None = None,
        recovery_reason: str | None = None,
        queue_limit: int = 1,
        allow_active: bool = False,
        clear_stale_active: bool = False,
    ) -> dict[str, Any]:
        """Route watchdog reasoning through canonical event + autonomy controls."""

        dispatch_context = {}
        if work_item is not None:
            primary, writable = work_item_repository_scope(work_item)
            payload = {**payload, "resource_ids": list(dict.fromkeys(
                [*getattr(work_item, "resource_ids", ()), *writable]
            ))}
            if getattr(work_item, "goal_id", None):
                payload["goal_id"] = work_item.goal_id
            dispatch_context = {
                "work_item_ref": work_item.ref,
                "repository_resource_id": primary,
                "writable_repository_resource_ids": writable,
                "require_idle": True,
            }
            if work_item.handoff is not None and work_item.handoff.status == "pending":
                dispatch_context["handoff_requested_at"] = work_item.handoff.requested_at

        recovery_attempted = False
        recovered_binding = None
        recovery_error = None

        async def dispatch_admitted() -> dict[str, Any]:
            nonlocal recovery_attempted, recovered_binding, recovery_error
            # Worker recovery is an execution side effect. It belongs INSIDE
            # the controller's admitted reasoner. A reasoning retry must reuse
            # this same preparation outcome, never start another worker after
            # an uncertain or failed replacement.
            prepared = binding
            native_recovery = getattr(self.runtime, "owner_lane_recovery", None)
            if native_recovery is not None:
                prepared = await asyncio.to_thread(native_recovery.resolve_binding, binding)
                terminal = await asyncio.to_thread(native_recovery.terminal, prepared)
                if recovery_reason is not None and terminal is not None:
                    if work_item is not None:
                        # Terminal native lanes never fall through to generic
                        # replacement, including busy or spent recovery lanes.
                        return await native_recovery.recover(prepared, work_item, text)
                    return {"ok": False, "reason": "owner_recovery_current_work_required",
                        "threadId": prepared.thread_id,
                        "actionOwner": self.runtime.binding_prefix(binding)}
            if recovery_reason is not None:
                if not recovery_attempted:
                    recovery_attempted = True
                    try:
                        if clear_stale_active:
                            # This collaborator schedules reconciliation on this loop.
                            self.runtime.release_stale_active_turn(prepared.thread_id, recovery_reason)
                        if not await asyncio.to_thread(self._watchdog_lane_busy, prepared,
                                                       queue_limit, allow_active):
                            recovered_binding = await self.runtime.replace_nonperforming_thread(
                                prepared, recovery_reason)
                    except Exception as error:
                        recovery_error = error
                        raise
                elif recovery_error is not None:
                    raise recovery_error
                if recovered_binding is None:
                    return {"ok": True, "ignored": True, "reason": "owner_lane_busy",
                            "threadId": binding.thread_id}
                prepared = recovered_binding
                # Recovery can await provider/storage IO. Re-read the actual
                # successor; an earlier idle snapshot cannot authorize another
                # owner turn. Canonical dispatch also requires idle admission.
                if await asyncio.to_thread(self._watchdog_lane_busy, prepared,
                                           queue_limit, allow_active):
                    return {"ok": True, "ignored": True, "reason": "owner_lane_busy",
                            "threadId": prepared.thread_id}
                self.runtime.record_watchdog_dispatch(cycle_key)
            result = await self.runtime.dispatch_event(prepared, text, source, **dispatch_context)
            return {**result, "threadId": prepared.thread_id}

        if self.controller is None or self.canonical_events is None:
            return await dispatch_admitted()

        # Project scope may read canonical PostgreSQL state. Resolve it before
        # ingestion without blocking runtime RPC or changing the scoped result.
        organization_id, workspace_id = await asyncio.to_thread(
            self.runtime.project_scope, str(payload.get("project_id") or "")
        )
        normalized = json.dumps(
            {
                "payload": payload,
                "tenant_id": organization_id,
                "workspace_id": workspace_id,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        # Repeated idle-owner wakeups are new scheduled observations after
        # cooldown; unchanged issue payloads must not deduplicate forever.
        window = int(time.time() // WatchdogDispatchPolicy.cooldown_seconds())
        delivery = await self.canonical_events.ingest(
            event_type=CanonicalEventType.WORK_TRANSITION,
            source=f"autonomy-watchdog:{source}",
            idempotency_key=f"{cycle_key}:{window}:{digest}",
            payload=payload,
            tenant_id=organization_id,
            workspace_id=workspace_id,
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
                await dispatch_admitted()
            )
            return AutonomyReasoningResult(
                summary=(f"{source}: owner lane is already active or queued"
                         if dispatched.get("reason") == "owner_lane_busy"
                         else f"{source} dispatched through bounded autonomy")
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

    def _watchdog_lane_busy(
        self, binding: Any, queue_limit: int = 1, allow_active: bool = False,
        include_recent: bool = False,
    ) -> bool:
        """Blocking canonical reads; callers keep them off the observer loop."""
        d = self.runtime
        recovery = getattr(d, "owner_lane_recovery", None)
        if recovery is not None:
            binding = recovery.resolve_binding(binding)
        return (
            (not allow_active and d.thread_is_active(binding.thread_id))
            or d.thread_queue_depth(binding.thread_id) >= queue_limit
            or (include_recent and d.thread_recently_active(binding.thread_id))
        )

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
        d = self.runtime
        settings = await asyncio.to_thread(d.load_gitlab_routing_settings)
        if not settings.enabled:
            return
        # The canonical snapshot can require database IO and full catalog decoding.
        states = await asyncio.to_thread(d.load_work_item_states)
        for project_id, project_settings in settings.projects.items():
            if not project_settings.enabled:
                continue
            token = d.gitlab_token_for_project(project_id)
            group = d.gitlab_group_path(project_settings)
            for owner in d.owner_queue_agents:
                canonical_items = [
                    state
                    for state in states.values()
                    if state.project_id == project_id
                    and state.current_stage
                    in self.ACTIONABLE_OWNER_STAGES
                    and not state.closed_at
                    and not (
                        state.handoff
                        and state.handoff.status == "pending"
                    )
                    and d.coerce_owner(
                        actionable_owner_value(state)
                    )
                    == owner
                ]
                canonical_items.sort(
                    key=lambda state: (
                        0
                        if state.current_stage
                        == "failed_with_action_owner"
                        else 1,
                        d.owner_activity_timestamp(state),
                        state.ref,
                    )
                )
                binding = await asyncio.to_thread(d.binding_for_agent,
                    owner,
                    project_id,
                    preferred_conversation_id=d.handoff_coordination_channel,
                )
                if not binding:
                    continue
                missing_state_refs: list[str] = []
                if not canonical_items:
                    # Canonical owned work needs no raw provider credential.
                    # Only the legacy discovery fallback requires these.
                    if not token or not group:
                        continue
                    issues = d.gitlab_group_issues(
                        project_id,
                        project_settings,
                        labels=[f"owner::{owner}"],
                    )
                    if inspect.isawaitable(issues):
                        issues = await issues
                    missing_state_refs = [
                        issue.get("references", {}).get("full", "")
                        for issue in issues
                        if issue.get("references", {}).get("full")
                        and issue.get("state") == "opened"
                        and issue.get("references", {}).get("full")
                        not in states
                    ]
                if not missing_state_refs and not canonical_items:
                    continue
                dispatch_key = f"owner-work:{project_id}:{binding.thread_id}:{owner}"
                # Active/queued state is the duplicate guard. An earlier
                # failed dispatch must not leave either canonical work or a
                # GitLab-only owned issue without a live owner turn.
                # Canonical actionable work requires a live lane. Historical
                # activity is not evidence that the owner is still pursuing
                # the item; active/queued state and the dispatch cooldown are
                # the duplicate-dispatch guards for this path.
                selected = canonical_items[0] if canonical_items else None
                native_recovery = getattr(d, "owner_lane_recovery", None)
                if (selected is not None and native_recovery is not None
                        and await asyncio.to_thread(native_recovery.terminal, binding) is not None):
                    # Preserve spent/uncertain current-work originals while
                    # selecting another independent, currently owned task.
                    ready = await asyncio.to_thread(native_recovery.select_current_work, binding, canonical_items)
                    selected = ready or selected
                if missing_state_refs:
                    refs = ", ".join(missing_state_refs[:8])
                    extra_count = len(missing_state_refs) - 8
                    extra = (
                        f", plus {extra_count} more"
                        if extra_count > 0
                        else ""
                    )
                    agent_name = (
                        d.binding_prefix(binding)
                        or binding.thread_name
                        or owner
                    )
                    text = (
                        f"{agent_name}: bounded event-path fallback "
                        "triggered. GitLab shows owned open items with "
                        "no canonical codex-web state yet: "
                        f"{refs}{extra}. Reconcile those items through "
                        "codex-web now, post the exact item and next "
                        "action in the configured handoff coordination "
                        "channel, and keep working until completion or "
                        "one concrete escalation."
                    )
                    dispatch_mode = "missing_canonical_state"
                else:
                    text = d.work_item_dispatch_text(selected)
                    dispatch_mode = "idle_actionable_owner"
                dispatch_window = int(
                    time.time() // self.OWNER_DISPATCH_WINDOW_SECONDS
                )
                result = await self._bounded_reasoning_dispatch(
                    binding,
                    text,
                    "owner-work-watchdog",
                    recovery_reason="owner-work-watchdog",
                    clear_stale_active=True,
                    cycle_key=dispatch_key,
                    work_item=selected,
                    payload={
                        "project_id": project_id,
                        "agent": owner,
                        "missing_state_refs": missing_state_refs[:8],
                        "work_item_ref": (
                            selected.ref if selected is not None else None
                        ),
                        "work_item_stage": (
                            selected.current_stage
                            if selected is not None
                            else None
                        ),
                        "dispatch_mode": dispatch_mode,
                        "dispatch_window": dispatch_window,
                    },
                )
                d.append_bot_event(
                    {
                        "type": "owner_work_watchdog_dispatched",
                        "project_id": project_id,
                        "agent": owner,
                        "thread_id": result.get("threadId", binding.thread_id),
                        "issue_count": (
                            len(missing_state_refs)
                            or len(canonical_items)
                        ),
                        "work_item_ref": (
                            selected.ref if selected is not None else None
                        ),
                        "dispatch_mode": dispatch_mode,
                        "result": result,
                    }
                )

    async def run_release_gate_cycle(self) -> None:
        d = self.runtime
        settings = await asyncio.to_thread(d.load_gitlab_routing_settings)
        if not settings.enabled:
            return
        states = await asyncio.to_thread(d.load_work_item_states)
        now = time.time()
        for project_id, project_settings in settings.projects.items():
            if not project_settings.enabled:
                continue
            binding = await asyncio.to_thread(d.binding_for_agent, "release manager", project_id)
            if not binding:
                continue
            p1_issues = d.gitlab_group_issues(
                project_id,
                project_settings,
                labels=["priority::P1"],
            )
            if inspect.isawaitable(p1_issues):
                p1_issues = await p1_issues
            if p1_issues:
                continue
            stale_release_states = [
                state
                for state in states.values()
                if state.project_id == project_id
                and d.coerce_owner(actionable_owner_value(state)) == "release manager"
                and state.current_stage in {"ready_for_validation", "validation_running", "ready_to_close"}
                and (now - d.owner_activity_timestamp(state)) >= d.release_validation_sla_seconds()
            ]
            if not stale_release_states:
                continue
            dispatch_key = f"release-gate:{project_id}:{binding.thread_id}"
            if not d.watchdog_dispatch_allowed(dispatch_key):
                continue
            if await asyncio.to_thread(d.thread_recently_active, binding.thread_id):
                continue
            refs = ", ".join(state.ref for state in stale_release_states[:8])
            extra = f", plus {len(stale_release_states) - 8} more" if len(stale_release_states) > 8 else ""
            text = (
                "Release Manager: bounded release-lane fallback triggered. "
                f"Open P1 count is zero, but release-ready items are stale: {refs}{extra}. "
                "Resume the exact deploy/verify/E2E next action or state one exact blocker in the configured "
                "handoff coordination channel. Reconcile the affected work item in codex-web before ending the turn."
            )
            result = await self._bounded_reasoning_dispatch(
                binding,
                text,
                "release-gate-watchdog",
                recovery_reason="release-gate-watchdog",
                clear_stale_active=True,
                cycle_key=dispatch_key,
                payload={
                    "project_id": project_id,
                    "stale_refs": [state.ref for state in stale_release_states[:8]],
                },
            )
            d.append_bot_event(
                {
                    "type": "release_gate_watchdog_dispatched",
                    "project_id": project_id,
                    "thread_id": result.get("threadId", binding.thread_id),
                    "result": result,
                }
            )

    async def run_orchestrator_cycle(self) -> None:
        d = self.runtime
        settings = await asyncio.to_thread(d.load_gitlab_routing_settings)
        if not settings.enabled:
            return
        for project_id, project_settings in settings.projects.items():
            if not project_settings.enabled:
                continue
            binding = await asyncio.to_thread(d.orchestrator_binding, project_id)
            if not binding:
                continue
            items = await asyncio.to_thread(d.orchestrator_watchdog_candidates, project_id)
            if not items:
                continue
            recovery = getattr(d, "owner_lane_recovery", None)
            recovery_work = None
            if recovery is not None:
                states = await asyncio.to_thread(d.load_work_item_states)
                owned = [state for state in states.values()
                    if state.project_id == project_id and state.current_stage in self.ACTIONABLE_OWNER_STAGES
                    and recovery.owns(binding, state)]
                owned.sort(key=lambda state: (d.owner_activity_timestamp(state), state.ref))
                recovery_work = (await asyncio.to_thread(recovery.select_current_work, binding, owned)
                    or (owned[0] if owned else None))
            recoverable = (recovery_work is not None
                and await asyncio.to_thread(recovery.candidate, binding, recovery_work))
            if not recoverable and await asyncio.to_thread(self._watchdog_lane_busy, binding, 3, True):
                continue
            dispatch_key = f"orchestrator-watchdog:{project_id}:{binding.thread_id}"
            if not d.watchdog_dispatch_allowed(dispatch_key):
                continue
            result = await self._bounded_reasoning_dispatch(
                binding,
                d.format_orchestrator_watchdog_prompt(project_id, items),
                "orchestrator-watchdog",
                recovery_reason="orchestrator-watchdog",
                queue_limit=3, allow_active=True,
                cycle_key=dispatch_key,
                work_item=recovery_work if recoverable else None,
                payload={
                    "project_id": project_id,
                    "item_refs": [state.ref for _, state, _ in items[:8]],
                },
            )
            d.append_bot_event(
                {
                    "type": "orchestrator_watchdog_dispatched",
                    "project_id": project_id,
                    "thread_id": result.get("threadId", binding.thread_id),
                    "item_refs": [state.ref for _, state, _ in items[:8]],
                    "count": len(items),
                    "result": result,
                }
            )

    async def run_split_brain_cycle(self) -> None:
        d = self.runtime
        settings = await asyncio.to_thread(d.load_gitlab_routing_settings)
        if not settings.enabled:
            return
        for project_id, project_settings in settings.projects.items():
            if not project_settings.enabled:
                continue
            binding = await asyncio.to_thread(d.orchestrator_binding, project_id)
            if not binding:
                continue
            items = await asyncio.to_thread(d.split_brain_watchdog_candidates, project_id)
            if not items:
                continue
            if await asyncio.to_thread(self._watchdog_lane_busy, binding, 3):
                continue
            dispatch_key = f"split-brain-watchdog:{project_id}:{binding.thread_id}"
            if not d.watchdog_dispatch_allowed(dispatch_key):
                continue
            result = await self._bounded_reasoning_dispatch(
                binding,
                d.format_split_brain_watchdog_prompt(project_id, items),
                "split-brain-watchdog",
                recovery_reason="split-brain-watchdog",
                queue_limit=3,
                cycle_key=dispatch_key,
                payload={
                    "project_id": project_id,
                    "item_refs": [state.ref for state, _ in items[:8]],
                },
            )
            d.append_bot_event(
                {
                    "type": "split_brain_watchdog_dispatched",
                    "project_id": project_id,
                    "thread_id": result.get("threadId", binding.thread_id),
                    "item_refs": [state.ref for state, _ in items[:8]],
                    "count": len(items),
                    "result": result,
                }
            )

    async def run_work_item_sla_cycle(self) -> None:
        d = self.runtime
        states = await asyncio.to_thread(d.load_work_item_states)
        if not states:
            return
        now = time.time()
        for ref, state in list(states.items()):
            if state.current_stage == "closed" or state.closed_at:
                continue
            if not state.project_id:
                continue

            if state.handoff and state.handoff.status == "pending":
                handoff = state.handoff
                delivery = handoff.delivery
                awaiting_delivery = delivery is not None and delivery.status == "awaiting_dispatch"
                clock_start = (
                    delivery.started_at
                    if delivery is not None and delivery.status == "delivered"
                    else handoff.requested_at
                )
                if clock_start is None:
                    # Missing qualified evidence cannot start an ACK deadline.
                    continue
                pending_age = now - clock_start
                timeout = d.handoff_delivery_timeout_seconds() if awaiting_delivery else d.handoff_timeout_seconds()
                expiry_reason = "handoff_delivery_expired" if awaiting_delivery else "handoff_expired"
                recipient = d.coerce_owner(handoff.to_agent)
                if pending_age >= timeout:
                    if d.update_work_item_state is None:
                        # Never expire from a detached snapshot without a
                        # canonical atomic mutation collaborator.
                        continue
                    expired = False

                    def expire_current(current):
                        nonlocal expired
                        expired = False
                        if (
                            current is None or current.closed_at
                            or current.current_stage == "closed"
                            or current.handoff != handoff
                        ):
                            return current
                        sender = d.coerce_owner(handoff.from_agent)
                        current = d.archive_active_handoff(
                            current, now=now, status="superseded",
                            reason_code=expiry_reason,
                        )
                        current.current_owner = sender
                        current.current_stage = "implementation_active"
                        current.next_owner = sender
                        current.next_action = (
                            f"{sender}: reconcile the expired delivery to {recipient} and renew only after its exact blocker changes."
                            if awaiting_delivery else
                            f"{sender}: reconcile the expired intake to {recipient}; no pending acknowledgement remains."
                        )
                        current.blocker = (
                            "Structured handoff could not be delivered within its dispatch window."
                            if awaiting_delivery else "Structured handoff expired without acknowledgement."
                        )
                        current.updated_at = now
                        current.last_meaningful_update_at = now
                        expired = True
                        return current

                    await asyncio.to_thread(d.update_work_item_state, ref, expire_current)
                    if not expired:
                        continue
                    await asyncio.to_thread(d.append_work_item_event,
                        d.work_item_event(
                            ref,
                            expiry_reason,
                            payload={
                                "from_agent": handoff.from_agent,
                                "to_agent": handoff.to_agent,
                            },
                        )
                    )
                    if d.schedule_current_work_item_writeback is not None:
                        # Resolve current canonical state after the atomic
                        # mutation, not the detached SLA snapshot. Reuse the
                        # bounded provider-neutral writeback coordinator.
                        d.schedule_current_work_item_writeback(ref)
                elif recipient:
                    binding = await asyncio.to_thread(d.binding_for_agent,
                        recipient,
                        state.project_id,
                        preferred_conversation_id=d.handoff_coordination_channel,
                    )
                    if binding and not await asyncio.to_thread(self._watchdog_lane_busy, binding):
                        dispatch_key = f"work-item-handoff:{ref}:{binding.thread_id}:{recipient}"
                        if d.watchdog_dispatch_allowed(dispatch_key) and not await asyncio.to_thread(d.thread_recently_active, binding.thread_id):
                            result = await self._bounded_reasoning_dispatch(
                                binding,
                                d.work_item_dispatch_text(state),
                                "work-item-sla",
                                recovery_reason="work-item-handoff",
                                cycle_key=dispatch_key,
                                work_item=state,
                                payload={
                                    "project_id": state.project_id,
                                    "ref": ref,
                                    "agent": recipient,
                                    "stage": state.current_stage,
                                    "handoff_status": "pending",
                                },
                            )
                            d.append_bot_event(
                                {
                                    "type": "work_item_handoff_watchdog_dispatched",
                                    "ref": ref,
                                    "thread_id": result.get("threadId", binding.thread_id),
                                    "agent": recipient,
                                    "result": result,
                                }
                            )
                continue

            owner = d.coerce_owner(actionable_owner_value(state))
            if not owner:
                continue
            age = now - d.owner_activity_timestamp(state)
            threshold = d.work_item_sla_threshold_seconds(state)
            if age < threshold:
                continue
            binding = await asyncio.to_thread(d.binding_for_agent,
                owner,
                state.project_id,
                preferred_conversation_id=d.handoff_coordination_channel,
            )
            if not binding:
                continue
            if await asyncio.to_thread(self._watchdog_lane_busy, binding, 1, False, True):
                continue
            dispatch_key = f"work-item-sla:{ref}:{binding.thread_id}:{owner}:{state.current_stage}"
            if not d.watchdog_dispatch_allowed(dispatch_key):
                continue
            result = await self._bounded_reasoning_dispatch(
                binding,
                d.work_item_dispatch_text(state),
                "work-item-sla",
                recovery_reason="work-item-sla",
                work_item=state,
                cycle_key=dispatch_key,
                payload={
                    "project_id": state.project_id,
                    "ref": ref,
                    "agent": owner,
                    "stage": state.current_stage,
                },
            )
            d.append_bot_event(
                {
                    "type": "work_item_sla_dispatched",
                    "ref": ref,
                    "thread_id": result.get("threadId", binding.thread_id),
                    "agent": owner,
                    "current_stage": state.current_stage,
                    "age_seconds": age,
                    "result": result,
                }
            )


def install_autonomy_service(
    app: Any,
    host: Any,
    action_execution: ActionExecutionService | None = None,
    action_intents: ActionIntentService | None = None,
    *,
    controller: AutonomyController | None = None,
    canonical_events: CanonicalEventIngestionService | None = None,
    runtime: AutonomyRuntimeDependencies | None = None,
) -> AutonomyService:
    """Install extracted autonomy cycle ownership before worker supervision."""

    existing = getattr(app.state, "autonomy_service", None)
    if isinstance(existing, AutonomyService):
        service = existing
        if runtime is not None:
            service.runtime = runtime
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
            runtime=runtime,
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
