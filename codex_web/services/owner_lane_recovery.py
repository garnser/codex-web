"""Bounded current-work recovery through the existing retained turn domain."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from types import SimpleNamespace

from fastapi import HTTPException

from codex_web.models import OwnerLaneRecoveryContext, QueuedTurn
from codex_web.services.work_item_dependencies import actionable_owner_value, work_item_repository_scope


class OwnerLaneRecoveryService:
    SOURCE = 'owner-lane-recovery'
    TERMINAL = frozenset({'lost', 'failed', 'cancelled'})

    def __init__(self, *, execution, preflight, dispatch, get_state,
                 load_bindings, binding_owner, controller, project_enabled, goal_for=None, resolve_binding=None):
        self.execution, self.preflight = execution, preflight
        self.dispatch, self.get_state = dispatch, get_state
        self.load_bindings, self.binding_owner = load_bindings, binding_owner
        self.controller, self.project_enabled, self.goal_for = controller, project_enabled, goal_for
        self.resolve_binding = resolve_binding or (lambda binding: binding)

    @staticmethod
    def revision(state):
        # No wall-clock window or unrelated display/writeback update creates a
        # new model request. Only changed canonical work evidence does.
        keys = ('ref', 'organization_id', 'workspace_id', 'project_id', 'goal_id',
                'current_owner', 'current_stage', 'next_owner', 'next_action',
                'closed_at', 'resource_ids', 'handoff', 'source_identity',
                'verified_artifact_relations', 'blocker', 'blocking_findings',
                'implementation_owner', 'validation_owner', 'release_owner', 'mr_refs')
        raw = state.model_dump(mode='json')
        context = {key: raw.get(key) for key in keys}
        _, writable = work_item_repository_scope(state)
        context['writable_repository_resource_ids'] = list(writable)
        context['retry_attempt'] = state.execution.retry.attempt
        # Runtime delivery/usage/workspace bookkeeping and checkpoint IDs must
        # not mint another model budget. Current canonical business intent and
        # provider/artifact revisions above supply changed-evidence wakeups.
        return hashlib.sha256(json.dumps(context, sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()

    def terminal(self, binding):
        binding = self.resolve_binding(binding)
        bootstrap = self.execution._bootstrap_binding_for_thread(binding.thread_id)
        if bootstrap is None:
            return None
        assignment = self.execution._assignment_record(bootstrap.assignment_id)
        if (assignment is None or str(assignment.status) not in self.TERMINAL
                or assignment.lease is not None or assignment.project_id != binding.project_id
                or assignment.execution_id != bootstrap.execution_id
                or assignment.execution_workspace_id != bootstrap.execution_workspace_id):
            return None
        if self.execution.thread_has_inflight_agent_runtime_assignment(binding.thread_id):
            return None
        return assignment

    def owns(self, binding, state):
        return bool(state is not None and not state.closed_at and state.current_stage != 'closed'
            and state.project_id == binding.project_id
            and str(actionable_owner_value(state) or '').strip().lower()
                == str(self.binding_owner(binding) or '').strip().lower())

    def candidate(self, binding, state):
        binding = self.resolve_binding(binding)
        if not self.owns(binding, state) or self.terminal(binding) is None:
            return False
        # Fresh retained/in-flight requests already own the lane. Historical
        # blocked or uncertain originals are evidence, not schedulable work.
        for queued in self.dispatch.queue_policy.queue(binding.thread_id):
            attempt = self.preflight.store.get('preflight-queued-' + queued.id)
            if attempt is None or attempt.status in {'queued', 'admitting', 'retrying'}:
                return False
        return True

    @staticmethod
    def refuse(code, message):
        # This local exception is a pre-write admission witness, not a native
        # error-string inference. Runtime retains the original exactly.
        from codex_web.runtime.execution import ExecutionPrestartRefusal
        raise ExecutionPrestartRefusal(status_code=409,
            detail={'code': code, 'message': message, 'retryable': False})

    @classmethod
    def request_id(cls, binding, state):
        key = hashlib.sha256(json.dumps([state.organization_id, state.workspace_id,
            binding.id, state.ref, cls.revision(state)], separators=(',', ':')).encode()).hexdigest()
        return 'owner-recovery-' + key

    def select_current_work(self, binding, states):
        """Bounded deterministic selection; an old held item cannot monopolize a lane."""
        # Prefer existing open MRs and their correlated work, preserving the
        # caller's deterministic age/stage ordering within each group. Verified
        # closed/merged relations cannot be overridden by stale legacy refs.
        def has_open_mr(state):
            if state.verified_artifact_relations is not None:
                return any(relation.state == 'opened' for relation in state.verified_artifact_relations)
            return bool(state.mr_refs or state.kind == 'merge_request'
                or state.artifact_state == 'merge_request')
        ordered = sorted(states, key=lambda state: not has_open_mr(state))
        for state in ordered[:32]:
            if not self.owns(binding, state):
                continue
            if self.preflight.store.get('preflight-queued-' + self.request_id(binding, state)) is not None:
                continue
            try:
                originals = self.preflight.store.for_work_item(organization_id=state.organization_id,
                    workspace_id=state.workspace_id, project_id=state.project_id, ref=state.ref)
            except (ValueError, LookupError, RuntimeError):
                # Retain a visible blocker through ordinary admitted recovery;
                # an unavailable index never proves independent current work.
                return state
            if any(original.queued_start_receipt is not None
                    or original.status in {'queued', 'admitting', 'retrying', 'reconciliation_required'}
                    for original in originals):
                continue
            return state
        return None

    def observe_teardown(self, assignment):
        try:
            _, session = self.execution._session_for_assignment(assignment.id)
        except HTTPException:
            return None
        local = session.status()
        allowed_fences = {assignment.fence}
        if str(assignment.status) == 'cancelled':
            allowed_fences.add(assignment.fence - 1)
        if (local.running or local.pending_executor_work != 0
                or local.assignment_id != assignment.id
                or local.worker_id != assignment.assigned_worker_id
                or local.fence not in allowed_fences):
            return None
        return local.fence, time.time()

    def validate_queued(self, queued):
        context = queued.owner_lane_recovery
        if context is None or queued.source != self.SOURCE:
            self.refuse('owner_recovery_provenance_missing', 'Current-work recovery provenance is unavailable.')
        project = self.dispatch.projects.get(queued.project_id)
        matches = [self.resolve_binding(b) for b in self.load_bindings()
            if b.id == context.bot_binding_id and b.project_id == queued.project_id]
        matches = [b for b in matches if b.thread_id == queued.thread_id]
        state = self.get_state(queued.work_item_ref)
        if len(matches) != 1 or not self.owns(matches[0], state) or self.revision(state) != context.work_revision:
            self.refuse('owner_recovery_work_changed', 'Current work or its responsible thread changed; action owner must reconcile this retained recovery.')
        if (project.organization_id, project.workspace_id) != (state.organization_id, state.workspace_id):
            self.refuse('owner_recovery_scope_changed', 'Current work no longer belongs to the admitted tenant and Project.')
        resolved = self.dispatch.agent_profile_resolver(matches[0])
        if resolved is None:
            self.refuse('owner_recovery_profile_unavailable', 'The current owner has no eligible Agent Profile.')
        profile, actor = resolved
        if (profile.profile_id, profile.revision, actor.identity_id) != (
                queued.agent_profile_id, queued.agent_profile_revision, queued.agent_profile_actor_id):
            self.refuse('owner_recovery_profile_changed', 'The current owner Profile or actor changed.')
        if self.execution.automation_wait_reason(queued.source) or not self.project_enabled(project.id):
            self.refuse('owner_recovery_paused', 'Automated recovery is paused for this Project.')
        control = self.controller.store.control()
        resources = tuple(dict.fromkeys((*state.resource_ids, *queued.writable_repository_resource_ids,
            *queued.read_only_repository_resource_ids)))
        payload = {'goal_id': state.goal_id, 'project_id': state.project_id, 'work_item_ref': state.ref}
        if (control.mode != 'active' or control.dry_run or control.simulation
                or str(control.policy.level) in {'observe', 'recommend', 'prepare'}
                or self.controller._scoped_pause_reason(control, actor=actor, project_id=project.id, resource_ids=resources)
                or self.controller._exclusive_goal_scope_reason(control, SimpleNamespace(payload=payload))):
            self.refuse('owner_recovery_paused', 'Current autonomy controls do not admit this recovery.')
        if self.controller.policy is not None:
            effective, _ = self.controller.policy.effective(actor=actor, project_id=project.id,
                action_id=None, now=time.time())
            if str(effective.level) in {'observe', 'recommend', 'prepare'}:
                self.refuse('owner_recovery_paused', 'Current scoped autonomy policy does not admit worker recovery.')
        if state.goal_id and self.goal_for is not None:
            goal = self.goal_for(state)
            if str(goal.status) != 'active':
                self.refuse('owner_recovery_goal_inactive', 'The current Goal is not active.')
        failed = self.execution._assignment_record(context.failed_assignment_id)
        if (failed is None or str(failed.status) not in self.TERMINAL or failed.lease is not None
                or failed.fence != context.failed_fence
                or (failed.organization_id, failed.workspace_id, failed.project_id)
                    != (state.organization_id, state.workspace_id, state.project_id)):
            self.refuse('owner_recovery_custody_changed', 'The observed failed assignment is no longer terminal and fenced in this scope.')
        if str(failed.status) == 'cancelled':
            self.refuse('owner_recovery_cancelled', 'Canonical custody was explicitly cancelled. Automatic continuation must not undo that resource release; a new authorized request must establish current custody. Action owner: ' + context.action_owner)
        try:
            _, session = self.execution._session_for_assignment(failed.id)
        except HTTPException:
            session = None
        if session is not None:
            local = session.status()
            if local.running or local.pending_executor_work != 0:
                self.refuse('owner_recovery_teardown_unsettled', 'The failed runner process or executor work has not settled.')
        # The server captures this exact process/fence observation BEFORE any
        # new custody I/O and retains it in the immutable original snapshot.
        # Ordinary rollover may then remove the old manager handle; absence
        # alone is never used as a substitute for that earlier positive proof.
        allowed_fences = {failed.fence}
        if str(failed.status) == 'cancelled':
            allowed_fences.add(failed.fence - 1)
        if (context.teardown_observed_at is None or context.teardown_observed_at > queued.created_at
                or context.observed_process_fence not in allowed_fences):
            self.refuse('owner_recovery_process_unverified', 'Terminal assignment has no retained local process-exit observation. Action owner: ' + context.action_owner)
        reserved = self.preflight.store.get('preflight-queued-' + queued.id)
        snapshot = reserved.queued_turn_snapshot if reserved is not None else None
        original = QueuedTurn.model_validate(snapshot) if snapshot else None
        if original is None or original.model_dump(exclude={'attempts'}) != queued.model_dump(exclude={'attempts'}):
            self.refuse('owner_recovery_provenance_changed', 'The recovery request differs from its retained original.')
        runtime = getattr(failed, 'runtime_binding', None)
        if runtime is not None and (runtime.provider_id, runtime.runtime_id) != ('openai', 'codex'):
            self.refuse('owner_recovery_transport_unsupported', 'Automatic owner recovery requires the guarded Codex native-start capture transport.')
        # An unresolved delivery for THIS work is not an independent new task.
        # Other historical originals on the thread remain untouched.
        try:
            originals = self.preflight.store.for_work_item(organization_id=state.organization_id,
                workspace_id=state.workspace_id, project_id=state.project_id, ref=state.ref,
                excluding_attempt_id='preflight-queued-' + queued.id)
        except (ValueError, LookupError, RuntimeError):
            self.refuse('owner_recovery_retained_index_unavailable', 'The canonical retained-input index cannot establish independent current work; inspect it before recovery.')
        if any(original.status == 'reconciliation_required' and original.queued_start_receipt is None
                for original in originals):
            self.refuse('owner_recovery_original_unresolved', 'Resolve the retained native outcome for this Work Item before automatic continuation. Action owner: ' + context.action_owner)
        if any(original.status in {'queued', 'admitting', 'retrying'} for original in originals):
            self.refuse('owner_recovery_current_work_reserved',
                'Another retained request already owns admission for this Work Item, potentially on another thread. Reconcile its current disposition before automatic continuation. Action owner: ' + context.action_owner)
        if any(original.queued_start_receipt is not None for original in originals):
            # A positive START is a historical delivery fact, not a proof that
            # its later turn completed. Neither lost custody nor generic usage
            # and transcript display state supplies a generation-bound outcome.
            self.refuse('owner_recovery_prior_turn_unresolved',
                'A previous input for this Work Item positively started. Its later native turn outcome requires explicit reconciliation before automatic same-item continuation; do not retry that original. Action owner: ' + context.action_owner)

    def native_goal_blocker(self, binding):
        """Bare native Goals get a precise blocker, never a replayed objective."""
        from codex_web.goal_execution_bindings import ProjectDelegationAuthorization
        if (not binding.thread_id
                or binding.delegation_authorization != ProjectDelegationAuthorization.AUTHORIZED
                or str(binding.status) in {'completed', 'cancelled', 'unknown', 'blocked'}
                or not self.project_enabled(binding.project_id)
                or self.execution.automation_wait_reason(self.SOURCE)):
            return None
        goal = self.goal_for(binding) if self.goal_for is not None else None
        if goal is None or str(goal.status) != 'active' or goal.revision != binding.goal_revision:
            return None
        current = self.resolve_binding(binding)
        failed = self.terminal(current)
        if failed is None or (failed.organization_id, failed.workspace_id) != (
                binding.organization_id, binding.workspace_id):
            return None
        # Qualified Work Items are handled only by admitted owner cycles. A
        # Goal alone cannot establish an independent safe new native task.
        for bot in self.load_bindings():
            if bot.project_id != binding.project_id:
                continue
            bot = self.resolve_binding(bot)
            if bot.thread_id != current.thread_id:
                continue
            states = [self.get_state(ref) for ref in binding.work_item_refs[:32]]
            states = [state for state in states if state is not None]
            if self.select_current_work(bot, states) is not None:
                return None
        return ('native_runner_recovery: assignment ' + failed.id + ' fence '
            + str(failed.fence) + ' is ' + str(failed.status)
            + '; no independent current owned Work Item is qualified for automatic continuation. '
            + 'Retained native inputs and provider outcomes require reconciliation; '
            + 'do not replay the Goal objective. Action owner: ' + binding.execution_owner_id)

    async def recover(self, binding, state, text):
        """Called only inside the admitted controller reasoner; never replaces a thread."""
        binding = await asyncio.to_thread(self.resolve_binding, binding)
        current = await asyncio.to_thread(self.get_state, state.ref)
        assignment = await asyncio.to_thread(self.terminal, binding)
        if (assignment is None or not self.owns(binding, current)
                or self.revision(current) != self.revision(state)):
            # The caller rendered its prompt from this exact business intent;
            # a fresher scope/owner/action cannot authorize the older text.
            return {'ok': True, 'ignored': True, 'reason': 'owner_recovery_changed', 'threadId': binding.thread_id}
        revision = self.revision(current)
        # One request per logical binding/current work revision, even if its
        # worker subsequently fails again. No cooldown expires this reservation.
        request_id = self.request_id(binding, current)
        key = request_id.removeprefix('owner-recovery-')
        attempt_id = 'preflight-queued-' + request_id
        existing = await asyncio.to_thread(self.preflight.store.get, attempt_id)
        if existing is not None:
            return {'ok': True, 'deduplicated': True, 'reason': 'owner_recovery_retained',
                'threadId': binding.thread_id, 'preflightAttemptId': existing.id,
                'recoveryStatus': existing.status, 'actionOwner': actionable_owner_value(current)}
        if not await asyncio.to_thread(self.candidate, binding, current):
            return {'ok': True, 'ignored': True, 'reason': 'owner_lane_busy', 'threadId': binding.thread_id}
        project = await asyncio.to_thread(self.dispatch.projects.get, binding.project_id)
        settings = await asyncio.to_thread(self.dispatch.settings.get, binding.thread_id)
        profile_context = await asyncio.to_thread(self.dispatch.agent_profile_resolver, binding)
        if profile_context is None:
            return {'ok': False, 'reason': 'owner_recovery_profile_unavailable', 'threadId': binding.thread_id}
        profile, actor = profile_context
        primary, writable = work_item_repository_scope(current)
        execution_profile, _ = await asyncio.to_thread(self.dispatch.execution_profiles.resolve,
            profile.execution_profile_id, organization_id=project.organization_id,
            workspace_id=project.workspace_id, project_id=project.id)
        if execution_profile.repository_access == 'none':
            primary, writable = None, ()
        target = (await asyncio.to_thread(self.dispatch.targets.event_reply_target, binding)
                  or self.dispatch.targets.conversation_target(binding))
        teardown = await asyncio.to_thread(self.observe_teardown, assignment)
        queued = QueuedTurn(id=request_id, thread_id=binding.thread_id, project_id=project.id,
            execution_id='thread-turn-' + key,
            message=('Continue only the current canonical task and checkpoint below. Historical uncertain native or provider inputs remain held; do not replay them or infer their outcomes.\n\n' + text),
            work_item_ref=current.ref, source=self.SOURCE, created_at=time.time(),
            sandbox=binding.sandbox or settings.sandbox or project.sandbox,
            approval_policy=binding.approval_policy or settings.approval_policy or project.approval_policy,
            model=settings.model or project.model, reasoning_effort=settings.reasoning_effort,
            repository_resource_id=primary, writable_repository_resource_ids=writable,
            execution_profile_id=profile.execution_profile_id,
            agent_profile_id=profile.profile_id, agent_profile_revision=profile.revision,
            agent_profile_actor_id=actor.identity_id, reply_target=target,
            owner_lane_recovery=OwnerLaneRecoveryContext(bot_binding_id=binding.id,
                failed_assignment_id=assignment.id, failed_fence=assignment.fence,
                teardown_observed_at=teardown[1] if teardown else None,
                observed_process_fence=teardown[0] if teardown else None,
                work_revision=revision, action_owner=actor.identity_id, goal_id=current.goal_id))
        # The existing immutable preflight reservation precedes ALL native I/O.
        # A crash after reservation is retained for inspection, never redelivery.
        try:
            attempt = await asyncio.to_thread(self.preflight.retain_queued, queued,
                organization_id=project.organization_id, workspace_id=project.workspace_id)
        except ValueError:
            # Two observations can construct different capture timestamps for
            # this same key. Only the first immutable original wins. An actual
            # committed reservation, not the exception text, establishes dedup.
            retained = await asyncio.to_thread(self.preflight.store.get, attempt_id)
            original = QueuedTurn.model_validate(retained.queued_turn_snapshot) if retained else None
            if (original is None or original.source != self.SOURCE
                    or original.owner_lane_recovery is None
                    or original.owner_lane_recovery.work_revision != revision
                    or original.owner_lane_recovery.bot_binding_id != binding.id
                    or original.work_item_ref != current.ref
                    or (retained.organization_id, retained.workspace_id, retained.project_id)
                        != (project.organization_id, project.workspace_id, project.id)):
                raise
            return {'ok': True, 'deduplicated': True, 'reason': 'owner_recovery_retained',
                'threadId': binding.thread_id, 'preflightAttemptId': retained.id,
                'recoveryStatus': retained.status, 'actionOwner': actionable_owner_value(current)}
        try:
            await asyncio.to_thread(self.validate_queued, queued)
        except HTTPException as exc:
            attempt = await asyncio.to_thread(self.preflight.queued_disposition,
                attempt.id, status='blocked', detail=exc.detail)
            await self.execution.publish_queue_status(binding.thread_id)
            return {'ok': False, 'reason': 'owner_recovery_blocked', 'threadId': binding.thread_id,
                'preflightAttemptId': attempt.id, 'actionOwner': actionable_owner_value(current)}
        # Append through the existing atomic queue repository. A human arrival
        # racing the earlier read keeps its place; no retained original moves.
        def append(items):
            return items if any(item.id == queued.id for item in items) else [*items, queued]
        await asyncio.to_thread(self.execution._update_thread_queue, binding.thread_id, append)
        self.execution.schedule_queue_drain(binding.thread_id)
        await self.execution.publish_queue_status(binding.thread_id)
        return {'ok': True, 'queued': True, 'threadId': binding.thread_id,
            'queuedId': queued.id, 'preflightAttemptId': attempt.id, 'actionOwner': actionable_owner_value(current)}
