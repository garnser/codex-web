"""Resolve alias recipients through existing bindings, bootstraps and profiles.

No binding/profile is created here. The queued request still crosses canonical
identity, RoleAuthority, worker lease and native-start admission boundaries.
"""
from __future__ import annotations

import time

from fastapi import HTTPException

from codex_web.owner_routing import OwnerRoutingError, OwnerRoutingPin, OwnerRoutingBlocker, normalized_owner
from codex_web.identity import TenantScope
from codex_web.services.work_item_dependencies import actionable_owner_value


class OwnerRoutingService:
    def __init__(self, *, routing, get_state, projects, load_bindings, binding_prefix,
                 bootstraps, assignments, profiles, control_actor, actor_resolver,
                 replacement_resolver, update_state=None, append_event=None, preflight=None):
        self.routing = routing
        self.get_state = get_state
        self.projects = projects
        self.load_bindings = load_bindings
        self.binding_prefix = binding_prefix
        self.bootstraps = bootstraps
        self.assignments = assignments
        self.profiles = profiles
        self.control_actor = control_actor
        self.actor_resolver = actor_resolver
        self.replacement_resolver = replacement_resolver
        self.update_state, self.append_event = update_state, append_event
        self.preflight = preflight

    def require_resolved_originals(self, state, *, excluding_attempt_id=None, pin=None):
        if self.preflight is None:
            raise OwnerRoutingError('owner_routing_retained_index_unavailable')
        if excluding_attempt_id is not None:
            current = self.preflight.get(excluding_attempt_id)
            if (current is None or pin is None
                    or (current.queued_turn_snapshot or {}).get('owner_routing_pin') != pin.model_dump(mode='json')
                    or (current.organization_id, current.workspace_id, current.project_id)
                    != (state.organization_id, state.workspace_id, state.project_id)):
                raise OwnerRoutingError('owner_routing_retained_intent_mismatch')
        try:
            unresolved = self.preflight.unresolved_work_item(organization_id=state.organization_id,
                workspace_id=state.workspace_id, project_id=state.project_id, ref=state.ref,
                excluding_attempt_id=excluding_attempt_id)
        except (LookupError, ValueError, RuntimeError) as exc:
            known = {'owner_routing_retained_index_unavailable', 'owner_routing_retained_lookup_truncated'}
            raise OwnerRoutingError(str(exc) if str(exc) in known else 'owner_routing_retained_lookup_unavailable') from exc
        if unresolved is not None:
            raise OwnerRoutingError('owner_routing_native_outcome_unresolved')

    def record_failure(self, state, error):
        if self.update_state is None:
            return
        definition = None
        try:
            routing = self.snapshot(state)
            definition = routing.definition if routing is not None else None
        except OwnerRoutingError:
            pass
        owner = normalized_owner(state.handoff.to_agent if state.handoff and state.handoff.status == 'pending'
            else actionable_owner_value(state))
        blocker = OwnerRoutingBlocker(code=str(error), configured_owner=owner,
            definition=definition, recorded_at=time.time())
        changed = False
        def update(current):
            nonlocal changed
            changed = False
            if (current is None or (current.organization_id, current.workspace_id, current.project_id)
                    != (state.organization_id, state.workspace_id, state.project_id)
                    or current.updated_at != state.updated_at):
                return current
            previous = current.owner_routing_blocker
            if previous is not None and previous.model_dump(exclude={'recorded_at'}) == blocker.model_dump(exclude={'recorded_at'}):
                return current
            current.owner_routing_blocker = blocker
            changed = True
            return current
        self.update_state(state.ref, update)
        if changed and self.append_event is not None:
            from codex_web.models import WorkItemEvent
            self.append_event(WorkItemEvent(ref=state.ref, event_type='owner_routing_blocked',
                created_at=blocker.recorded_at, source='canonical-owner-routing',
                payload={'code': blocker.code, 'configured_owner': owner,
                    'definition_record_id': definition.record_id if definition is not None else None}))

    def snapshot(self, state):
        return self.routing(state)

    def clear_failure(self, pin):
        if self.update_state is None:
            return
        def update(current):
            if current is None or current.owner_routing_blocker is None:
                return current
            try:
                self.validate_intent(pin, current)
            except OwnerRoutingError:
                return current
            current.owner_routing_blocker = None
            return current
        self.update_state(pin.work_item_ref, update)

    def existing_binding(self, state, owner, *, preferred_conversation_id=None):
        # Alias routing never uses fuzzy thread names or clones a binding into
        # another conversation. Only an existing exact canonical role qualifies.
        candidates = [item for item in self.load_bindings()
            if item.project_id == state.project_id and not item.is_master
            and normalized_owner(self.binding_prefix(item)) == owner and item.thread_id]
        preferred = [item for item in candidates
            if item.external_conversation_id == preferred_conversation_id]
        candidates = preferred or candidates
        threads = {item.thread_id for item in candidates}
        if not candidates:
            raise OwnerRoutingError('work_item_owner_binding_unavailable')
        if len(threads) != 1:
            raise OwnerRoutingError('work_item_owner_binding_ambiguous')
        # Multiple provider reply bindings for the same thread are equivalent
        # executors; retain the selected reply binding in the original request.
        return sorted(candidates, key=lambda item: item.id)[0]

    def executor(self, state, binding, owner):
        try:
            project = self.projects.get(state.project_id, scope=TenantScope(
                organization_id=state.organization_id, workspace_id=state.workspace_id))
        except (LookupError, ValueError, RuntimeError, HTTPException) as exc:
            raise OwnerRoutingError('owner_routing_scope_mismatch') from exc
        if ((project.organization_id, project.workspace_id, project.id)
                != (state.organization_id, state.workspace_id, state.project_id)
                or binding.project_id != project.id
                or normalized_owner(self.binding_prefix(binding)) != owner):
            raise OwnerRoutingError('owner_routing_scope_mismatch')
        lookup_actor = self.control_actor()
        if (lookup_actor.organization_id, lookup_actor.workspace_id) != (
                state.organization_id, state.workspace_id):
            raise OwnerRoutingError('owner_executor_actor_scope_mismatch')
        try:
            bootstrap = self.bootstraps.get_by_thread(binding.thread_id, lookup_actor)
            assignment = self.assignments(bootstrap.assignment_id)
            if (assignment is None or assignment.agent_profile is None
                    or (assignment.organization_id, assignment.workspace_id, assignment.project_id)
                    != (state.organization_id, state.workspace_id, state.project_id)):
                raise OwnerRoutingError('owner_executor_bootstrap_unverified')
            original = assignment.agent_profile
            actor = self.actor_resolver(assignment.created_by, project)
            if (actor.identity_id != assignment.created_by
                    or (actor.organization_id, actor.workspace_id)
                    != (state.organization_id, state.workspace_id)):
                raise OwnerRoutingError('owner_executor_actor_scope_mismatch')
            profile, _ = self.profiles.resolve_for_execution(original.profile_id, actor=actor,
                project_id=project.id)
            if (profile.revision != original.profile_revision or profile.record_id != original.profile_record_id
                    or profile.role_id != owner or original.role_id != owner
                    or (profile.organization_id, profile.workspace_id)
                    != (state.organization_id, state.workspace_id)):
                raise OwnerRoutingError('owner_executor_profile_changed')
            return profile, actor
        except OwnerRoutingError:
            raise
        except (LookupError, ValueError, RuntimeError, HTTPException) as exc:
            raise OwnerRoutingError('owner_executor_profile_unavailable') from exc

    def pin(self, state, binding, configured_owner):
        routing = self.snapshot(state)
        intent = routing.binding(configured_owner) if routing is not None else None
        if intent is None:
            return None
        self.require_resolved_originals(state)
        self.executor(state, binding, intent.canonical_owner)
        handoff = state.handoff
        pending = handoff is not None and handoff.status == 'pending'
        if pending and (handoff.owner_routing != intent
                or normalized_owner(handoff.to_agent) != intent.configured_owner
                or (handoff.delivery is not None and handoff.delivery.status == 'delivered')):
            # Do not attach a new interpretation to a retained/possibly started
            # legacy handoff. A governed new intent must identify its recipient.
            raise OwnerRoutingError('owner_alias_intent_unbound_or_changed')
        return OwnerRoutingPin(binding=intent, work_item_ref=state.ref, stage=state.current_stage,
            retry_attempt=state.execution.retry.attempt,
            handoff_requested_at=handoff.requested_at if pending else None,
            bot_binding_id=binding.id, original_thread_id=binding.thread_id)

    def context_for_pin(self, pin, binding):
        state = self.get_state(pin.work_item_ref)
        self.validate_intent(pin, state)
        self.require_resolved_originals(state)
        self.validate_thread(pin, state, binding)
        return self.executor(state, binding, pin.binding.canonical_owner)

    def validate_thread(self, pin, state, binding):
        if binding.thread_id == pin.original_thread_id:
            if binding.id != pin.bot_binding_id:
                raise OwnerRoutingError('owner_executor_binding_changed')
        else:
            replacement = self.replacement_resolver(pin.original_thread_id,
                project_id=state.project_id, organization_id=state.organization_id,
                workspace_id=state.workspace_id)
            if replacement != binding.thread_id:
                raise OwnerRoutingError('owner_executor_replacement_unverified')

    def validate_intent(self, pin, state):
        pin.binding.for_work_item(state)
        if (state.ref != pin.work_item_ref or state.closed_at or state.current_stage == 'closed'
                or state.current_stage != pin.stage or state.execution.retry.attempt != pin.retry_attempt):
            raise OwnerRoutingError('owner_routing_work_item_changed')
        routing = self.snapshot(state)
        if routing is None or routing.binding(pin.binding.configured_owner) != pin.binding:
            raise OwnerRoutingError('owner_routing_definition_changed')
        if pin.handoff_requested_at is not None:
            handoff = state.handoff
            if (handoff is None or handoff.status != 'pending'
                    or handoff.requested_at != pin.handoff_requested_at
                    or handoff.owner_routing != pin.binding
                    or normalized_owner(handoff.to_agent) != pin.binding.configured_owner
                    or (handoff.delivery is not None and handoff.delivery.status == 'delivered')):
                raise OwnerRoutingError('owner_routing_handoff_changed')
        elif ((state.handoff is not None and state.handoff.status == 'pending')
                or normalized_owner(actionable_owner_value(state)) != pin.binding.configured_owner):
            raise OwnerRoutingError('owner_routing_owner_changed')

    def validate_native(self, pin, *, thread_id, project, work_item_ref, handoff_requested_at,
                        agent_profile_id, agent_profile_revision, agent_profile_actor_id,
                        queued_admission_attempt_id=None):
        state = self.get_state(pin.work_item_ref)
        self.validate_intent(pin, state)
        self.require_resolved_originals(state, excluding_attempt_id=queued_admission_attempt_id, pin=pin)
        if ((project.organization_id, project.workspace_id, project.id)
                != (state.organization_id, state.workspace_id, state.project_id)
                or work_item_ref != pin.work_item_ref or handoff_requested_at != pin.handoff_requested_at):
            raise OwnerRoutingError('owner_routing_request_mismatch')
        candidates = [item for item in self.load_bindings() if item.thread_id == thread_id
            and item.project_id == state.project_id and not item.is_master
            and normalized_owner(self.binding_prefix(item)) == pin.binding.canonical_owner]
        if thread_id == pin.original_thread_id:
            candidates = [item for item in candidates if item.id == pin.bot_binding_id]
        if not candidates:
            raise OwnerRoutingError('owner_executor_binding_changed')
        binding = sorted(candidates, key=lambda item: item.id)[0]
        self.validate_thread(pin, state, binding)
        profile, actor = self.executor(state, binding, pin.binding.canonical_owner)
        if (profile.profile_id, profile.revision, actor.identity_id) != (
                agent_profile_id, agent_profile_revision, agent_profile_actor_id):
            raise OwnerRoutingError('owner_executor_profile_changed')
