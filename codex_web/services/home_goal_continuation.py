"""Home admission adapter for the existing lease-owned Goal continuation owner.

The canonical Goal/binding/current human administrator is the grant. The source
only locates an original persisted delivery; it never authenticates a new actor.
"""
import asyncio
import hashlib
import json
import time
import uuid

from codex_web.home_goal import SOURCE, HomeGoalDelivery, HomeGoalProgress
from codex_web.goal_execution_bindings import GoalExecutionBindingEvent, GoalExecutionBindingStatus
from codex_web.identity import TenantScope
from codex_web.models import TurnCreate, QueuedTurn
from codex_web.services.identity import IdentityService
from codex_web.services.goal_continuation import GoalContinuationDispatchResult
from codex_web.services.queued_native_start import validate_attribution


class HomeGoalReservationChanged(ValueError):
    pass


class HomeGoalContinuation:
    def __init__(self, *, continuation, home_host, turns, preflight, usage):
        self.continuation, self.home = continuation, home_host
        self.bindings, self.goals, self.sessions = continuation.bindings, continuation.goals, continuation.agent_sessions
        self.turns, self.preflight, self.usage = turns, preflight, usage
        self.restart_recovery = None

    @staticmethod
    def handles(binding):
        return binding.project_id == 'home'

    def binding(self, binding_id, scope):
        state = self.bindings.store.binding_state(binding_id,
            organization_id=scope.organization_id, workspace_id=scope.workspace_id)
        return self.bindings._binding(state, binding_id, scope)

    def goal(self, goal_id, scope):
        state = self.goals.store.goal_state(goal_id, organization_id=scope.organization_id, workspace_id=scope.workspace_id)
        return self.goals._goal(state, goal_id, scope)

    def context(self, binding, *, require_lease=True):
        scope = TenantScope(organization_id=binding.organization_id, workspace_id=binding.workspace_id)
        current = self.binding(binding.id, scope)
        goal = self.goal(current.goal_id, scope)
        actor = self.home.broker.identity.actor_for_identity(goal.owner_identity_id, scope=scope)
        IdentityService.require_admin(actor)
        if (actor.principal_kind != 'human' or current.execution_mode != 'application_continuation'
                or current.created_by != actor.identity_id or current.execution_owner_id != actor.identity_id
                or goal.status != 'active' or goal.revision != current.goal_revision
                or current.project_id != 'home' or current.status not in {'active', 'requested', 'idle'}
                or not any(row.project_id == 'home' for row in goal.work_graph_bindings)):
            raise ValueError('Current original HumanAdmin Home Goal authorization changed')
        reason = self.continuation._policy_stop_reason(current)
        if reason:
            raise ValueError(reason)
        if require_lease and (current.lease_owner_id != self.continuation.owner_id
                or current.lease_expires_at is None or current.lease_expires_at <= time.time()):
            raise ValueError('Original Goal continuation lease expired or changed')
        session = self.sessions.get(current.agent_session_id, actor)
        selected = self.home.projects.get('home', actor.tenant).home_operator
        if selected is None or not selected.thread_id or selected.thread_id != current.thread_id:
            raise ValueError('Selected Home Goal conversation changed')
        bootstrap = self.home.bootstrap_bindings.get_by_thread(current.thread_id, actor)
        assignment = self.home.broker.execution_workers.store.assignment(bootstrap.assignment_id)
        if (assignment is None or (session.project_id, session.provider_native_session_id,
                session.provider_id, session.runtime_id, session.assignment_id, session.execution_id,
                session.execution_workspace_id) != ('home', current.thread_id, current.provider_id,
                    current.runtime_id, assignment.id, assignment.execution_id, assignment.execution_workspace_id)):
            raise ValueError('Original Home Goal canonical Session custody changed')
        worker = self.home.broker.execution_workers.store.worker(assignment.assigned_worker_id)
        if (assignment.status not in {'claimed', 'running'} or assignment.lease is None
                or assignment.lease.fence != assignment.fence
                or assignment.lease.worker_id != assignment.assigned_worker_id
                or assignment.lease.expires_at <= time.time() or worker is None or worker.lifecycle != 'active'
                or (worker.organization_id, worker.workspace_id) != (actor.organization_id, actor.workspace_id)
                or assignment.deadline_at is not None and assignment.deadline_at <= time.time()):
            raise ValueError('Current live Home Goal custody unavailable')
        self.home._authorize_context(assignment, actor)
        return current, goal, actor, assignment, selected

    @staticmethod
    def message(goal):
        # Goal text is bounded task data, never an authority statement.
        data = dict(goal_id=goal.id, revision=goal.revision, title=goal.title,
            objective=goal.description, criteria=[r.model_dump(mode='json') for r in goal.success_criteria],
            constraints=[r.model_dump(mode='json') for r in goal.constraints])
        return ('Continue this explicitly authorized canonical Home Goal once. Stop on completion, a concrete blocker, '
            'approval need or budget exhaustion. Preserve uncertain original actions; do not replay or poll while idle.\n'
            + json.dumps(data, sort_keys=True))

    def mutate(self, binding_id, scope, updater, reason, *, event_id=None):
        result = []
        def apply(state):
            row = self.bindings._binding(state, binding_id, scope)
            updated = updater(row, state)
            if updated == row:
                result.append(row)
                return state
            updated=updated.model_copy(update={'updated_at':time.time(),'updated_by':self.continuation.owner_id,'change_reason':reason})
            state.bindings = [updated if r.id == row.id else r for r in state.bindings]
            state.events.append(GoalExecutionBindingEvent(binding_id=row.id, goal_id=row.goal_id,
                event_type='binding_home_continuation', actor_id=self.continuation.owner_id, reason=reason,
                **({'id':event_id} if event_id is not None else {})))
            result.append(updated)
            return state
        self.bindings.store.update(apply)
        return result[0]

    async def reconcile(self, binding, *, scope):
        progress = binding.home_continuation
        if progress is None or progress.delivery is None or progress.delivery.phase == 'settled':
            return None
        if progress.delivery.phase == 'terminal_pending_usage':
            settled = await asyncio.to_thread(self._settle_usage, binding, scope)
            if settled:
                # Re-enter the ordinary owner on a later canonical recovery event.
                return GoalContinuationDispatchResult(binding_id=binding.id,outcome='not_dispatched',reason='home_goal_usage_settled')
        renewed=await asyncio.to_thread(self.renew_known_not_sent,binding)
        if renewed:
            self.turns.execution.schedule_queue_drain(binding.thread_id)
            return GoalContinuationDispatchResult(binding_id=binding.id,outcome='not_dispatched',reason='home_goal_original_lease_renewed')
        # Existing requested identity is held even if a lease expires or an HTTP
        # response was lost. Only canonical START/terminal/usage can settle it.
        return GoalContinuationDispatchResult(binding_id=binding.id, outcome='not_dispatched',
            reason='home_goal_original_input_requires_settlement:' + progress.delivery.id)

    def renew_known_not_sent(self,binding):
        """Renew owner lease only; the existing queue still owns SAME input."""
        if binding.lease_expires_at is not None and binding.lease_expires_at>time.time():return False
        scope=TenantScope(organization_id=binding.organization_id,workspace_id=binding.workspace_id)
        d=binding.home_continuation.delivery if binding.home_continuation else None
        if d is None or binding.status!='active':return False
        try:
            queued=None
            if d.phase=='queued':
                self.context(binding,require_lease=False)
                queued=next((r for r in self.turns.execution._thread_queue(d.thread_id) if r.id==d.queued_id),None)
                if queued is None or (queued.source,queued.execution_id,queued.message,queued.agent_profile_actor_id)!=(
                        SOURCE+binding.id,d.requested_execution_id,d.message,d.requested_by):return False
            elif self.restart_recovery is not None:
                queued=self.restart_recovery.pending_goal_queue(binding)
            if queued is None:return False
            attempt=self.preflight.retain_queued(queued,organization_id=scope.organization_id,workspace_id=scope.workspace_id)
            if (attempt.status!='queued' or attempt.queued_admission_claim_id is not None
                    or attempt.started_at is not None or attempt.scheduling_retirement is not None
                    or any(getattr(attempt,name) is not None for name in ('queued_start_correlation','queued_start_evidence',
                        'queued_start_receipt','queued_resume_correlation','queued_resume_evidence'))
                    or attempt.queued_turn_snapshot!=queued.model_dump(mode='json')):return False
            return self.bindings.claim_continuation(binding.id,scope=scope,owner_id=self.continuation.owner_id,
                lease_seconds=self.continuation.lease_seconds) is not None
        except Exception:
            # Missing/malformed evidence is a held original, never a new grant.
            return False

    async def dispatch(self, claimed, *, scope):
        from codex_web.services.post_turn_readiness import PostTurnReadinessService
        return await PostTurnReadinessService._run_owned(lambda:self._dispatch(claimed,scope=scope),
            seconds=30.,name='home-goal-delivery-'+claimed.id)

    async def _dispatch(self, claimed, *, scope):
        reserved = None
        current = claimed
        try:
            current, goal, actor, assignment, selected = await asyncio.to_thread(self.context, claimed)
            progress = current.home_continuation or HomeGoalProgress()
            if progress.delivery is not None and progress.delivery.phase != 'settled':
                return GoalContinuationDispatchResult(binding_id=current.id,outcome='not_claimed',reason='original_home_goal_delivery_owned')
            budget = goal.budget
            if (budget.max_model_calls is None or budget.max_input_tokens is None or budget.max_output_tokens is None
                    or progress.reserved_calls >= budget.max_model_calls or progress.model_calls >= budget.max_model_calls
                    or budget.max_cost_usd is not None and (progress.cost_usd is None or progress.cost_usd >= budget.max_cost_usd)
                    or progress.input_tokens >= budget.max_input_tokens or progress.output_tokens >= budget.max_output_tokens):
                raise ValueError('home_goal_finite_budget_unavailable_or_exhausted')
            message = self.message(goal)
            if len(message.encode()) > min(16000, budget.max_input_tokens - progress.input_tokens):
                raise ValueError('home_goal_new_input_contribution_exceeds_budget')
            pin = assignment.agent_profile
            delivery_id = 'home-goal-' + uuid.uuid4().hex
            delivery = HomeGoalDelivery(id=delivery_id, requested_execution_id=delivery_id,
                goal_revision=goal.revision, requested_by=actor.identity_id, home_revision=selected.revision,
                thread_id=selected.thread_id, assignment_id=assignment.id, execution_id=assignment.execution_id,
                execution_workspace_id=assignment.execution_workspace_id, worker_id=assignment.assigned_worker_id,
                fence=assignment.fence, profile_id=pin.profile_id, profile_revision=pin.profile_revision,
                profile_record_id=pin.profile_record_id, definition=assignment.execution_profile_definition,
                message=message, message_sha256=hashlib.sha256(message.encode()).hexdigest())
            def reserve(row, state):
                if row != current or row.home_continuation and row.home_continuation.delivery and row.home_continuation.delivery.phase != 'settled':
                    raise HomeGoalReservationChanged('Original Goal delivery or binding changed before reservation')
                return row.model_copy(update={'home_continuation':progress.model_copy(update={
                    'reserved_calls':progress.reserved_calls+1, 'delivery':delivery}),
                    'last_execution_id':delivery.requested_execution_id})
            reserved = await asyncio.to_thread(self.mutate, current.id, scope, reserve, 'Original Home Goal input reserved '+delivery.id)
            def guard():
                self.authorize_input(source=SOURCE+reserved.id, assignment=assignment, actor=actor,
                    project=self.home.projects.get('home', actor.tenant), request_checksum=delivery.message_sha256,
                    input_execution_id=delivery.requested_execution_id)
            await asyncio.to_thread(guard)
            result = await self.turns.start(delivery.thread_id, TurnCreate(project_id='home',message=message,
                execution_profile_id=assignment.execution_profile_id,agent_profile_id=pin.profile_id,
                agent_profile_revision=pin.profile_revision,defer_start=True), actor=actor,
                admission_guard=guard, source=SOURCE+reserved.id, execution_id=delivery.requested_execution_id,
                allow_assignment_renewal=False)
            if not result.get('queuedId'):
                raise RuntimeError('Original Home Goal queue outcome unknown')
            def accepted(row, state):
                d = row.home_continuation.delivery
                if d.id != delivery.id:
                    # A very fast real terminal may already have settled this
                    # original and admitted the next input. Confirm the old
                    # positive effect; never overwrite the later delivery.
                    attempt=self.preflight.store.get('preflight-queued-'+result['queuedId'])
                    receipt=attempt.queued_start_receipt if attempt else None
                    if receipt is None:
                        raise ValueError('Changed delivery lacks original positive queue proof')
                    original=delivery.model_copy(update={'native_turn_id':receipt.provider_native_turn_id})
                    self.project_native_start_proof(attempt,original,row)
                    usage=self._observed_usage(row,original,scope)
                    if usage is None:
                        raise ValueError('Changed delivery lacks original settled usage')
                    events=state.events
                    if not any(e.id==self.usage_event_id(row.id,usage.id) and e.binding_id==row.id and e.goal_id==row.goal_id
                            and e.event_type=='binding_home_continuation'
                            and e.reason=='Actual original Home Goal usage '+usage.id for e in events):
                        raise ValueError('Original Home Goal accounting transition unavailable')
                    return row
                if d.phase not in {'reserved','queued','started','terminal_pending_usage','settled'}:
                    raise ValueError('Original Home Goal delivery changed after queue admission')
                if d.phase not in {'reserved','queued'}:
                    return row
                return row.model_copy(update={'home_continuation':row.home_continuation.model_copy(update={
                    'delivery':d.model_copy(update={'phase':'queued','queued_id':result['queuedId']})})})
            await asyncio.to_thread(self.mutate,reserved.id,scope,accepted,'Original Home Goal queued '+delivery.id)
            return GoalContinuationDispatchResult(binding_id=reserved.id,outcome='queued')
        except BaseException as exc:
            if reserved is not None:
                def unknown(row, state):
                    d=row.home_continuation.delivery
                    if d.id != delivery.id:
                        raise ValueError('Original delivery changed during outcome reconciliation')
                    phase=d.phase if d.phase in {'started','terminal_pending_usage','settled'} else 'unknown'
                    return row.model_copy(update={'status':GoalExecutionBindingStatus.UNKNOWN,
                        'stop_reason':'Home Goal input outcome requires original reconciliation',
                        'home_continuation':row.home_continuation.model_copy(update={'delivery':d.model_copy(update={'phase':phase})})})
                await asyncio.to_thread(self.mutate,reserved.id,scope,unknown,'Original Home Goal delivery outcome unknown')
            else:
                if isinstance(exc,HomeGoalReservationChanged):
                    return GoalContinuationDispatchResult(binding_id=claimed.id,outcome='not_claimed',reason=str(exc))
                # A stale claimant may never block a newer owner's delivery.
                blocked=[]
                def block(row, state):
                    if row != current:
                        return row
                    blocked.append(True)
                    return row.model_copy(update={'status':GoalExecutionBindingStatus.BLOCKED,'stop_reason':str(exc)})
                stopped=await asyncio.to_thread(self.mutate,claimed.id,scope,block,str(exc))
                if not blocked:
                    if isinstance(exc,asyncio.CancelledError):
                        raise
                    return GoalContinuationDispatchResult(binding_id=claimed.id,outcome='not_claimed',reason='canonical_binding_changed')
            if isinstance(exc, asyncio.CancelledError):
                raise
            return GoalContinuationDispatchResult(binding_id=claimed.id,outcome='blocked' if reserved is None else 'unknown',reason=str(exc))

    def authorize_input(self, *, source, assignment, actor, project, request_checksum=None, input_execution_id=None):
        normalized = source.removeprefix('queued:') if isinstance(source,str) else ''
        if not normalized.startswith(SOURCE):
            raise ValueError('Original canonical Home Goal source required')
        binding = self.binding(normalized[len(SOURCE):],actor.tenant)
        current, goal, owner, snapshot, selected = self.context(binding)
        progress = current.home_continuation
        d = progress.delivery if progress else None
        if (d is None or d.phase not in {'reserved','queued'} or not actor.has_same_authority(owner)
                or project.id != 'home' or selected.revision != d.home_revision
                or (snapshot.id,snapshot.execution_id,snapshot.execution_workspace_id,snapshot.assigned_worker_id,snapshot.fence,
                    snapshot.agent_profile.profile_id,snapshot.agent_profile.profile_revision,snapshot.agent_profile.profile_record_id,
                    snapshot.execution_profile_definition) != (d.assignment_id,d.execution_id,d.execution_workspace_id,d.worker_id,d.fence,
                    d.profile_id,d.profile_revision,d.profile_record_id,d.definition)
                or assignment != snapshot or goal.revision != d.goal_revision
                or (request_checksum is not None or input_execution_id is not None) and (
                    request_checksum != d.message_sha256 or input_execution_id != d.requested_execution_id)
                or current.last_execution_id != d.requested_execution_id):
            raise ValueError('Original Home Goal input/current authority or custody changed')
        return d

    def project_native_start(self, attempt):
        if attempt.status != 'started' or not attempt.queued_turn_snapshot:
            return False
        queued=QueuedTurn.model_validate(attempt.queued_turn_snapshot)
        source=(queued.source or '').removeprefix('queued:')
        if not source.startswith(SOURCE):
            return False
        correlation,receipt,evidence=(attempt.queued_start_correlation,attempt.queued_start_receipt,attempt.queued_start_evidence)
        if correlation is None or receipt is None or evidence is None:
            raise ValueError('Canonical Home Goal START proof unavailable')
        validate_attribution(self.preflight,attempt,correlation)
        scope=TenantScope(organization_id=attempt.organization_id,workspace_id=attempt.workspace_id)
        binding=self.binding(source[len(SOURCE):],scope)
        progress=binding.home_continuation
        d=progress.delivery if progress else None
        if (d is None or queued.execution_id != d.requested_execution_id
                or queued.thread_id != d.thread_id or queued.message != d.message
                or queued.agent_profile_actor_id != d.requested_by
                or (receipt.provider_native_session_id,receipt.execution_id,correlation.assignment_id,
                    correlation.worker_id,correlation.fence) != (d.thread_id,d.execution_id,d.assignment_id,d.worker_id,d.fence)
                or (receipt.provider_id,receipt.runtime_id)!=(binding.provider_id,binding.runtime_id)
                or evidence.provider_native_turn_id != receipt.provider_native_turn_id):
            raise ValueError('Original Home Goal START attribution changed')
        def started(row, state):
            current=row.home_continuation.delivery
            if current.id!=d.id or current.phase=='settled':
                return row
            if current.native_turn_id is not None and current.native_turn_id!=receipt.provider_native_turn_id:
                raise ValueError('Original Home Goal has another native START')
            if current.phase=='terminal_pending_usage':
                return row
            return row.model_copy(update={'last_turn_id':receipt.provider_native_turn_id,
                'home_continuation':row.home_continuation.model_copy(update={'delivery':current.model_copy(update={
                    'phase':'started','queued_id':queued.id,'admission_attempt_id':attempt.id,
                    'native_turn_id':receipt.provider_native_turn_id})})})
        self.mutate(binding.id,scope,started,'Canonical Home Goal START '+d.id)
        return True

    def matches_terminal_event(self,binding,event):
        """Match historical usage publication to this retained original input.

        An event supplies identity only; counters still come from attributed
        canonical usage, and an unresolved recovery cannot admit another input.
        """
        progress=binding.home_continuation
        d=progress.delivery if progress else None
        if d is None or d.phase!='terminal_pending_usage':return False
        if (event.provider_native_session_id,event.provider_native_turn_id)==(d.thread_id,d.native_turn_id):return True
        if self.restart_recovery is None:return False
        context=self.restart_recovery.goal_usage_context(binding)
        return bool(context and not context.get('pending') and any(
            (event.provider_native_session_id,event.provider_native_turn_id)==(active.thread_id,active.turn_id)
            for active in context['turns']))

    def _settle_usage(self, binding, scope):
        progress=binding.home_continuation
        d=progress.delivery if progress else None
        if d is None or d.phase!='terminal_pending_usage' or d.native_turn_id is None:
            return False
        if self.restart_recovery is not None:
            recovered=self.restart_recovery.goal_usage_context(binding)
            if recovered is not None:
                return self._settle_restarted_usage(binding,scope,recovered)
        attempt=self.preflight.store.get(d.admission_attempt_id)
        if attempt is None:
            raise ValueError('Original Home Goal terminal start unavailable')
        self.project_native_start_proof(attempt,d,binding)
        usage=self._observed_usage(binding,d,scope)
        if usage is None:
            return False
        usage_id=usage.id
        won=[]
        def settle(row, state):
            current=row.home_continuation.delivery
            if current.id!=d.id or current.phase!='terminal_pending_usage':
                return row
            won.append(True)
            observed=row.home_continuation.model_copy(update={
                'model_calls':row.home_continuation.model_calls+usage.model_call_count,
                'cost_usd':row.home_continuation.cost_usd+usage.cost_usd if row.home_continuation.cost_usd is not None and usage.cost_usd is not None else None,
                'input_tokens':row.home_continuation.input_tokens+usage.input_tokens,
                'output_tokens':row.home_continuation.output_tokens+usage.output_tokens,
                'delivery':current.model_copy(update={'phase':'settled','usage_id':usage_id})})
            status=row.status
            reason='Original Home Goal native usage observed'
            if status=='active':
                status=GoalExecutionBindingStatus.IDLE if usage.terminal_outcome=='succeeded' and current.completion_succeeded else GoalExecutionBindingStatus.BLOCKED
                if status==GoalExecutionBindingStatus.BLOCKED:
                    reason='Original Home Goal terminal/checkpoint did not succeed'
            return row.model_copy(update={'home_continuation':observed,'status':status,'stop_reason':reason,
                'lease_owner_id':None,'lease_expires_at':None,'heartbeat_at':time.time()})
        self.mutate(binding.id,scope,settle,'Actual original Home Goal usage '+usage_id,
            event_id=self.usage_event_id(binding.id,usage_id))
        return bool(won)

    @staticmethod
    def usage_event_id(binding_id,usage_id):
        return 'home-goal-usage-'+hashlib.sha256(f'{binding_id}\n{usage_id}'.encode()).hexdigest()[:32]

    def _observed_usage(self,binding,d,scope):
        usage_id='runtime-usage-'+hashlib.sha256(f'{binding.agent_session_id}\n{d.native_turn_id}'.encode()).hexdigest()[:32]
        usage=self.usage.get(usage_id)
        if (usage is None or usage.completed_at is None or usage.terminal_outcome=='unknown'
                or usage.input_tokens is None or usage.output_tokens is None or usage.runtime_duration_seconds is None
                or usage.model_call_count is None):
            return None
        if (usage.organization_id,usage.workspace_id,usage.project_id,usage.assignment_id,usage.execution_id,
                usage.execution_workspace_id,usage.agent_session_id,usage.provider_id,usage.runtime_id,
                usage.provider_native_session_id,usage.provider_native_turn_id)!=(
                scope.organization_id,scope.workspace_id,'home',d.assignment_id,d.execution_id,d.execution_workspace_id,
                binding.agent_session_id,binding.provider_id,binding.runtime_id,d.thread_id,d.native_turn_id):
            raise ValueError('Original Home Goal usage attribution changed')
        goal=self.goal(binding.goal_id,scope)
        if goal.budget.max_cost_usd is not None and usage.cost_usd is None:
            return None
        return usage

    def restart_origin(self,active,queued,attempt,*,actor):
        source=(active.source or '').removeprefix('queued:')
        if not source.startswith(SOURCE):raise ValueError('Canonical Goal origin required')
        binding=self.binding(source[len(SOURCE):],actor.tenant)
        goal=self.goal(binding.goal_id,actor.tenant)
        owner=self.home.broker.identity.actor_for_identity(goal.owner_identity_id,scope=actor.tenant)
        IdentityService.require_admin(owner)
        d=binding.home_continuation.delivery if binding.home_continuation else None
        selected=self.home.projects.get('home',actor.tenant).home_operator
        if (d is None or d.phase not in {'started','terminal_pending_usage'} or binding.status!='active'
                or binding.execution_mode!='application_continuation' or goal.status!='active'
                or goal.revision!=binding.goal_revision or d.goal_revision!=goal.revision
                or owner.principal_kind!='human' or not actor.has_same_authority(owner)
                or (binding.created_by,binding.execution_owner_id)!=(owner.identity_id,owner.identity_id)
                or selected is None or (selected.thread_id,selected.revision)!=(d.thread_id,d.home_revision)
                or (active.thread_id,active.turn_id,active.assignment_id,active.execution_id,
                    active.execution_workspace_id,active.worker_id,active.fence)!=(d.thread_id,d.native_turn_id,
                    d.assignment_id,d.execution_id,d.execution_workspace_id,d.worker_id,d.fence)):
            raise ValueError('Current original Goal restart authority changed')
        reason=self.continuation._policy_stop_reason(binding)
        if reason:raise ValueError(reason)
        self.project_native_start_proof(attempt,d,binding)
        if queued.id!=d.queued_id:raise ValueError('Goal original queue changed')
        return binding,d

    @staticmethod
    def restart_event_id(proof):
        return 'home-goal-restart-'+hashlib.sha256(proof.recovery_id.encode()).hexdigest()[:32]

    def reserve_restart(self,binding,delivery,proof):
        goal=self.goal(binding.goal_id,TenantScope(organization_id=binding.organization_id,workspace_id=binding.workspace_id))
        scope=TenantScope(organization_id=binding.organization_id,workspace_id=binding.workspace_id)
        claimed=self.bindings.claim_continuation(binding.id,scope=scope,owner_id=self.continuation.owner_id,
            lease_seconds=self.continuation.lease_seconds)
        if claimed is None:raise ValueError('Original Goal continuation lease unavailable')
        def reserve(row,state):
            if row.home_continuation.delivery!=delivery or row.goal_revision!=proof.goal_revision or row.status!='active':
                raise ValueError('Goal restart reservation changed')
            if any(e.id==self.restart_event_id(proof) for e in state.events):return row
            progress=row.home_continuation;budget=goal.budget
            if (budget.max_model_calls is None or budget.max_input_tokens is None or budget.max_output_tokens is None
                    or progress.reserved_calls>=budget.max_model_calls or progress.model_calls>=budget.max_model_calls
                    or progress.input_tokens>=budget.max_input_tokens or progress.output_tokens>=budget.max_output_tokens
                    or budget.max_cost_usd is not None and (progress.cost_usd is None or progress.cost_usd>=budget.max_cost_usd)):
                raise ValueError('Goal restart finite budget unavailable or exhausted')
            return row.model_copy(update={'home_continuation':progress.model_copy(update={'reserved_calls':progress.reserved_calls+1})})
        return self.mutate(binding.id,scope,reserve,'Home Goal restart input reserved '+proof.recovery_id,
            event_id=self.restart_event_id(proof))

    def validate_restart_reservation(self,binding,delivery,proof,reservation,*,require_lease=True):
        scope=TenantScope(organization_id=binding.organization_id,workspace_id=binding.workspace_id)
        # This witness is recorded only AFTER the atomic Goal budget reservation.
        # The existing recovery record is a point read; no event-history scan at
        # the synchronous final native guard is permitted.
        if (not isinstance(reservation,dict) or set(reservation)!={'binding_id','delivery_id','goal_revision','reserved_calls','event_id'}
                or (reservation['binding_id'],reservation['delivery_id'],reservation['goal_revision'],reservation['event_id'])!=
                    (binding.id,delivery.id,binding.goal_revision,self.restart_event_id(proof))
                or type(reservation['reserved_calls']) is not int or reservation['reserved_calls']<1):
            raise ValueError('Canonical Goal restart reservation unavailable')
        goal=self.goal(binding.goal_id,scope);progress=binding.home_continuation
        budget=goal.budget
        if ((require_lease and (binding.lease_owner_id!=self.continuation.owner_id or binding.lease_expires_at is None
                or binding.lease_expires_at<=time.time()))
                or budget.max_model_calls is None or progress.reserved_calls>budget.max_model_calls
                or progress.model_calls>=budget.max_model_calls
                or progress.reserved_calls<reservation['reserved_calls']
                or budget.max_input_tokens is None or progress.input_tokens>=budget.max_input_tokens
                or budget.max_output_tokens is None or progress.output_tokens>=budget.max_output_tokens
                or budget.max_cost_usd is not None and (progress.cost_usd is None or progress.cost_usd>=budget.max_cost_usd)):
            raise ValueError('Current Goal restart budget changed')

    def _settle_restarted_usage(self,binding,scope,context):
        if context.get('pending'):
            return False
        d=binding.home_continuation.delivery
        observed=[];missing=False
        for active in context['turns']:
            snapshot=d.model_copy(update={'thread_id':active.thread_id,'native_turn_id':active.turn_id,
                'assignment_id':active.assignment_id,'execution_id':active.execution_id,
                'execution_workspace_id':active.execution_workspace_id,'worker_id':active.worker_id,'fence':active.fence})
            usage=self._observed_usage(binding,snapshot,scope)
            if usage is None:
                missing=True
                continue
            observed.append(usage)
            def account(row,state):
                current=row.home_continuation
                if current.delivery.id!=d.id:return row
                if any(e.id==self.usage_event_id(row.id,usage.id) for e in state.events):return row
                return row.model_copy(update={'home_continuation':current.model_copy(update={
                    'model_calls':current.model_calls+usage.model_call_count,
                    'input_tokens':current.input_tokens+usage.input_tokens,'output_tokens':current.output_tokens+usage.output_tokens,
                    'cost_usd':current.cost_usd+usage.cost_usd if current.cost_usd is not None and usage.cost_usd is not None else None})})
            self.mutate(binding.id,scope,account,'Actual original Home Goal usage '+usage.id,
                event_id=self.usage_event_id(binding.id,usage.id))
        if missing:
            return False
        current=self.binding(binding.id,scope)
        if current.home_continuation.delivery.id!=d.id:return False
        # Retain accounted usage BEFORE releasing this original delivery. If the
        # point write fails, existing terminal reconciliation retries this phase;
        # no settled row can be stranded without its history witness.
        self.restart_recovery.retain_goal_settlement(current,context,[usage.id for usage in observed])
        latest=observed[-1];won=[]
        def settled(row,state):
            current=row.home_continuation.delivery
            if current.id!=d.id or current.phase!='terminal_pending_usage':return row
            won.append(True)
            status=row.status
            if status=='active':
                success=context['succeeded'] and latest.terminal_outcome=='succeeded' and all(u.terminal_outcome!='failed' for u in observed)
                status=GoalExecutionBindingStatus.IDLE if success else GoalExecutionBindingStatus.BLOCKED
            return row.model_copy(update={'status':status,'stop_reason':'Original and recovered Goal native usage observed',
                'lease_owner_id':None,'lease_expires_at':None,'home_continuation':row.home_continuation.model_copy(update={
                    'delivery':current.model_copy(update={'phase':'settled','usage_id':observed[0].id,
                        'completion_succeeded':context['succeeded']})})})
        self.mutate(binding.id,scope,settled,'Home Goal recovered chain settled '+context['turns'][-1].turn_id)
        return bool(won)

    async def record_restarted_terminal(self,active,*,succeeded,recovery):
        assignment=await asyncio.to_thread(self.home.broker.execution_workers.store.assignment,active.assignment_id)
        if assignment is None:raise ValueError('Recovery terminal tenant unavailable')
        scope=TenantScope(organization_id=assignment.organization_id,workspace_id=assignment.workspace_id)
        evidence=await asyncio.to_thread(recovery.retain_goal_terminal,active,succeeded=succeeded)
        if evidence is None:return
        binding=await asyncio.to_thread(self.binding,evidence['binding_id'],scope)
        d=binding.home_continuation.delivery
        if d is None or (d.id,binding.goal_revision)!=(evidence['delivery_id'],evidence['goal_revision']):return
        def pending(row,state):
            current=row.home_continuation.delivery
            if current.id!=d.id or current.phase=='settled':return row
            return row.model_copy(update={'last_turn_id':active.turn_id,'home_continuation':row.home_continuation.model_copy(update={
                'delivery':current.model_copy(update={'phase':'terminal_pending_usage','completion_succeeded':bool(succeeded)})})})
        await asyncio.to_thread(self.mutate,binding.id,scope,pending,'Home Goal recovery native terminal '+active.turn_id)
        binding=await asyncio.to_thread(self.binding,binding.id,scope)
        settled=await asyncio.to_thread(self._settle_usage,binding,scope)
        if settled:
            current=await asyncio.to_thread(self.binding,binding.id,scope)
            if current.status=='idle':await self.continuation.dispatch_once(current.id,scope=scope)

    def project_native_start_proof(self, attempt, d, binding):
        correlation,receipt,evidence=(attempt.queued_start_correlation,attempt.queued_start_receipt,attempt.queued_start_evidence)
        if attempt.status!='started' or correlation is None or receipt is None or evidence is None:
            raise ValueError('Original Home Goal positive START required')
        validate_attribution(self.preflight,attempt,correlation)
        queued=QueuedTurn.model_validate(attempt.queued_turn_snapshot)
        if (queued.source!=SOURCE+binding.id or queued.execution_id!=d.requested_execution_id or queued.message!=d.message
                or queued.thread_id!=d.thread_id or queued.agent_profile_actor_id!=d.requested_by
                or (receipt.provider_native_session_id,receipt.provider_native_turn_id,receipt.execution_id,
                    correlation.assignment_id,correlation.worker_id,correlation.fence)!=(
                    d.thread_id,d.native_turn_id,d.execution_id,d.assignment_id,d.worker_id,d.fence)):
            raise ValueError('Original Home Goal terminal proof changed')

    async def record_terminal(self, active, *, succeeded):
        source=(active.source or '').removeprefix('queued:')
        if not source.startswith(SOURCE):
            return
        scope=None
        def record():
            nonlocal scope
            # Active provenance is only a locator. Canonical binding, START and
            # exact original native turn remain mandatory before accounting.
            assignment=self.home.broker.execution_workers.store.assignment(active.assignment_id)
            if assignment is None:
                raise ValueError('Original Home Goal terminal tenant unavailable')
            scope=TenantScope(organization_id=assignment.organization_id,workspace_id=assignment.workspace_id)
            binding=self.binding(source[len(SOURCE):],scope)
            d=binding.home_continuation.delivery if binding.home_continuation else None
            if d is None or d.phase=='settled':
                return False
            # The canonical preflight key is a point, never a history scan.
            if d.queued_id is None:
                raise ValueError('Original Home Goal queued identity unavailable')
            attempt=self.preflight.store.get('preflight-queued-'+d.queued_id)
            if attempt is None:
                raise ValueError('Original Home Goal native admission missing')
            self.project_native_start(attempt)
            binding=self.binding(binding.id,scope);d=binding.home_continuation.delivery
            self.project_native_start_proof(attempt,d,binding)
            if (active.thread_id,active.turn_id,active.assignment_id,active.execution_id,
                    active.execution_workspace_id,active.worker_id,active.fence)!=(d.thread_id,d.native_turn_id,
                    d.assignment_id,d.execution_id,d.execution_workspace_id,d.worker_id,d.fence):
                raise ValueError('Original Home Goal terminal custody changed')
            def terminal(row, state):
                current=row.home_continuation.delivery
                if current.id!=d.id or current.phase=='settled':return row
                return row.model_copy(update={'home_continuation':row.home_continuation.model_copy(update={
                    'delivery':current.model_copy(update={'phase':'terminal_pending_usage','completion_succeeded':bool(succeeded)})})})
            self.mutate(binding.id,scope,terminal,'Original Home Goal native terminal '+d.id)
            return self._settle_usage(self.binding(binding.id,scope),scope)
        settled=await asyncio.to_thread(record)
        if settled and scope is not None:
            current=await asyncio.to_thread(self.binding,source[len(SOURCE):],scope)
            if current.status=='idle':
                # Same existing Goal owner, rechecking canonical stop/budget gates.
                await self.continuation.dispatch_once(current.id,scope=scope)
