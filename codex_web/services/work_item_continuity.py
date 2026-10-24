from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from functools import wraps

from fastapi import HTTPException

from codex_web.models import WorkItemState, WorkItemHandoffDelivery
from codex_web.services.work_item_dependencies import actionable_owner_value, work_item_repository_scope
from codex_web.services.keyed_background_tasks import KeyedTaskCoordinator
from codex_web.services.runtime_policy import RuntimePolicy
from codex_web.services.turn_queue_policy import TurnQueuePolicy
from codex_web.owner_routing import OwnerRoutingError


def _routing_dispatch(method):
    @wraps(method)
    async def dispatch(self, state, *args, **kwargs):
        try:
            return await method(self, state, *args, **kwargs)
        except OwnerRoutingError as exc:
            if self.owner_routing is not None:
                self.owner_routing.record_failure(state, exc)
            if method.__name__ == 'dispatch_structured_handoff' and state.handoff is not None:
                # The canonical blocker CAS can refuse a stale snapshot. Keep
                # this attempted handoff's refusal inspectable without retrying
                # or claiming a native input outcome from the handled result.
                code = str(exc)
                try:
                    self.append_event({
                        'type': 'work_item_handoff_dispatch_failed', 'ref': state.ref,
                        'source': kwargs.get('source'),
                        'handoff_requested_at': state.handoff.requested_at,
                        'recipient': state.handoff.to_agent,
                        'reason_code': code if re.fullmatch(r'(?:owner_|work_item_)[a-z0-9_]{1,112}', code) else 'owner_routing_refused',
                        'dispatch_outcome': 'owner_routing_blocked',
                        'native_outcome': 'not_inferred',
                    })
                except Exception:
                    pass  # A journal failure cannot turn a refusal into dispatch.
            return {'status': 'owner_routing_blocked', 'code': str(exc), 'dispatched': False}
    return dispatch


class DeferredWorkItemContinuityService:
    """Stable early reference that binds to the composed continuity owner later."""

    def __init__(self) -> None:
        self.service: WorkItemContinuityService | None = None

    def bind(self, service: "WorkItemContinuityService") -> None:
        self.service = service

    def record_handoff_delivery(self, ref: str, **evidence: Any) -> bool:
        return self._require().record_handoff_delivery(ref, **evidence)

    def _require(self) -> "WorkItemContinuityService":
        if self.service is None:
            raise RuntimeError("work-item continuity is not composed yet")
        return self.service

    def responsible_binding(self, state: WorkItemState) -> Any | None:
        return self._require().responsible_binding(state)

    def owner_dispatch_projection(self, state: WorkItemState) -> dict[str, Any]:
        return self._require().owner_dispatch_projection(state)

    def handoff_eligibility_projection(self, state: WorkItemState, **expected) -> dict[str, Any]:
        return self._require().handoff_eligibility_projection(state, **expected)

    def require_handoff_inspection_ready(self):
        return self._require().require_handoff_inspection_ready()

    def schedule_structured_handoff_dispatch(
        self,
        state: WorkItemState,
        *,
        source: str,
    ) -> None:
        self._require().schedule_structured_handoff_dispatch(
            state,
            source=source,
        )

    def schedule_handoff_continuity_check(
        self,
        state: WorkItemState,
        *,
        source: str,
    ) -> None:
        self._require().schedule_handoff_continuity_check(
            state,
            source=source,
        )

    def schedule_actionable_owner_dispatch(
        self,
        state: WorkItemState,
        *,
        source: str,
        actor: str | None = None,
    ) -> None:
        self._require().schedule_actionable_owner_dispatch(
            state,
            source=source,
            actor=actor,
        )

    async def steer_actionable_owner(
        self,
        state: WorkItemState,
        *,
        expected_owner: str,
        expected_thread_id: str,
        idempotency_key: str,
        actor: str,
        source: str,
        expected_definition=None,
    ) -> dict[str, Any]:
        return await self._require().steer_actionable_owner(
            state,
            expected_owner=expected_owner,
            expected_thread_id=expected_thread_id,
            idempotency_key=idempotency_key,
            actor=actor,
            source=source,
            **({'expected_definition': expected_definition} if expected_definition is not None else {}),
        )

    def schedule_actionable_owner_continuity_check(
        self,
        state: WorkItemState,
        *,
        source: str,
    ) -> None:
        self._require().schedule_actionable_owner_continuity_check(
            state,
            source=source,
        )

    def dispatch_text(self, state: WorkItemState) -> str:
        return self._require().dispatch_text(state)

    async def stop(self) -> None:
        if self.service is not None:
            await self.service.stop()


class WorkItemContinuityService:
    """Own handoff and actionable-owner dispatch/continuity scheduling."""

    def __init__(
        self,
        *,
        policy: RuntimePolicy,
        get_state: Callable[[str], WorkItemState],
        coerce_owner: Callable[[str | None], str | None],
        binding_for_agent: Callable[..., Any],
        replace_nonperforming_thread: Callable[[Any, str], Awaitable[Any]],
        dispatch_event: Callable[..., Awaitable[dict[str, Any]]],
        dispatch_text: Callable[[WorkItemState], str],
        append_event: Callable[[dict[str, Any]], None],
        truncate_text: Callable[[str, int], str],
        thread_is_active: Callable[[str], bool],
        thread_queue_depth: Callable[[str], int],
        thread_recently_active: Callable[[str], bool],
        watchdog_dispatch_allowed: Callable[[str], bool],
        record_watchdog_dispatch: Callable[[str], None],
        coordination_channel: str,
        revalidate_after_thread_replacement: bool = True,
        update_state: Callable[..., Any] | None = None,
        thread_queue: Callable[[str], list[Any]] | None = None,
    ) -> None:
        self.policy = policy
        self.owner_routing = None
        self.update_state = update_state
        self.get_state = get_state
        self.coerce_owner = coerce_owner
        self.binding_for_agent = binding_for_agent
        self.replace_nonperforming_thread = replace_nonperforming_thread
        self.dispatch_event = dispatch_event
        self.dispatch_text = dispatch_text
        self.append_event = append_event
        self.truncate_text = truncate_text
        self.thread_is_active = thread_is_active
        self.thread_queue_depth = thread_queue_depth
        self.thread_queue = thread_queue
        self.thread_recently_active = thread_recently_active
        self.watchdog_dispatch_allowed = watchdog_dispatch_allowed
        self.record_watchdog_dispatch = record_watchdog_dispatch
        self.coordination_channel = coordination_channel
        self.revalidate_after_thread_replacement = (
            revalidate_after_thread_replacement
        )
        self.actionable_owner_tasks: dict[str, asyncio.Task[None]] = {}
        self.handoff_tasks: dict[str, asyncio.Task[None]] = {}
        self.owner_steer_locks: dict[str, asyncio.Lock] = {}
        max_concurrency_getter = getattr(
            self.policy,
            "background_task_max_concurrency",
            None,
        )
        per_scope_getter = getattr(
            self.policy,
            "continuity_per_project_concurrency",
            None,
        )
        self.dispatch_coordinator = KeyedTaskCoordinator(
            max_concurrency=(
                int(max_concurrency_getter())
                if callable(max_concurrency_getter)
                else 16
            ),
            per_scope_concurrency=(
                int(per_scope_getter())
                if callable(per_scope_getter)
                else 4
            ),
        )

    @staticmethod
    def _handoff_revision(state: WorkItemState) -> str:
        handoff = state.handoff
        return "|".join(
            (
                state.ref,
                str(handoff.to_agent if handoff else ""),
                str(handoff.requested_at if handoff else ""),
                str(handoff.status if handoff else ""),
            )
        )

    def _owner_revision(self, state: WorkItemState) -> str:
        return "|".join(
            (
                state.ref,
                str(
                    self.coerce_owner(
                        actionable_owner_value(state)
                    )
                    or ""
                ),
                str(state.current_stage or ""),
                str(state.closed_at or ""),
                str(state.handoff.status if state.handoff else ""),
            )
        )

    def status(self) -> dict[str, Any]:
        return {
            "dispatch": self.dispatch_coordinator.status(),
            "delayedActionableOwnerChecks": sum(
                1
                for task in self.actionable_owner_tasks.values()
                if not task.done()
            ),
            "delayedHandoffChecks": sum(
                1
                for task in self.handoff_tasks.values()
                if not task.done()
            ),
        }

    @staticmethod
    def actionable_owner_stage(stage: str | None) -> bool:
        return stage in {
            "implementation_active",
            "failed_with_action_owner",
            "ready_for_validation",
            "validation_running",
            "ready_to_close",
        }

    def responsible_binding(self, state: WorkItemState) -> Any | None:
        configured = actionable_owner_value(state)
        owner = self._routed_owner(state, configured)
        if not owner or not state.project_id:
            return None
        if self._alias_intent(state, configured) is not None:
            self.owner_routing.require_resolved_originals(state)
            binding = self.owner_routing.existing_binding(state, owner,
                preferred_conversation_id=self.coordination_channel)
            self.owner_routing.executor(state, binding, owner)
            return binding
        return self.binding_for_agent(
            owner,
            state.project_id,
            preferred_conversation_id=self.coordination_channel,
        )

    def _alias_intent(self, state, value):
        routing = self.owner_routing.snapshot(state) if self.owner_routing is not None else None
        return routing.binding(value) if routing is not None else None

    def _routed_owner(self, state, value):
        routing = self.owner_routing.snapshot(state) if self.owner_routing is not None else None
        owner = routing.owner(value) if routing is not None else self.coerce_owner(value)
        if routing is not None and owner is not None and owner not in routing.known_owners:
            raise OwnerRoutingError('owner_routing_owner_unmapped')
        return owner

    def _owner_dispatch_resolution(
        self,
        state: WorkItemState,
    ) -> tuple[str | None, Any | None, str | None]:
        """Resolve the bounded owner binding shared by reads and steering."""
        from codex_web.owner_routing import OwnerRoutingError
        try:
            owner = self._routed_owner(state, actionable_owner_value(state))
        except OwnerRoutingError as exc:
            return self.coerce_owner(actionable_owner_value(state)), None, str(exc)
        if state.current_stage == "closed" or state.closed_at:
            return owner, None, "work_item_closed"
        if not self.actionable_owner_stage(state.current_stage):
            return owner, None, "work_item_not_actionable"
        if state.handoff and state.handoff.status == "pending":
            return owner, None, "work_item_handoff_pending"
        if not owner:
            return None, None, "work_item_owner_unavailable"
        try:
            binding = self.responsible_binding(state)
        except OwnerRoutingError as exc:
            return owner, None, str(exc)
        if binding is None or not str(getattr(binding, "thread_id", "")).strip():
            return owner, None, "work_item_owner_binding_unavailable"
        if getattr(binding, "project_id", None) != state.project_id:
            return owner, None, "work_item_owner_project_mismatch"
        return owner, binding, None

    def record_handoff_delivery(
        self, ref: str, *, expected_requested_at: float, recipient: str,
        organization_id: str, workspace_id: str, project_id: str,
        thread_id: str, turn_id: str, execution_id: str, started_at: float,
        execution_profile_id: str | None = None,
        repository_resource_id: str | None = None,
        writable_repository_resource_ids: tuple[str, ...] = (),
        read_only_repository_resource_ids: tuple[str, ...] = (),
        owner_routing_pin=None,
    ) -> bool:
        """Record a successful native start, never a queue/unknown-start receipt."""
        if self.update_state is None or not all((thread_id, turn_id, execution_id, execution_profile_id)):
            return False
        recorded = False
        scope_rejected = False

        def update(current):
            nonlocal recorded, scope_rejected
            recorded = False
            scope_rejected = False
            if current is None or current.closed_at or current.current_stage == "closed":
                return current
            handoff = current.handoff
            if (
                current.organization_id != organization_id or current.workspace_id != workspace_id
                or current.project_id != project_id or handoff is None
                or handoff.status != "pending" or handoff.requested_at != expected_requested_at
                or handoff.delivery is None
                or (handoff.owner_routing is None and self.coerce_owner(handoff.to_agent) != self.coerce_owner(recipient))
                or started_at < handoff.requested_at
                or (handoff.delivery is not None and handoff.delivery.status == "delivered")
            ):
                return current
            if handoff.owner_routing is not None:
                intent = handoff.owner_routing
                if (owner_routing_pin is None or owner_routing_pin.binding != intent
                        or owner_routing_pin.work_item_ref != ref
                        or owner_routing_pin.handoff_requested_at != expected_requested_at
                        or intent.canonical_owner != self.coerce_owner(recipient)
                        or intent.configured_owner != self.coerce_owner(handoff.to_agent)
                        or (intent.organization_id, intent.workspace_id, intent.project_id)
                        != (organization_id, workspace_id, project_id)):
                    return current
            elif owner_routing_pin is not None:
                pin = owner_routing_pin
                if (pin.schema_version != '1.1' or pin.recipient is None
                        or pin.binding.schema_version != '1.1'
                        or pin.binding.configured_owner != self.coerce_owner(handoff.to_agent)
                        or pin.binding.canonical_owner != self.coerce_owner(recipient)
                        or pin.work_item_ref != ref or pin.handoff_requested_at != expected_requested_at
                        or pin.recipient.thread_id != thread_id or pin.recipient.execution_id != execution_id
                        or (pin.binding.organization_id, pin.binding.workspace_id, pin.binding.project_id)
                           != (organization_id, workspace_id, project_id)):
                    return current
            expected_primary, expected_writable = self._dispatch_repository_scope(current)
            if execution_profile_id in {"orchestration-only", "orchestration-admin"}:
                # The broker's coordination profile grants no repository scope.
                scope_matches = not (
                    repository_resource_id or writable_repository_resource_ids
                    or read_only_repository_resource_ids
                )
            elif execution_profile_id == "repository-write":
                scope_matches = bool(expected_writable) and (
                    set(writable_repository_resource_ids) == set(expected_writable)
                    and repository_resource_id in expected_writable
                    and (expected_primary is None or repository_resource_id == expected_primary)
                )
            else:
                scope_matches = False
            if not scope_matches:
                scope_rejected = True
                return current
            handoff.delivery = WorkItemHandoffDelivery(status="delivered", started_at=started_at,
                thread_id=thread_id, turn_id=turn_id, execution_id=execution_id,
                scope_verified=True, execution_profile_id=execution_profile_id,
                repository_resource_id=repository_resource_id,
                writable_repository_resource_ids=writable_repository_resource_ids,
                read_only_repository_resource_ids=read_only_repository_resource_ids)
            current.updated_at = time.time()
            recorded = True
            return current

        self.update_state(ref, update)
        if recorded:
            self.append_event({"type":"work_item_handoff_delivery_started", "ref":ref,
                "requested_at":expected_requested_at,"recipient":recipient,
                "thread_id":thread_id,"turn_id":turn_id,"execution_id":execution_id,
                "started_at":started_at,"scope_verified":True,
                "execution_profile_id":execution_profile_id,
                "repository_resource_id":repository_resource_id,
                "writable_repository_resource_ids":list(writable_repository_resource_ids),
                "read_only_repository_resource_ids":list(read_only_repository_resource_ids)})
        elif scope_rejected:
            self.append_event({"type":"work_item_handoff_delivery_scope_rejected", "ref":ref,
                "requested_at":expected_requested_at,"recipient":recipient,
                "thread_id":thread_id,"turn_id":turn_id,"execution_id":execution_id,
                "execution_profile_id":execution_profile_id,
                "repository_resource_id":repository_resource_id,
                "writable_repository_resource_ids":list(writable_repository_resource_ids)})
        return recorded

    def require_handoff_inspection_ready(self):
        if self.owner_routing is None:
            raise OwnerRoutingError('owner_routing_inspection_unavailable')
        self.owner_routing.require_readonly_inspection_ready()

    def handoff_eligibility_projection(self, state: WorkItemState, *,
        expected_requested_at: float, expected_recipient: str,
    ) -> dict[str, Any]:
        """Explicit current inspection, never preparation or input admission."""
        handoff = state.handoff
        observed = state.model_dump(mode='json')
        result = {'inspection_only': True, 'eligible': False,
            'dispatch_authorized': False, 'admission_checked': False,
            'native_outcome': 'not_inferred', 'ref': state.ref,
            'observed_work_updated_at': state.updated_at,
            'handoff_requested_at': handoff.requested_at if handoff else None,
            'recipient': handoff.to_agent if handoff else None,
            'current_thread_id': None}
        def verdict(status, reason=None):
            return {**result, 'status': status, 'blocked_reason': reason}
        if handoff is None or handoff.status != 'pending':
            return verdict('no_pending_handoff', 'work_item_handoff_not_pending')
        if (handoff.requested_at != expected_requested_at
                or handoff.to_agent != expected_recipient):
            return verdict('changed', 'work_item_handoff_changed')
        if handoff.acknowledged_at is not None or (
                handoff.delivery is not None and handoff.delivery.status == 'delivered'):
            return verdict('already_delivered', 'work_item_handoff_delivery_already_recorded')
        if state.closed_at or state.current_stage == 'closed':
            return verdict('ineligible', 'work_item_closed')
        if self.owner_routing is None:
            return verdict('unavailable', 'owner_routing_unavailable')
        try:
            self.require_handoff_inspection_ready()
            recipient = self._routed_owner(state, handoff.to_agent)
            if not recipient:
                return verdict('ineligible', 'owner_routing_recipient_unavailable')
            intent = self._alias_intent(state, handoff.to_agent)
            binding = (self.owner_routing.existing_binding(state, recipient,
                preferred_conversation_id=self.coordination_channel) if intent is not None else
                self.binding_for_agent(recipient, state.project_id,
                    preferred_conversation_id=self.coordination_channel))
            if binding is None:
                return verdict('ineligible', 'work_item_owner_binding_unavailable')
            _, writable = self._dispatch_repository_scope(state)
            # Deliberately synchronous: the async preparer can create a worker.
            # These are the same selection and fresh pin checks used by dispatch.
            pin = self.owner_routing.compatible_pin(state, binding,
                handoff.to_agent, writable_repository_ids=writable,
                lookup_actor=self.owner_routing.inspection_actor)
            if pin is None:
                return verdict('unavailable', 'owner_routing_pin_unavailable')
            profile, _ = self.owner_routing.context_for_pin(pin, binding,
                lookup_actor=self.owner_routing.inspection_actor)
            latest = self.get_state(state.ref)
            if latest.model_dump(mode='json') != observed:
                return verdict('changed', 'work_item_handoff_changed')
            witness = pin.recipient
            result.update(current_thread_id=witness.thread_id if witness else binding.thread_id,
                definition=pin.binding.definition.model_dump(mode='json'),
                configured_owner=pin.binding.configured_owner,
                canonical_owner=pin.binding.canonical_owner,
                requested_writable_repository_ids=list(writable),
                agent_profile_id=profile.profile_id, agent_profile_revision=profile.revision,
                configured_execution_profile_id=profile.execution_profile_id,
                bot_binding_id=binding.id,
                assignment_id=witness.assignment_id if witness else None,
                fence=witness.fence if witness else None)
            return {**verdict('eligible'), 'eligible': True}
        except OwnerRoutingError as exc:
            if str(exc) in {'owner_routing_inspection_not_initialized', 'owner_routing_inspection_unavailable'}:
                return verdict('unavailable', str(exc))
            try:
                if self.get_state(state.ref).model_dump(mode='json') != observed:
                    return verdict('changed', 'work_item_handoff_changed')
            except Exception:
                return verdict('unavailable', 'owner_routing_inspection_unavailable')
            code = str(exc)
            code = code if re.fullmatch(r'(?:owner_|work_item_)[a-z0-9_]{1,112}', code) else 'owner_routing_refused'
            return verdict('ineligible', code)
        except Exception:
            return verdict('unavailable', 'owner_routing_inspection_unavailable')

    def owner_dispatch_projection(self, state: WorkItemState) -> dict[str, Any]:
        """Expose only a currently eligible exact owner/thread pair."""
        owner, binding, blocked_reason = self._owner_dispatch_resolution(state)
        metadata = {}
        if self.owner_routing is not None:
            try:
                intent = self._alias_intent(state, actionable_owner_value(state))
                if intent is not None:
                    metadata = {'expected_definition': intent.definition.model_dump(mode='json'),
                        'owner_routing': intent.model_dump(mode='json'),
                        'next_action': 'Inspect the existing canonical binding, bootstrap and Agent Profile; repair configuration before retrying.'}
                    if blocked_reason == 'owner_routing_native_outcome_unresolved':
                        metadata['next_action'] = 'Inspect and reconcile the retained original native outcome before dispatching this Work Item again.'
                    if binding is not None:
                        profile, _ = self.owner_routing.executor(state, binding, intent.canonical_owner)
                        metadata.update({'agent_profile_id': profile.profile_id,
                            'agent_profile_revision': profile.revision,
                            'configured_execution_profile_id': profile.execution_profile_id,
                            'bot_binding_id': binding.id})
            except OwnerRoutingError as exc:
                blocked_reason, binding = str(exc), None
        target_thread_id = binding.thread_id if binding is not None else None
        readonly_ids = ()
        if blocked_reason is None and binding is not None and self.owner_routing is not None:
            try:
                _, writable_ids = self._dispatch_repository_scope(state)
                pin = self.owner_routing.compatible_pin(state, binding,
                    actionable_owner_value(state), writable_repository_ids=writable_ids)
                if pin is not None and pin.recipient is not None:
                    target_thread_id = pin.recipient.thread_id
                    readonly_ids = self.owner_routing.repository_scope_for_pin(pin, binding).read_only_repository_ids
            except OwnerRoutingError as exc:
                blocked_reason = str(exc)
        if blocked_reason is None and binding is not None and self.thread_queue_depth(target_thread_id):
            outcome = self._owner_queue_outcome(state, target_thread_id,
                read_only_repository_resource_ids=readonly_ids)
            metadata['queue_status'] = outcome
            if outcome != 'already_queued':
                blocked_reason = outcome
                metadata['next_action'] = 'Inspect the retained queue and its original outcomes; an unrelated input does not deliver this Work Item.'
        if blocked_reason is not None or binding is None:
            return {
                "eligible": False,
                "blocked_reason": blocked_reason,
                "expected_owner": None,
                "expected_thread_id": None,
                **metadata,
            }
        return {
            "eligible": True,
            "blocked_reason": None,
            "expected_owner": owner,
            "expected_thread_id": binding.thread_id,
            **metadata,
        }

    @staticmethod
    def _dispatch_repository_scope(
        state: WorkItemState,
    ) -> tuple[str | None, tuple[str, ...]]:
        return work_item_repository_scope(state)

    def _owner_queue_outcome(self, state: WorkItemState, thread_id: str, *,
        read_only_repository_resource_ids: tuple[str, ...] = (),
    ) -> str:
        if self.thread_queue is None:
            return 'owner_queue_context_unavailable'
        repository_id, writable_ids = self._dispatch_repository_scope(state)
        outcome = TurnQueuePolicy.work_item_queue_outcome(
            self.thread_queue(thread_id), work_item_ref=state.ref,
            project_id=state.project_id, message=self.dispatch_text(state),
            repository_resource_id=repository_id,
            writable_repository_resource_ids=writable_ids,
            read_only_repository_resource_ids=read_only_repository_resource_ids,
        )
        # A concurrent drain is no proof that the previously observed input
        # matched. Leave admission to a subsequent fresh canonical invocation.
        return outcome

    @_routing_dispatch
    async def dispatch_structured_handoff(
        self,
        state: WorkItemState,
        *,
        source: str,
    ) -> None:
        handoff = state.handoff
        if not state.project_id or not handoff or handoff.status != "pending":
            return
        recipient = self._routed_owner(state, handoff.to_agent)
        if not recipient:
            return
        intent = self._alias_intent(state, handoff.to_agent)
        binding = (self.owner_routing.existing_binding(state, recipient,
            preferred_conversation_id=self.coordination_channel) if intent is not None else
            self.binding_for_agent(recipient, state.project_id,
                preferred_conversation_id=self.coordination_channel))
        if not binding:
            self.append_event(
                {
                    "type": "work_item_handoff_dispatch_skipped",
                    "ref": state.ref,
                    "agent": recipient,
                    "reason": "no_binding",
                    "source": source,
                }
            )
            return
        repository_resource_id, writable_repository_resource_ids = self._dispatch_repository_scope(state)
        pin = (await self.owner_routing.prepare_compatible_pin(state, binding, handoff.to_agent,
            writable_repository_ids=writable_repository_resource_ids)
            if self.owner_routing is not None else None)
        if pin is None or pin.recipient is None:
            binding = await self.replace_nonperforming_thread(binding, source)
        if pin is not None:
            await asyncio.to_thread(self.owner_routing.context_for_pin, pin, binding)
        if self.revalidate_after_thread_replacement or pin is not None:
            try:
                latest = self.get_state(state.ref)
            except HTTPException:
                return
            latest_handoff = latest.handoff
            latest_recipient = (
                self._routed_owner(latest, latest_handoff.to_agent)
                if latest_handoff
                else None
            )
            if (
                not latest_handoff
                or latest_handoff.status != "pending"
                or latest_recipient != recipient
                or latest_handoff.requested_at != handoff.requested_at
            ):
                return
            state = latest
        recipient_thread_id = pin.recipient.thread_id if pin is not None and pin.recipient is not None else binding.thread_id
        dispatch_key = (
            f"handoff-dispatch:{state.ref}:{recipient_thread_id}:"
            f"{recipient}:{handoff.requested_at}"
        )
        if not self.watchdog_dispatch_allowed(dispatch_key):
            return
        self.record_watchdog_dispatch(dispatch_key)
        repository_resource_id, writable_repository_resource_ids = (
            self._dispatch_repository_scope(state)
        )
        result = await self.dispatch_event(
            binding,
            self.dispatch_text(state),
            source,
            work_item_ref=state.ref,
            handoff_requested_at=handoff.requested_at,
            repository_resource_id=repository_resource_id,
            writable_repository_resource_ids=writable_repository_resource_ids,
            **({'owner_routing_pin': pin} if pin is not None else {}),
        )
        if pin is not None and result.get('queued'):
            self.owner_routing.clear_failure(pin)
        self.append_event(
            {
                "type": "work_item_handoff_dispatched",
                "ref": state.ref,
                "thread_id": recipient_thread_id,
                "agent": recipient,
                "source": source,
                "result": result,
            }
        )

    def schedule_structured_handoff_dispatch(
        self,
        state: WorkItemState,
        *,
        source: str,
    ) -> None:
        handoff = state.handoff
        if (
            not state.project_id
            or not handoff
            or handoff.status != "pending"
        ):
            return
        expected_recipient = self.coerce_owner(handoff.to_agent)
        if not expected_recipient:
            return
        expected_requested_at = handoff.requested_at
        ref = state.ref

        async def run() -> None:
            try:
                latest = self.get_state(ref)
            except HTTPException:
                return
            latest_handoff = latest.handoff
            if (
                not latest_handoff
                or latest_handoff.status != "pending"
                or self.coerce_owner(latest_handoff.to_agent)
                != expected_recipient
                or latest_handoff.requested_at
                != expected_requested_at
            ):
                return
            try:
                await self.dispatch_structured_handoff(
                    latest,
                    source=source,
                )
            except Exception as exc:
                self.append_event(
                    {
                        "type": "work_item_handoff_dispatch_failed",
                        "ref": ref,
                        "source": source,
                        "error": self.truncate_text(
                            str(getattr(exc, "detail", exc)),
                            500,
                        ),
                    }
                )

        self.dispatch_coordinator.schedule(
            f"handoff-dispatch:{ref}",
            run,
            revision=self._handoff_revision(state),
            scope=state.project_id,
            timeout_seconds=(
                float(
                    getattr(
                        self.policy,
                        "continuity_dispatch_timeout",
                        lambda: 120.0,
                    )()
                )
            ),
        )

    async def run_handoff_continuity_check(
        self,
        ref: str,
        *,
        expected_recipient: str | None,
        expected_requested_at: float | None,
        source: str,
    ) -> None:
        delay = self.policy.handoff_continuity_delay()
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            state = self.get_state(ref)
        except HTTPException:
            return
        handoff = state.handoff
        if not handoff or handoff.status != "pending":
            return
        recipient = self.coerce_owner(handoff.to_agent)
        if expected_recipient and recipient != expected_recipient:
            return
        if (
            expected_requested_at is not None
            and handoff.requested_at != expected_requested_at
        ):
            return
        if not state.project_id or not recipient:
            return
        intent = self._alias_intent(state, handoff.to_agent)
        if intent is not None:
            recipient = intent.canonical_owner
        binding = (self.owner_routing.existing_binding(state, recipient,
            preferred_conversation_id=self.coordination_channel) if intent is not None else
            self.binding_for_agent(recipient, state.project_id,
                preferred_conversation_id=self.coordination_channel))
        if not binding:
            return
        if (
            self.thread_is_active(binding.thread_id)
            or self.thread_queue_depth(binding.thread_id)
            or self.thread_recently_active(binding.thread_id)
        ):
            return
        if self.owner_routing is not None:
            # Canonical roles and aliases share the original dispatch's exact
            # compatible-recipient witness, including its read-only custody.
            await self.dispatch_structured_handoff(state, source=source)
            return
        binding = await self.replace_nonperforming_thread(binding, source)
        dispatch_key = (
            f"handoff-continuity:{ref}:{binding.thread_id}:"
            f"{recipient}:{handoff.requested_at}"
        )
        if not self.watchdog_dispatch_allowed(dispatch_key):
            return
        self.record_watchdog_dispatch(dispatch_key)
        repository_resource_id, writable_repository_resource_ids = self._dispatch_repository_scope(state)
        result = await self.dispatch_event(
            binding,
            self.dispatch_text(state),
            source,
            work_item_ref=state.ref,
            handoff_requested_at=handoff.requested_at,
            repository_resource_id=repository_resource_id,
            writable_repository_resource_ids=writable_repository_resource_ids,
        )
        self.append_event(
            {
                "type": "work_item_handoff_continuity_dispatched",
                "ref": ref,
                "thread_id": binding.thread_id,
                "agent": recipient,
                "result": result,
            }
        )

    def schedule_handoff_continuity_check(
        self,
        state: WorkItemState,
        *,
        source: str,
    ) -> None:
        handoff = state.handoff
        if not handoff or handoff.status != "pending":
            return
        recipient = self.coerce_owner(handoff.to_agent)
        if not recipient:
            return
        requested_at = handoff.requested_at
        existing = self.handoff_tasks.get(state.ref)
        if existing and not existing.done():
            existing.cancel()

        async def run() -> None:
            try:
                await self.run_handoff_continuity_check(
                    state.ref,
                    expected_recipient=recipient,
                    expected_requested_at=requested_at,
                    source=source,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.append_event(
                    {
                        "type": "handoff_continuity_check_failed",
                        "ref": state.ref,
                        "source": source,
                        "error": self.truncate_text(
                            str(getattr(exc, "detail", exc)),
                            500,
                        ),
                    }
                )
            finally:
                current = self.handoff_tasks.get(state.ref)
                if current is task:
                    self.handoff_tasks.pop(state.ref, None)

        task = asyncio.create_task(run())
        self.handoff_tasks[state.ref] = task

    @_routing_dispatch
    async def dispatch_actionable_owner(
        self,
        state: WorkItemState,
        *,
        source: str,
        actor: str | None = None,
    ) -> dict[str, Any]:
        if state.current_stage == "closed" or state.closed_at:
            return {"status": "closed", "dispatched": False}
        if not self.actionable_owner_stage(state.current_stage):
            return {"status": "not_actionable", "dispatched": False}
        if state.handoff and state.handoff.status == "pending":
            return {"status": "handoff_pending", "dispatched": False}
        owner = self._routed_owner(state, actionable_owner_value(state))
        if not owner or self.coerce_owner(actor) == owner:
            return {"status": "owner_unavailable", "dispatched": False}
        binding = self.responsible_binding(state)
        if not binding:
            return {"status": "binding_unavailable", "dispatched": False}
        explicit_retry = source == 'operator-retry'
        # Capture scalars before recipient lookup or replacement yields:
        # canonical state may mutate while this invocation is suspended.
        expected_retry_attempt = (
            state.execution.retry.attempt if explicit_retry else None
        )
        expected_stage = state.current_stage
        repository_resource_id, writable_repository_resource_ids = (
            self._dispatch_repository_scope(state)
        )
        pin = (await self.owner_routing.prepare_compatible_pin(
            state, binding, actionable_owner_value(state),
            writable_repository_ids=writable_repository_resource_ids)
            if self.owner_routing is not None else None)
        selected = pin is not None and pin.recipient is not None
        target_thread_id = pin.recipient.thread_id if selected else binding.thread_id
        readonly_ids = ()
        if selected:
            scope = await asyncio.to_thread(self.owner_routing.repository_scope_for_pin, pin, binding)
            readonly_ids = scope.read_only_repository_ids
        if self.thread_is_active(target_thread_id) and (selected or not explicit_retry):
            return {
                "status": "already_active",
                "dispatched": False,
                "owner": owner,
                "thread_id": target_thread_id,
            }
        if self.thread_queue_depth(target_thread_id) and (selected or not explicit_retry):
            return {
                "status": self._owner_queue_outcome(state, target_thread_id,
                    read_only_repository_resource_ids=readonly_ids),
                "dispatched": False,
                "owner": owner,
                "thread_id": target_thread_id,
            }
        if not selected:
            binding = await self.replace_nonperforming_thread(binding, source)
            target_thread_id = binding.thread_id
        if pin is not None:
            await asyncio.to_thread(self.owner_routing.context_for_pin, pin, binding)
        if self.revalidate_after_thread_replacement or explicit_retry or pin is not None:
            try:
                latest = self.get_state(state.ref)
            except HTTPException:
                return {"status": "work_item_unavailable", "dispatched": False}
            # Retry identity is mandatory even on the historical adapter, whose
            # optional owner/stage revalidation remains disabled.
            if (
                explicit_retry
                and latest.execution.retry.attempt != expected_retry_attempt
            ):
                return {"status": "stale_state", "dispatched": False}
        if self.revalidate_after_thread_replacement or pin is not None:
            latest_owner = self._routed_owner(latest,
                actionable_owner_value(latest)
            )
            if (
                latest.current_stage == "closed"
                or latest.closed_at
                or not self.actionable_owner_stage(latest.current_stage)
                or (
                    latest.handoff
                    and latest.handoff.status == "pending"
                )
                or latest_owner != owner
                or latest.current_stage != expected_stage
            ):
                return {"status": "stale_state", "dispatched": False}
            state = latest
        dispatch_key = (
            f"work-item-owner-progress:{state.ref}:{target_thread_id}:"
            f"{owner}:{state.current_stage}"
        )
        if explicit_retry:
            dispatch_key += f':operator-retry:{expected_retry_attempt}'
        if not self.watchdog_dispatch_allowed(dispatch_key):
            return {
                "status": "duplicate",
                "dispatched": False,
                "owner": owner,
                "thread_id": target_thread_id,
                "dispatch_key": dispatch_key,
            }
        self.record_watchdog_dispatch(dispatch_key)
        repository_resource_id, writable_repository_resource_ids = (
            self._dispatch_repository_scope(state)
        )
        result = await self.dispatch_event(
            binding,
            self.dispatch_text(state),
            source,
            work_item_ref=state.ref,
            **({'owner_routing_pin': pin} if pin is not None else {}),
            repository_resource_id=repository_resource_id,
            writable_repository_resource_ids=(
                writable_repository_resource_ids
            ),
            require_idle=selected or not explicit_retry,
        )
        if pin is not None and result.get('queued'):
            self.owner_routing.clear_failure(pin)
        skipped = str(result.get("skipped") or "").strip()
        if skipped:
            self.append_event(
                {
                    "type": "work_item_owner_progress_dispatch_skipped",
                    "ref": state.ref,
                    "thread_id": target_thread_id,
                    "agent": owner,
                    "current_stage": state.current_stage,
                    "source": source,
                    "actor": actor,
                    "reason": skipped,
                }
            )
            return {
                "status": skipped,
                "dispatched": False,
                "owner": owner,
                "thread_id": target_thread_id,
                "dispatch_key": dispatch_key,
                "dispatch": result,
            }
        self.append_event(
            {
                "type": "work_item_owner_progress_dispatched",
                "ref": state.ref,
                "thread_id": target_thread_id,
                "agent": owner,
                "current_stage": state.current_stage,
                "source": source,
                "actor": actor,
                "result": result,
            }
        )
        return {
            "status": "dispatched",
            "dispatched": True,
            "owner": owner,
            "thread_id": target_thread_id,
            "dispatch_key": dispatch_key,
            "dispatch": result,
        }

    @staticmethod
    def _steer_conflict(code: str, message: str) -> HTTPException:
        return HTTPException(
            status_code=409,
            detail={"code": code, "message": message},
        )

    async def steer_actionable_owner(
        self,
        state: WorkItemState,
        *,
        expected_owner: str,
        expected_thread_id: str,
        idempotency_key: str,
        actor: str,
        source: str,
        expected_definition=None,
    ) -> dict[str, Any]:
        """Wake only the exact canonical owner binding named by the caller."""
        lock = self.owner_steer_locks.setdefault(state.ref, asyncio.Lock())
        async with lock:
            try:
                latest = self.get_state(state.ref)
            except HTTPException as exc:
                raise self._steer_conflict(
                    "work_item_unavailable",
                    "work item became unavailable before owner steering",
                ) from exc
            return await self._steer_actionable_owner_locked(
                latest,
                expected_owner=expected_owner,
                expected_thread_id=expected_thread_id,
                idempotency_key=idempotency_key,
                actor=actor,
                source=source,
                expected_definition=expected_definition,
            )

    async def _steer_actionable_owner_locked(
        self,
        state: WorkItemState,
        *,
        expected_owner: str,
        expected_thread_id: str,
        idempotency_key: str,
        actor: str,
        source: str,
        expected_definition=None,
    ) -> dict[str, Any]:
        owner = self._routed_owner(state, actionable_owner_value(state))
        intent = self._alias_intent(state, actionable_owner_value(state))
        if intent is not None and expected_definition != intent.definition:
            raise self._steer_conflict('owner_routing_definition_changed',
                'Read current owner dispatch and supply its exact expected_definition before steering an alias.')
        expected = self.coerce_owner(expected_owner)
        if not owner or owner != expected:
            raise self._steer_conflict(
                "work_item_owner_changed",
                "expected owner does not match the canonical current owner",
            )
        _, binding, blocked_reason = self._owner_dispatch_resolution(state)
        if blocked_reason is not None:
            messages = {
                "work_item_closed": "closed work cannot be steered",
                "work_item_not_actionable": (
                    "work item is not in an actionable owner stage"
                ),
                "work_item_handoff_pending": (
                    "pending handoff must be resolved before owner steering"
                ),
                "work_item_owner_unavailable": (
                    "canonical current owner is unavailable"
                ),
                "work_item_owner_binding_unavailable": (
                    "canonical owner has no same-project thread binding"
                ),
                "work_item_owner_project_mismatch": (
                    "canonical owner thread is outside the work-item project"
                ),
            }
            raise self._steer_conflict(
                blocked_reason,
                messages.get(blocked_reason, 'Owner routing is blocked; inspect current binding, profile and definition.'),
            )
        assert binding is not None
        if binding.thread_id != expected_thread_id:
            raise self._steer_conflict(
                "work_item_owner_thread_changed",
                "expected thread does not match the canonical owner binding",
            )

        result = await self.dispatch_actionable_owner(
            state,
            source=source,
            actor=actor,
        )
        return {
            **result,
            "work_item_ref": state.ref,
            "idempotency_key": idempotency_key,
        }

    def schedule_actionable_owner_dispatch(
        self,
        state: WorkItemState,
        *,
        source: str,
        actor: str | None = None,
    ) -> None:
        if (
            not state.project_id
            or state.current_stage == "closed"
            or state.closed_at
            or not self.actionable_owner_stage(state.current_stage)
        ):
            return
        if state.handoff and state.handoff.status == "pending":
            return
        expected_owner = self.coerce_owner(
            actionable_owner_value(state)
        )
        if not expected_owner:
            return
        expected_stage = state.current_stage
        explicit_retry = source == 'operator-retry'
        expected_retry_attempt = state.execution.retry.attempt
        ref = state.ref

        async def run() -> None:
            try:
                latest = self.get_state(ref)
            except HTTPException:
                return
            current_owner = self.coerce_owner(
                actionable_owner_value(latest)
            )
            if (
                latest.current_stage == "closed"
                or latest.closed_at
                or not self.actionable_owner_stage(
                    latest.current_stage
                )
                or (
                    latest.handoff
                    and latest.handoff.status == "pending"
                )
                or current_owner != expected_owner
                or latest.current_stage != expected_stage
                or (explicit_retry and latest.execution.retry.attempt != expected_retry_attempt)
            ):
                return
            try:
                await self.dispatch_actionable_owner(
                    latest,
                    source=source,
                    actor=actor,
                )
            except Exception as exc:
                self.append_event(
                    {
                        "type": (
                            "work_item_owner_progress_dispatch_failed"
                        ),
                        "ref": ref,
                        "source": source,
                        "actor": actor,
                        "error": self.truncate_text(
                            str(getattr(exc, "detail", exc)),
                            500,
                        ),
                    }
                )

        self.dispatch_coordinator.schedule(
            f"owner-dispatch:{ref}" + (":operator-retry" if explicit_retry else ""),
            run,
            revision=self._owner_revision(state) + (f"|operator-retry:{expected_retry_attempt}" if explicit_retry else ""),
            scope=state.project_id,
            timeout_seconds=(
                float(
                    getattr(
                        self.policy,
                        "continuity_dispatch_timeout",
                        lambda: 120.0,
                    )()
                )
            ),
        )

    async def run_actionable_owner_continuity_check(
        self,
        ref: str,
        *,
        expected_owner: str | None,
        expected_stage: str | None,
        source: str,
    ) -> None:
        delay = self.policy.actionable_owner_continuity_delay()
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            state = self.get_state(ref)
        except HTTPException:
            return
        if state.current_stage == "closed" or state.closed_at:
            return
        if not self.actionable_owner_stage(state.current_stage):
            return
        current_owner = self.coerce_owner(
            actionable_owner_value(state)
        )
        if expected_owner and current_owner != expected_owner:
            return
        if expected_stage and state.current_stage != expected_stage:
            return
        await self.dispatch_actionable_owner(
            state,
            source=source,
            actor=None,
        )

    def schedule_actionable_owner_continuity_check(
        self,
        state: WorkItemState,
        *,
        source: str,
    ) -> None:
        if state.current_stage == "closed" or state.closed_at:
            return
        if not self.actionable_owner_stage(state.current_stage):
            return
        owner = self.coerce_owner(actionable_owner_value(state))
        if not owner:
            return
        existing = self.actionable_owner_tasks.get(state.ref)
        if existing and not existing.done():
            existing.cancel()

        async def run() -> None:
            try:
                await self.run_actionable_owner_continuity_check(
                    state.ref,
                    expected_owner=owner,
                    expected_stage=state.current_stage,
                    source=source,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.append_event(
                    {
                        "type": "actionable_owner_continuity_check_failed",
                        "ref": state.ref,
                        "source": source,
                        "error": self.truncate_text(
                            str(getattr(exc, "detail", exc)),
                            500,
                        ),
                    }
                )
            finally:
                current = self.actionable_owner_tasks.get(state.ref)
                if current is task:
                    self.actionable_owner_tasks.pop(state.ref, None)

        task = asyncio.create_task(run())
        self.actionable_owner_tasks[state.ref] = task

    async def stop(self) -> None:
        await self.dispatch_coordinator.stop()
        tasks = [
            *self.actionable_owner_tasks.values(),
            *self.handoff_tasks.values(),
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.actionable_owner_tasks.clear()
        self.handoff_tasks.clear()


def build_work_item_continuity_compatibility_service(
    host: Any, *, owner_routing=None,
) -> WorkItemContinuityService:
    """Build a dynamic adapter for historical direct-call entrypoints.

    Every collaborator resolves through the host at call time so supported
    tests/integrations can patch one historical seam. Canonical application
    paths use the explicitly composed WorkItemContinuityService instead. The
    production historical surface receives that same canonical owner router;
    only standalone legacy hosts may omit it.
    """

    policy = SimpleNamespace(
        actionable_owner_continuity_delay=(
            lambda: host._actionable_owner_continuity_delay_seconds()
        ),
        handoff_continuity_delay=(
            lambda: host._handoff_continuity_delay_seconds()
        ),
        continuity_dispatch_timeout=(
            lambda: float(
                getattr(
                    host,
                    "CONTINUITY_DISPATCH_TIMEOUT_SECONDS",
                    120.0,
                )
            )
        ),
        background_task_max_concurrency=(
            lambda: int(
                getattr(
                    host,
                    "BACKGROUND_TASK_MAX_CONCURRENCY",
                    16,
                )
            )
        ),
        continuity_per_project_concurrency=(
            lambda: int(
                getattr(
                    host,
                    "CONTINUITY_PER_PROJECT_CONCURRENCY",
                    4,
                )
            )
        ),
    )
    service = WorkItemContinuityService(
        policy=policy,
        get_state=lambda ref: host._work_item_state(ref),
        coerce_owner=lambda owner: host._coerce_owner(owner),
        binding_for_agent=lambda *args, **kwargs: host._binding_for_agent(
            *args,
            **kwargs,
        ),
        replace_nonperforming_thread=(
            lambda binding, reason: host._replace_nonperforming_thread_if_needed(
                binding,
                reason,
            )
        ),
        dispatch_event=lambda binding, text, source, **kwargs: host._dispatch_event_to_binding(
            binding,
            text,
            source,
            **kwargs,
        ),
        dispatch_text=lambda state: host._work_item_dispatch_text(state),
        append_event=lambda event: host._append_bot_event(event),
        truncate_text=lambda value, limit: host._truncate_text(value, limit),
        thread_is_active=lambda thread_id: host._thread_is_active(thread_id),
        thread_queue_depth=lambda thread_id: host._thread_queue_depth(thread_id),
        thread_queue=(lambda thread_id: host._thread_queue(thread_id)) if hasattr(host, '_thread_queue') else None,
        thread_recently_active=lambda thread_id: host._thread_recently_active(
            thread_id
        ),
        watchdog_dispatch_allowed=lambda key: host._watchdog_dispatch_allowed(
            key
        ),
        record_watchdog_dispatch=lambda key: host._record_watchdog_dispatch(
            key
        ),
        coordination_channel=host.HANDOFF_COORDINATION_CHANNEL,
        revalidate_after_thread_replacement=owner_routing is not None,
    )
    service.owner_routing = owner_routing
    return service
