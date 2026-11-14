"""Project Goals enter the existing worker queue, never the generic native RPC.

The source identifies a retained reservation. Authority comes from its current
Goal, binding, execution owner and canonical worker custody, not that string.
Native start truth remains in the existing execution preflight owner.
"""
import asyncio
import hashlib
import time
import uuid

from codex_web.goal_execution_bindings import GoalExecutionBindingEvent, GoalExecutionBindingStatus as Status
from codex_web.identity import TenantScope
from codex_web.models import QueuedTurn, TurnCreate
from codex_web.project_goal_delivery import ProjectGoalDelivery, ProjectGoalProgress
from codex_web.services.goal_continuation import GoalContinuationDispatchResult
from codex_web.services.procedure_discovery import checksum
from codex_web.services.queued_native_start import validate_attribution
from codex_web.services.autonomy_controller import AutonomyController
from codex_web.runtime.execution import AutomatedAdmissionPaused, ExecutionPrestartRefusal

SOURCE = 'project-goal-continuation:'


class _ProjectGoalPolicyRefusal(ValueError):
    """Canonical policy denial; remains ValueError-compatible outside input admission."""


class ProjectGoalContinuation:
    def __init__(self, *, continuation, turns, identity, profiles, execution_profiles,
                 workers, bootstrap_bindings, usage):
        self.continuation, self.turns, self.identity = continuation, turns, identity
        self.bindings, self.goals, self.sessions = continuation.bindings, continuation.goals, continuation.agent_sessions
        self.profiles, self.execution_profiles, self.workers = profiles, execution_profiles, workers
        self.bootstraps, self.usage = bootstrap_bindings, usage
        self.preflight = turns.execution.queued_preflight

    @staticmethod
    def handles(binding):
        # Other provider-neutral runtimes retain their existing contracts.
        return binding.project_id != 'home' and (binding.provider_id, binding.runtime_id) == ('openai', 'codex')

    @staticmethod
    def origin(source):
        normalized = source.removeprefix('queued:') if isinstance(source, str) else ''
        return normalized[len(SOURCE):] if normalized.startswith(SOURCE) else None

    def binding(self, binding_id, scope):
        state = self.bindings.store.binding_state(binding_id, organization_id=scope.organization_id,
            workspace_id=scope.workspace_id)
        return self.bindings._binding(state, binding_id, scope)

    @staticmethod
    def fingerprint(assignment):
        # Ordinary renewal/preparation can change observational/history fields.
        # None of those changes can substitute an executor, pin or resource.
        fields = ('id', 'organization_id', 'workspace_id', 'project_id', 'execution_id',
            'created_by', 'subject', 'assigned_worker_id', 'execution_workspace_id', 'fence',
            'agent_profile', 'execution_profile_id', 'execution_profile_definition',
            'runtime_binding', 'resource_ids', 'resource_context', 'repository_scope',
            'repository_target', 'sandbox', 'approval_policy', 'network')
        raw = assignment.model_dump(mode='json')
        return checksum({key: raw.get(key) for key in fields})

    def context(self, binding, *, lease=True):
        scope = TenantScope(organization_id=binding.organization_id, workspace_id=binding.workspace_id)
        current = self.binding(binding.id, scope)
        goal_state = self.goals.store.goal_state(current.goal_id,
            organization_id=scope.organization_id, workspace_id=scope.workspace_id)
        goal = self.goals._goal(goal_state, current.goal_id, scope)
        actor = self.identity.actor_for_identity(current.execution_owner_id, scope=scope)
        if (not self.handles(current) or current.execution_mode != 'application_continuation'
                or current.status not in {'requested', 'active', 'idle'}
                or goal.status != 'active' or goal.revision != current.goal_revision
                or goal.owner_identity_id != actor.identity_id):
            raise ValueError('Current Project Goal owner, revision or binding changed')
        graph = next((item for item in goal.work_graph_bindings if item.project_id == current.project_id), None)
        if graph is None or (graph.root_work_item_refs and (not current.work_item_refs
                or not set(current.work_item_refs).issubset(graph.root_work_item_refs))):
            raise ValueError('Current Project Goal roots changed')
        reason = self.continuation._policy_stop_reason(current)
        if reason:
            raise _ProjectGoalPolicyRefusal(reason)
        if lease and (current.lease_owner_id != self.continuation.owner_id
                or current.lease_expires_at is None or current.lease_expires_at <= time.time()):
            raise ValueError('Current Project Goal continuation lease unavailable')
        session = self.sessions.get(current.agent_session_id, actor)
        bootstrap = self.bootstraps.get_by_thread(current.thread_id, actor)
        assignment = self.workers.store.assignment(bootstrap.assignment_id)
        if (assignment is None or assignment.agent_profile is None
                or (assignment.organization_id, assignment.workspace_id, assignment.project_id, assignment.created_by)
                    != (scope.organization_id, scope.workspace_id, current.project_id, actor.identity_id)
                or (session.project_id, session.provider_native_session_id, session.provider_id, session.runtime_id,
                    session.assignment_id, session.execution_id, session.execution_workspace_id)
                    != (current.project_id, current.thread_id, current.provider_id, current.runtime_id,
                        assignment.id, assignment.execution_id, assignment.execution_workspace_id)):
            raise ValueError('Current Project Goal Session custody changed')
        worker = self.workers.store.worker(assignment.assigned_worker_id)
        runtime, pin = assignment.runtime_binding, assignment.agent_profile
        if (runtime is None or (runtime.provider_id, runtime.runtime_id) != (current.provider_id, current.runtime_id)
                or session.worker_id != assignment.assigned_worker_id
                or pin.selected_provider_id not in {None, runtime.provider_id}
                or pin.selected_runtime_id not in {None, runtime.runtime_id}
                or pin.selected_worker_id not in {None, assignment.assigned_worker_id}):
            raise ValueError('Current Project Goal runtime or Session worker changed')
        now = time.time()
        if (assignment.status not in {'claimed', 'running'} or assignment.lease is None
                or assignment.lease.worker_id != assignment.assigned_worker_id
                or assignment.lease.fence != assignment.fence or assignment.lease.expires_at <= now
                or assignment.deadline_at is not None and assignment.deadline_at <= now
                or worker is None or worker.lifecycle != 'active'
                or (worker.organization_id, worker.workspace_id) != (scope.organization_id, scope.workspace_id)):
            raise ValueError('Current Project Goal worker lease unavailable')
        profile, _ = self.profiles.resolve_for_execution(assignment.agent_profile.profile_id,
            actor=actor, project_id=current.project_id)
        pin = assignment.agent_profile
        selection = {key: getattr(pin, key) for key in ('selected_provider_id', 'selected_runtime_id',
            'selected_provider_revision', 'selected_runtime_capability_revision', 'selected_worker_id',
            'model_provider_id', 'model_id')}
        if profile.lifecycle != 'active' or self.profiles.binding_for(profile, **selection) != pin:
            raise ValueError('Current Project Goal Agent Profile changed')
        _, definition = self.execution_profiles.resolve(assignment.execution_profile_id,
            organization_id=scope.organization_id, workspace_id=scope.workspace_id, project_id=current.project_id)
        if definition != assignment.execution_profile_definition:
            raise ValueError('Current Project Goal execution Definition changed')
        control = self.continuation.autonomy.store.load().control
        scoped_pause = AutonomyController._scoped_pause_reason(control, actor=actor,
            project_id=current.project_id, resource_ids=assignment.resource_ids)
        if scoped_pause:
            raise ValueError(scoped_pause)
        wait = self.turns.execution.automation_wait_reason(SOURCE+current.id)
        if wait is not None:
            raise ValueError('Current Project Goal automated admission is paused')
        # Metadata reads may span canonical same-owner renewal. Re-read the
        # finite custody, rather than reject an expired earlier observation of
        # leases that their owners renewed while still live. Renewal is not a
        # substitute for unchanged binding/input and worker custody.
        fresh_goal_state = self.goals.store.goal_state(current.goal_id,
            organization_id=scope.organization_id, workspace_id=scope.workspace_id)
        fresh_goal = self.goals._goal(fresh_goal_state, current.goal_id, scope)
        fresh = self.binding(current.id, scope)
        fresh_assignment = self.workers.store.assignment(assignment.id)
        fresh_worker = self.workers.store.worker(assignment.assigned_worker_id)
        heartbeat_fields = {'lease_expires_at', 'heartbeat_at', 'updated_at', 'updated_by',
            'change_reason', 'updated_correlation_id', 'updated_causation_id'}
        if (fresh_goal != goal
                or fresh.model_dump(exclude=heartbeat_fields) != current.model_dump(exclude=heartbeat_fields)
                or fresh.lease_expires_at is not None and current.lease_expires_at is not None
                    and fresh.lease_expires_at < current.lease_expires_at
                or fresh.heartbeat_at is not None and current.heartbeat_at is not None
                    and fresh.heartbeat_at < current.heartbeat_at):
            raise ValueError('Current Project Goal binding changed during admission')
        if (fresh_assignment is None or self.fingerprint(fresh_assignment) != self.fingerprint(assignment)
                or fresh_assignment.status != assignment.status or fresh_assignment.lease is None
                or fresh_assignment.lease.model_dump(exclude={'expires_at', 'renewed_at'})
                    != assignment.lease.model_dump(exclude={'expires_at', 'renewed_at'})
                or fresh_assignment.deadline_at != assignment.deadline_at
                or fresh_assignment.lease.expires_at < assignment.lease.expires_at
                or assignment.lease.renewed_at is not None and (fresh_assignment.lease.renewed_at is None
                    or fresh_assignment.lease.renewed_at < assignment.lease.renewed_at)
                or fresh_worker is None or fresh_worker.lifecycle != 'active'
                or (fresh_worker.id, fresh_worker.organization_id, fresh_worker.workspace_id,
                    fresh_worker.service_identity_id) != (worker.id, worker.organization_id,
                        worker.workspace_id, worker.service_identity_id)):
            raise ValueError('Current Project Goal worker custody changed during admission')
        now = time.time()
        if (lease and (fresh.lease_expires_at is None or fresh.lease_expires_at <= now)
                or fresh_assignment.lease.expires_at <= now
                or fresh_assignment.deadline_at is not None and fresh_assignment.deadline_at <= now):
            raise ValueError('Current Project Goal lease expired during admission')
        return fresh, fresh_goal, actor, fresh_assignment

    def mutate(self, binding_id, scope, updater, reason):
        result = []
        def apply(state):
            row = self.bindings._binding(state, binding_id, scope)
            updated = updater(row)
            if row != updated:
                # Validate new typed joins before committing canonical state,
                # rather than discovering a malformed write on the next read.
                updated = type(row).model_validate(updated.model_dump(mode='python'))
                updated = updated.model_copy(update={'updated_at': time.time(),
                    'updated_by': self.continuation.owner_id, 'change_reason': reason})
                state.bindings = [updated if item.id == row.id else item for item in state.bindings]
                state.events.append(GoalExecutionBindingEvent(binding_id=row.id, goal_id=row.goal_id,
                    event_type='binding_project_continuation', actor_id=self.continuation.owner_id, reason=reason))
            result.append(updated)
            return state
        self.bindings.store.update(apply)
        return result[0]

    def authorize_input(self, *, source, assignment, actor, project, request_checksum=None, input_execution_id=None):
        binding_id = self.origin(source)
        if binding_id is None:
            return None
        try:
            current, goal, owner, snapshot = self.context(self.binding(binding_id, actor.tenant))
        except _ProjectGoalPolicyRefusal as exc:
            # This owner is called before native input, and again by the real
            # native writer guard. Never classify arbitrary ValueError/provider
            # prose as evidence that an input was not sent (#2110).
            raise ExecutionPrestartRefusal(status_code=409, detail={
                'code': 'project_goal_preinput_refused', 'message': str(exc),
                'goal_binding_id': binding_id,
            }) from exc
        progress = current.project_continuation
        d = progress.delivery if progress else None
        if (d is None or d.phase not in {'reserved', 'queued'} or not actor.has_same_authority(owner)
                or d.continuation_owner_id != current.lease_owner_id
                or d.goal_revision != goal.revision or d.work_item_refs != current.work_item_refs
                or (d.requested_by, d.session_id, d.thread_id) !=
                    (owner.identity_id, current.agent_session_id, current.thread_id)
                or assignment is None or self.fingerprint(assignment) != self.fingerprint(snapshot)
                or d.assignment_id != snapshot.id
                or self.fingerprint(snapshot) != d.assignment_fingerprint
                or project.id != current.project_id or current.last_execution_id != d.id
                or request_checksum is not None and request_checksum != d.message_sha256
                or input_execution_id is not None and input_execution_id != d.id):
            raise ValueError('Original Project Goal input or authority changed')
        return d

    def prepare_native_input_guard(self, *, project, actor, execution_id, assignment_id, source,
                                   request_checksum=None, input_execution_id=None):
        if self.origin(source) is None:
            return None
        if actor is None or request_checksum is None or input_execution_id is None:
            raise ValueError('Exact original Project Goal input required')
        def guard():
            assignment = self.workers.store.assignment(assignment_id)
            if assignment is None or assignment.execution_id != execution_id:
                raise ValueError('Original Project Goal execution changed')
            return self.authorize_input(source=source, assignment=assignment, actor=actor, project=project,
                request_checksum=request_checksum, input_execution_id=input_execution_id)
        guard()
        return guard

    def queued_admission(self, queued):
        binding_id = self.origin(queued.source)
        if binding_id is None:
            return None
        def validate():
            try:
                project = self.turns.projects.get(queued.project_id)
                scope = TenantScope(organization_id=project.organization_id, workspace_id=project.workspace_id)
                current, _, actor, assignment = self.context(self.binding(binding_id, scope))
                self.authorize_input(source=queued.source, assignment=assignment, actor=actor, project=project,
                    request_checksum=hashlib.sha256(queued.message.encode()).hexdigest(), input_execution_id=queued.execution_id)
                if (queued.thread_id, queued.project_id, queued.agent_profile_actor_id) != (
                        current.thread_id, current.project_id, actor.identity_id):
                    raise ValueError('Original Project Goal queue scope changed')
            except Exception as exc:
                raise AutomatedAdmissionPaused(status_code=409, detail={
                    'code': 'project_goal_continuation_blocked',
                    'message': 'Project Goal input is held until its current scope, owner and worker custody are ready',
                }) from exc
        return validate

    async def enqueue_owned(self, binding, operation, guard):
        """Renew the finite binding lease while awaiting this one queue write."""
        task = asyncio.create_task(operation())
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=max(1., min(60., self.continuation.lease_seconds / 3)))
                if done:
                    return await task
                await asyncio.to_thread(guard)
                await asyncio.to_thread(self.bindings.heartbeat_continuation, binding.id,
                    scope=TenantScope(organization_id=binding.organization_id, workspace_id=binding.workspace_id),
                    owner_id=self.continuation.owner_id, lease_seconds=self.continuation.lease_seconds)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def dispatch(self, claimed, *, scope):
        reserved = None
        try:
            current, goal, actor, assignment = await asyncio.to_thread(self.context, claimed)
            progress = current.project_continuation or ProjectGoalProgress()
            if progress.delivery is not None and progress.delivery.phase != 'settled':
                return GoalContinuationDispatchResult(binding_id=current.id, outcome='not_claimed', reason='original_project_goal_input_owned')
            budget = goal.budget
            if any(limit is not None and (used is None or used >= limit) for used, limit in (
                    (progress.reserved_calls, budget.max_model_calls), (progress.model_calls, budget.max_model_calls),
                    (progress.input_tokens, budget.max_input_tokens),
                    (progress.output_tokens, budget.max_output_tokens))):
                raise ValueError('Project Goal budget exhausted')
            if budget.max_cost_usd is not None and (progress.cost_usd is None or progress.cost_usd >= budget.max_cost_usd):
                raise ValueError('Project Goal cost budget unavailable or exhausted')
            message = self.continuation._prompt(current)
            delivery = ProjectGoalDelivery(id='project-goal-'+uuid.uuid4().hex, goal_revision=goal.revision,
                work_item_refs=current.work_item_refs, requested_by=actor.identity_id,
                continuation_owner_id=current.lease_owner_id, session_id=current.agent_session_id,
                thread_id=current.thread_id, assignment_id=assignment.id, assignment_fingerprint=self.fingerprint(assignment),
                message=message, message_sha256=hashlib.sha256(message.encode()).hexdigest())
            def reserve(row):
                if row != current:
                    raise ValueError('Project Goal reservation changed before commit')
                return row.model_copy(update={'last_execution_id': delivery.id, 'last_turn_id': None,
                    'project_continuation': progress.model_copy(update={'reserved_calls': progress.reserved_calls+1, 'delivery': delivery})})
            reserved = await asyncio.to_thread(self.mutate, current.id, scope, reserve, 'Original Project Goal input reserved')
            project = self.turns.projects.get(current.project_id)
            def guard():
                return self.authorize_input(source=SOURCE+current.id, assignment=assignment, actor=actor, project=project,
                    request_checksum=delivery.message_sha256, input_execution_id=delivery.id)
            pin = assignment.agent_profile
            result = await self.enqueue_owned(reserved, lambda: self.turns.start(delivery.thread_id,
                TurnCreate(project_id=current.project_id, message=message,
                    execution_profile_id=assignment.execution_profile_id, agent_profile_id=pin.profile_id,
                    agent_profile_revision=pin.profile_revision, defer_start=True), actor=actor, admission_guard=guard,
                execution_id=delivery.id, source=SOURCE+current.id, allow_assignment_renewal=False), guard)
            queued_id = result.get('queuedId')
            if not queued_id:
                raise ValueError('Original Project Goal queue outcome unknown')
            def accepted(row):
                d = row.project_continuation.delivery
                # A fast START/terminal remains owned by its actual receipt.
                if d.id != delivery.id or d.phase not in {'reserved', 'queued'}:
                    return row
                return row.model_copy(update={'project_continuation': row.project_continuation.model_copy(update={
                    'delivery': d.model_copy(update={'phase': 'queued', 'queued_id': queued_id,
                        'admission_attempt_id': 'preflight-queued-'+queued_id})})})
            await asyncio.to_thread(self.mutate, current.id, scope, accepted, 'Original Project Goal input queued')
            return GoalContinuationDispatchResult(binding_id=current.id, outcome='queued', reason='canonical_project_goal_queue')
        except BaseException as exc:
            def held(row):
                if reserved is not None:
                    d = row.project_continuation.delivery
                    if d.id != reserved.project_continuation.delivery.id or d.phase not in {'reserved', 'queued'}:
                        return row
                    stopped = row.status in {Status.BLOCKED, Status.CANCELLED, Status.COMPLETED, Status.UNKNOWN}
                    return row.model_copy(update={'status': row.status if stopped else Status.UNKNOWN,
                        'stop_reason': row.stop_reason if stopped else 'original_project_goal_input_uncertain',
                        'project_continuation': row.project_continuation.model_copy(update={'delivery': d.model_copy(update={'phase': 'unknown'})})})
                if row != claimed:
                    return row
                return row.model_copy(update={'status': Status.BLOCKED, 'stop_reason': 'project_goal_admission_unavailable'})
            await asyncio.to_thread(self.mutate, claimed.id, scope, held, 'Project Goal original held; no implicit replay')
            if isinstance(exc, asyncio.CancelledError):
                raise
            return GoalContinuationDispatchResult(binding_id=claimed.id, outcome='unknown' if reserved else 'blocked', reason=type(exc).__name__)

    def project_native_start(self, attempt):
        queued = QueuedTurn.model_validate(attempt.queued_turn_snapshot) if attempt.queued_turn_snapshot else None
        binding_id = self.origin(queued.source) if queued else None
        if binding_id is None:
            return False
        scope = TenantScope(organization_id=attempt.organization_id, workspace_id=attempt.workspace_id)
        binding = self.binding(binding_id, scope)
        d = binding.project_continuation.delivery if binding.project_continuation else None
        correlation, receipt, evidence = attempt.queued_start_correlation, attempt.queued_start_receipt, attempt.queued_start_evidence
        if d is None or attempt.status != 'started' or correlation is None or receipt is None or evidence is None:
            raise ValueError('Original Project Goal START proof unavailable')
        validate_attribution(self.preflight, attempt, correlation)
        assignment = self.workers.store.assignment(d.assignment_id)
        if (assignment is None or self.fingerprint(assignment) != d.assignment_fingerprint
                or (queued.execution_id, queued.thread_id, queued.project_id, queued.message, queued.agent_profile_actor_id)
                    != (d.id, d.thread_id, binding.project_id, d.message, d.requested_by)
                or (correlation.assignment_id, correlation.worker_id, correlation.fence) !=
                    (assignment.id, assignment.assigned_worker_id, assignment.fence)
                or (receipt.provider_native_session_id, receipt.execution_id, receipt.provider_id, receipt.runtime_id) !=
                    (d.thread_id, assignment.execution_id, binding.provider_id, binding.runtime_id)
                or evidence.provider_native_turn_id != receipt.provider_native_turn_id):
            raise ValueError('Original Project Goal START attribution changed')
        def started(row):
            current = row.project_continuation.delivery
            if current.id != d.id or current.phase in {'settled', 'terminal_pending_usage'}:
                return row
            if current.native_turn_id not in {None, receipt.provider_native_turn_id}:
                raise ValueError('Original Project Goal has another START')
            return row.model_copy(update={'last_turn_id': receipt.provider_native_turn_id,
                'project_continuation': row.project_continuation.model_copy(update={'delivery': current.model_copy(update={
                    'phase': 'started', 'queued_id': queued.id, 'admission_attempt_id': attempt.id,
                    'native_turn_id': receipt.provider_native_turn_id})})})
        self.mutate(binding.id, scope, started, 'Canonical Project Goal START observed')
        return True

    async def reconcile(self, binding, *, scope):
        progress = binding.project_continuation
        d = progress.delivery if progress else None
        if d is None or d.phase == 'settled':
            return None
        # Session idle, absent process handles or a missing queue are not a
        # no-effects witness. Recovery never reserves another input here.
        if d.phase == 'terminal_pending_usage':
            await self.settle_pending(binding, scope=scope)
        elif d.phase == 'reserved' and (binding.lease_expires_at is None or binding.lease_expires_at <= time.time()):
            def uncertain(row):
                if (row.status != Status.ACTIVE or row.project_continuation.delivery != d
                        or row.lease_expires_at is not None and row.lease_expires_at > time.time()):
                    return row
                return row.model_copy(update={'status': Status.UNKNOWN,
                    'stop_reason': 'expired_project_goal_reservation_requires_reconciliation',
                    'project_continuation': row.project_continuation.model_copy(update={
                        'delivery': d.model_copy(update={'phase': 'unknown'})})})
            await asyncio.to_thread(self.mutate, binding.id, scope, uncertain, 'Expired original Project Goal reservation held UNKNOWN')
        elif d.phase == 'queued' and d.admission_attempt_id and d.queued_id:
            try:
                current, _, _, assignment = await asyncio.to_thread(self.context, binding, lease=False)
            except Exception:
                return GoalContinuationDispatchResult(binding_id=binding.id, outcome='not_dispatched', reason='original_project_goal_current_custody_unavailable')
            queued = next((item for item in self.turns.execution._thread_queue(d.thread_id) if item.id == d.queued_id), None)
            if queued is not None:
                await asyncio.to_thread(self.preflight.retain_queued, queued,
                    organization_id=scope.organization_id, workspace_id=scope.workspace_id)
            def known_queued():
                attempt = self.preflight.store.get(d.admission_attempt_id)
                queued = next((item for item in self.turns.execution._thread_queue(d.thread_id) if item.id == d.queued_id), None)
                if (attempt is None or queued is None or attempt.status != 'queued'
                        or attempt.queued_admission_claim_id is not None or attempt.started_at is not None
                        or attempt.scheduling_retirement is not None
                        or any(getattr(attempt, key) is not None for key in ('queued_start_correlation',
                            'queued_start_evidence', 'queued_start_receipt', 'queued_resume_correlation', 'queued_resume_evidence'))
                        or attempt.queued_turn_snapshot != queued.model_dump(mode='json')
                        or (queued.execution_id, queued.source, queued.message, queued.agent_profile_actor_id,
                            queued.thread_id, queued.project_id) !=
                            (d.id, SOURCE+current.id, d.message, d.requested_by, d.thread_id, current.project_id)
                        or self.fingerprint(assignment) != d.assignment_fingerprint):
                    raise ValueError('Original Project Goal queued proof unavailable')
            try:
                await asyncio.to_thread(known_queued)
            except Exception:
                return GoalContinuationDispatchResult(binding_id=binding.id, outcome='not_dispatched', reason='original_project_goal_queued_proof_unavailable')
            if current.lease_expires_at is None or current.lease_expires_at <= time.time():
                claimed = await asyncio.to_thread(self.bindings.claim_continuation, current.id, scope=scope,
                    owner_id=self.continuation.owner_id, lease_seconds=self.continuation.lease_seconds)
                if claimed is not None:
                    def adopt(row):
                        known_queued()
                        if row != claimed or row.project_continuation.delivery != d:
                            raise ValueError('Original Project Goal queue owner changed')
                        return row.model_copy(update={'project_continuation': row.project_continuation.model_copy(update={
                            'delivery': d.model_copy(update={'continuation_owner_id': claimed.lease_owner_id})})})
                    await asyncio.to_thread(self.mutate, current.id, scope, adopt, 'Same proven queued Project Goal input re-adopted')
                    self.turns.execution.schedule_queue_drain(d.thread_id)
        return GoalContinuationDispatchResult(binding_id=binding.id, outcome='not_dispatched', reason='original_project_goal_input_owned')

    async def record_terminal(self, active, *, succeeded):
        binding_id = self.origin(active.source)
        if binding_id is None:
            return False
        attempt = await asyncio.to_thread(self.preflight.store.get, active.queued_admission_attempt_id)
        if attempt is None:
            raise ValueError('Original Project Goal terminal START missing')
        await asyncio.to_thread(self.project_native_start, attempt)
        scope = TenantScope(organization_id=attempt.organization_id, workspace_id=attempt.workspace_id)
        binding = await asyncio.to_thread(self.binding, binding_id, scope)
        d = binding.project_continuation.delivery
        assignment = await asyncio.to_thread(self.workers.store.assignment, d.assignment_id)
        if (active.turn_id, active.thread_id, active.project_id, active.assignment_id,
                active.execution_id, active.execution_workspace_id, active.worker_id, active.fence) != (
                d.native_turn_id, d.thread_id, binding.project_id, d.assignment_id, assignment.execution_id,
                assignment.execution_workspace_id, assignment.assigned_worker_id, assignment.fence):
            raise ValueError('Original Project Goal terminal custody changed')
        def terminal(row):
            current = row.project_continuation.delivery
            if current.id != d.id or current.phase == 'settled':
                return row
            if current.completion_succeeded not in {None, succeeded}:
                raise ValueError('Original Project Goal terminal outcome changed')
            return row.model_copy(update={'project_continuation': row.project_continuation.model_copy(update={
                'delivery': current.model_copy(update={'phase': 'terminal_pending_usage', 'completion_succeeded': succeeded})})})
        binding = await asyncio.to_thread(self.mutate, binding.id, scope, terminal, 'Owned Project Goal terminal observed')
        await self.settle_pending(binding, scope=scope)
        return True

    async def settle_pending(self, binding, *, scope):
        settled = await asyncio.to_thread(self.settle_usage, binding, scope)
        continuation = None
        if settled:
            current = await asyncio.to_thread(self.binding, binding.id, scope)
            if current.status == Status.IDLE:
                continuation = await self.continuation.dispatch_once(current.id, scope=scope)
        return settled, continuation

    def settle_usage(self, binding, scope):
        d = binding.project_continuation.delivery
        if d.phase != 'terminal_pending_usage':
            return False
        usage_id = 'runtime-usage-'+hashlib.sha256(f'{binding.agent_session_id}\n{d.native_turn_id}'.encode()).hexdigest()[:32]
        usage = self.usage.get(usage_id)
        if usage is None or usage.completed_at is None or usage.terminal_outcome == 'unknown':
            return False
        attempt = self.preflight.store.get(d.admission_attempt_id)
        if attempt is None:
            raise ValueError('Original Project Goal usage START missing')
        receipt, correlation = attempt.queued_start_receipt, attempt.queued_start_correlation
        assignment = self.workers.store.assignment(d.assignment_id)
        if assignment is None or receipt is None or correlation is None or self.fingerprint(assignment) != d.assignment_fingerprint:
            raise ValueError('Original Project Goal usage custody changed')
        if (usage.organization_id, usage.workspace_id, usage.project_id, usage.agent_session_id,
                usage.provider_native_session_id, usage.provider_native_turn_id, usage.assignment_id,
                usage.execution_id, usage.execution_workspace_id, usage.provider_id, usage.runtime_id) != (
                scope.organization_id, scope.workspace_id, binding.project_id, binding.agent_session_id,
                d.thread_id, d.native_turn_id, d.assignment_id, receipt.execution_id,
                assignment.execution_workspace_id, binding.provider_id, binding.runtime_id):
            raise ValueError('Original Project Goal usage attribution changed')
        if ((usage.terminal_outcome == 'succeeded') != d.completion_succeeded
                or usage.completed_at < receipt.observed_at):
            raise ValueError('Original Project Goal usage outcome changed')
        changed = []
        def measured_sum(left, right):
            return left+right if left is not None and right is not None else None
        def settle(row):
            progress = row.project_continuation
            if progress.delivery != d:
                return row
            changed.append(True)
            preserve = row.status in {'blocked', 'cancelled', 'completed', 'unknown'}
            return row.model_copy(update={'status': row.status if preserve else (Status.IDLE if d.completion_succeeded else Status.BLOCKED),
                'lease_owner_id': None, 'lease_expires_at': None,
                'project_continuation': progress.model_copy(update={'delivery': d.model_copy(update={'phase': 'settled', 'usage_id': usage.id}),
                    'model_calls': measured_sum(progress.model_calls, usage.model_call_count),
                    'input_tokens': measured_sum(progress.input_tokens, usage.input_tokens),
                    'output_tokens': measured_sum(progress.output_tokens, usage.output_tokens),
                    'cost_usd': measured_sum(progress.cost_usd, usage.cost_usd)})})
        self.mutate(binding.id, scope, settle, 'Original Project Goal terminal usage settled')
        return bool(changed)
