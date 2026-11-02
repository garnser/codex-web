"""Event-only deterministic procedures owned by existing AutomationRun/checkpoints."""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
from dataclasses import dataclass

from codex_web.artifact_evidence import ArtifactCreate, sha256_digest
from codex_web.automation_definitions import AutomationTargetKind
from codex_web.automation_runs import AutomationRunStatus, AutomationRunTrigger, ProcedureAutomationCustody
from codex_web.identity import TenantScope
from codex_web.services.procedure_execution import bounded_data
from codex_web.storage.state_store import storage_operation_budget


class ProcedureAutomationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class ProcedureSelection:
    actor: object
    assignment: object
    values: dict
    observation: str


class ProcedureAutomationService:
    """No native input, assignment creation, authority grant or polling owner."""
    def __init__(self, *, runs, engine, identity, routing, threads, continuity,
                 get_work, events, event_triggers, controller, coordinator,
                 automation_execution, startup_limit=64):
        self.runs, self.engine, self.identity = runs, engine, identity
        self.routing, self.threads, self.continuity = routing, threads, continuity
        self.get_work, self.events, self.event_triggers = get_work, events, event_triggers
        self.controller, self.coordinator = controller, coordinator
        self.automation_execution = automation_execution
        self.startup_limit = max(1, min(128, startup_limit))

    def _get(self, run):
        return self.runs.store.get(run.id, organization_id=run.organization_id, workspace_id=run.workspace_id)

    def _definition(self, run, *, effects=True):
        automation, reference = self.runs.definitions.resolve_reference(run.definition_ref,
            organization_id=run.organization_id, workspace_id=run.workspace_id, project_id=run.project_id)
        current, current_ref = self.runs.definitions.resolve(run.automation_id,
            organization_id=run.organization_id, workspace_id=run.workspace_id, project_id=run.project_id)
        if (reference != current_ref or automation != current or automation.lifecycle != 'enabled'
                or automation.target.kind != AutomationTargetKind.CANONICAL_PROCEDURE):
            raise ProcedureAutomationError('procedure_automation_definition_changed')
        actor = self.identity.actor_for_identity(automation.owner_identity_id,
            scope=TenantScope(organization_id=run.organization_id, workspace_id=run.workspace_id))
        event = self.events.event(run.trigger.event_id) if run.trigger.event_id else None
        if (event is None or event.event_type != 'work.transition' or run.trigger.kind != 'canonical_event'
                or not str(event.source).startswith('autonomy-watchdog:')
                or (event.tenant_id, event.workspace_id) != (run.organization_id, run.workspace_id)
                or str(event.payload.get('project_id') or '') != run.project_id
                or not self.event_triggers._filter_matches(event.payload, automation.trigger.event_filter)):
            raise ProcedureAutomationError('procedure_automation_event_changed')
        if effects:
            control = self.controller.store.control()
            if (control.mode != 'active' or control.dry_run or control.simulation
                    or self.controller._scoped_pause_reason(control, actor=actor, project_id=run.project_id)
                    or self.controller._exclusive_goal_scope_reason(control, event)):
                raise ProcedureAutomationError('procedure_automation_controls_blocked')
            if run.started_at is not None and self.runs.clock() >= run.started_at + automation.budget.max_duration_seconds:
                raise ProcedureAutomationError('procedure_automation_duration_exhausted')
        return automation, actor, event

    async def _select(self, automation, actor, event, *, original_values=None):
        ref = str(event.payload.get('work_item_ref') or event.payload.get('workItemRef') or '')
        state = await asyncio.to_thread(self.get_work, ref)
        if ((state.organization_id, state.workspace_id, state.project_id)
                != (actor.organization_id, actor.workspace_id, str(event.payload.get('project_id') or ''))
                or not state.resource_ids or state.handoff is None or state.handoff.status not in {'pending','accepted'}):
            raise ProcedureAutomationError('procedure_automation_handoff_absent_or_scope_changed')
        snapshot = await asyncio.to_thread(self.routing.snapshot, state)
        handoff_owner = state.handoff.from_agent if state.handoff.status == 'pending' else state.handoff.to_agent
        if snapshot.owner(state.current_owner) != snapshot.owner(handoff_owner):
            raise ProcedureAutomationError('procedure_automation_work_owner_changed')
        supervisor_role = snapshot.owner(automation.target.supervisor_role)
        if not supervisor_role or supervisor_role not in snapshot.coordination_owners:
            raise ProcedureAutomationError('procedure_automation_supervisor_role_unresolved')

        def assignment_for(role, *, supervisor_owner=False):
            binding = self.routing.existing_binding(state, role)
            profile, requester = self.routing.executor(state, binding, role, lookup_actor=lambda:actor)
            bootstrap = self.routing.bootstraps.get_by_thread(binding.thread_id, actor)
            assignment = self.engine.workers.store.assignment(bootstrap.assignment_id)
            if (assignment is None or (supervisor_owner and (requester.identity_id != actor.identity_id
                    or assignment.created_by != actor.identity_id))
                    or (assignment.organization_id, assignment.workspace_id, assignment.project_id)
                        != (state.organization_id, state.workspace_id, state.project_id)
                    or (assignment.execution_id, assignment.execution_workspace_id)
                        != (bootstrap.execution_id, bootstrap.execution_workspace_id)
                    or assignment.agent_profile is None or profile.profile_id != assignment.agent_profile.profile_id):
                raise ProcedureAutomationError('procedure_automation_bootstrap_changed')
            return binding, assignment

        binding, supervisor = await asyncio.to_thread(assignment_for, supervisor_role, supervisor_owner=True)
        await asyncio.to_thread(self.engine._live, supervisor)
        if set(state.resource_ids) - set(supervisor.resource_ids):
            raise ProcedureAutomationError('procedure_automation_target_context_missing')
        await asyncio.to_thread(self.engine.skills.validate_required_procedure,
            automation.target.procedure_ref, actor=actor, project_id=state.project_id)
        from codex_web.skills import SkillDefinition
        record = await asyncio.to_thread(self.engine.skills.definitions.get_record,
            automation.target.procedure_ref.record_id)
        operational = SkillDefinition.model_validate(record.payload).operational_procedure
        if operational is None or operational.schema_version != '1.4':
            raise ProcedureAutomationError('procedure_automation_schema_required')
        await asyncio.to_thread(self.engine._targets, operational, supervisor, actor)
        observed = await self.threads.runner(binding.thread_id)
        runner, activity = observed.get('runner') or {}, observed.get('activity') or {}
        if (runner.get('assignmentId') != supervisor.id or runner.get('fence') != supervisor.fence
                or runner.get('live') is not True or runner.get('processRunning') is not True
                or runner.get('pendingExecutorWork') != 0 or activity.get('state') != 'idle'
                or activity.get('turnId') is not None or self.continuity.thread_is_active(binding.thread_id)):
            raise ProcedureAutomationError('procedure_automation_supervisor_not_idle')
        recipient_role = snapshot.owner(state.handoff.to_agent)
        recipient_binding, recipient = await asyncio.to_thread(assignment_for, recipient_role)
        target = recipient
        if original_values is not None:
            from codex_web.services.procedure_custody_recovery import CustodyInput
            pinned = CustodyInput.model_validate(original_values, strict=True)
            if (pinned.work_item_ref != state.ref
                    or pinned.expected_handoff_requested_at != state.handoff.requested_at):
                raise ProcedureAutomationError('procedure_automation_original_handoff_changed')
            target = await asyncio.to_thread(self.engine.workers.store.assignment, pinned.target_assignment_id)
            if target is None or target.fence != pinned.expected_target_fence:
                raise ProcedureAutomationError('procedure_automation_original_target_changed')
        if (target.id == supervisor.id or set(target.resource_ids) - set(supervisor.resource_ids)
                or (target.organization_id,target.workspace_id,target.project_id)
                    != (state.organization_id,state.workspace_id,state.project_id)):
            raise ProcedureAutomationError('procedure_automation_target_context_missing')
        values = original_values or {'work_item_ref':state.ref, 'target_assignment_id':target.id,
            'expected_target_fence':target.fence, 'expected_handoff_requested_at':state.handoff.requested_at}
        target_observation = await self.threads.runner(recipient_binding.thread_id)
        target_runner, target_activity = target_observation.get('runner') or {}, target_observation.get('activity') or {}
        # Closed canonical facts, not event prose, provider output or observations
        # promoted into authority. Unchanged watchdog windows cannot advance.
        observation = digest({'values':values, 'owner':state.current_owner, 'next_owner':state.next_owner,
            'handoff_status':str(state.handoff.status),
            'acknowledged_at':state.handoff.acknowledged_at,
            'delivery':state.handoff.delivery.model_dump(mode='json') if state.handoff.delivery else None,
            'target_status':str(target.status), 'target_lease_state':
                'absent' if target.lease is None else 'expired' if target.lease.expires_at <= self.runs.clock() else 'live',
            'recipient_assignment_id':recipient.id, 'recipient_fence':recipient.fence,
            'recipient_activity':{k:target_activity.get(k) for k in ('state','turnId')},
            'recipient_runner':{k:target_runner.get(k) for k in ('assignmentId','fence','live','processRunning','pendingExecutorWork')},
            'supervisor_id':supervisor.id, 'supervisor_fence':supervisor.fence})
        return ProcedureSelection(actor, supervisor, values, observation)

    def _original_values(self, run, actor):
        custody = run.procedure_custody
        if custody is None:
            return None
        assignment = self.engine.get(custody.assignment_id, actor)
        raw, _ = self.engine._content(custody.input_snapshot_id, actor, assignment)
        values = bounded_data(json.loads(raw))
        if (digest(values) != custody.input_snapshot_sha256
                or values.get('work_item_ref') != run.work_item_ref
                or values.get('expected_handoff_requested_at') != custody.handoff_requested_at):
            raise ProcedureAutomationError('procedure_automation_input_snapshot_changed')
        return values

    def _snapshot(self, run, selection):
        data = json.dumps(bounded_data(selection.values), sort_keys=True, separators=(',', ':')).encode()
        a = selection.assignment
        previous = a.procedure_checkpoint
        if previous is not None and previous.definition == self._definition(run)[0].target.procedure_ref:
            prior, _ = self.engine._content(previous.input_snapshot_id, selection.actor, a,
                previous.input_snapshot_checksum)
            if digest(bounded_data(json.loads(prior))) == digest(selection.values):
                return self.engine.evidence.get_artifact(previous.input_snapshot_id, actor=selection.actor)
        artifact = self.engine.evidence.create_artifact(ArtifactCreate(project_id=a.project_id,
            work_item_ref=selection.values['work_item_ref'], execution_id=a.execution_id,
            execution_workspace_id=a.execution_workspace_id, resource_ids=a.resource_ids,
            artifact_type='report', name='Canonical procedure input', source='automation-run:'+run.id,
            digest=sha256_digest(data)), actor=selection.actor)
        return self.engine.evidence.attach_artifact_content(artifact.id, (data,), actor=selection.actor)

    @staticmethod
    def _key(reference, procedure, values):
        return reference.record_id + ':procedure:' + digest({'definition':procedure.model_dump(mode='json'), 'inputs':values})

    async def handle_match(self, event, match):
        if match.automation.target.kind != AutomationTargetKind.CANONICAL_PROCEDURE:
            return None
        if event.event_type != 'work.transition' or not str(event.source).startswith('autonomy-watchdog:'):
            return {'handled':True,'code':'procedure_automation_event_source_unavailable'}
        actor = await asyncio.to_thread(self.identity.actor_for_identity, match.automation.owner_identity_id,
            scope=TenantScope(organization_id=event.tenant_id, workspace_id=event.workspace_id))
        retained = []
        try:
            ref = str(event.payload.get('work_item_ref') or event.payload.get('workItemRef') or '')
            state = await asyncio.to_thread(self.get_work, ref)
            retained = await asyncio.to_thread(self.runs.store.list,
                organization_id=event.tenant_id, workspace_id=event.workspace_id, automation_id=match.automation_id)
            same = [r for r in retained if r.definition_ref == match.definition_ref and r.work_item_ref == ref
                and r.procedure_custody is not None and state.handoff is not None
                and r.procedure_custody.handoff_requested_at == state.handoff.requested_at]
            if len(same)>1:
                raise ProcedureAutomationError('procedure_automation_original_ambiguous')
            existing = same[0] if same else None
            values = await asyncio.to_thread(self._original_values, existing, actor) if existing else None
            selection = await self._select(match.automation, actor, event, original_values=values)
        except Exception as exc:
            # Explicit procedure policy refuses; never fall through to an LLM.
            code = getattr(exc,'code','procedure_automation_selection_unavailable')
            for current in retained:
                if current.work_item_ref == ref and current.status in {'admitted','running'}:
                    await asyncio.to_thread(self._note,current,code)
            return {'handled':True,'code':code}
        admission = await asyncio.to_thread(self.runs.admit, match.automation_id,
            AutomationRunTrigger(kind='canonical_event', source_id=event.event_id, event_id=event.event_id,
                occurred_at=event.occurred_at, correlation_id=event.correlation_id, causation_id=event.causation_id),
            organization_id=event.tenant_id, workspace_id=event.workspace_id,
            project_id=selection.assignment.project_id, definition_ref=match.definition_ref,
            dedupe_key_override=self._key(match.definition_ref, match.automation.target.procedure_ref, selection.values),
            work_item_ref=selection.values['work_item_ref'])
        run = admission.run
        if run.status in {AutomationRunStatus.ADMITTED, AutomationRunStatus.RUNNING}:
            self.schedule_launch(run, event_id=event.event_id, revision=selection.observation)
        return {'handled':True,'run_id':run.id,'status':str(run.status),'code':run.result_code or run.block_code}

    async def handle_event(self, event):
        matches = [m for m in self.event_triggers.matches(event)
            if m.automation.target.kind == AutomationTargetKind.CANONICAL_PROCEDURE]
        if not matches:
            return None
        if len(matches) != 1:
            return {'handled':True,'code':'procedure_automation_policy_ambiguous'}
        return await self.handle_match(event, matches[0])

    def schedule_launch(self, run, *, event_id=None, revision=None):
        if run.target_kind != 'canonical_procedure' or run.status not in {'admitted','running'}:
            return False
        context = contextvars.copy_context()
        context.run(storage_operation_budget.set, None)
        return context.run(self.coordinator.schedule, 'canonical-procedure:'+run.id,
            lambda:self._owned(run,event_id=event_id, revision=revision),
            revision=revision or 'resume:'+str(run.updated_at),
            scope='canonical-procedure:'+run.organization_id+':'+run.workspace_id)

    def _note(self, run, code):
        def update(state):
            for index,row in enumerate(state.runs):
                if row.id == run.id and row.definition_ref == run.definition_ref and row.status in {'admitted','running'}:
                    state.runs[index] = row.model_copy(update={'result_code':str(code)[:200],
                        'result_reason':'Canonical procedure has not proved completion', 'updated_at':self.runs.clock()})
            return state
        self.runs.store.update(update)

    async def _owned(self, run, *, event_id, revision):
        from codex_web.services.post_turn_readiness import PostTurnReadinessService
        async def operation():
            await self._drive(run,event_id=event_id,revision=revision)
        try:
            automation, _, _ = await asyncio.to_thread(self._definition,run)
            seconds = automation.budget.max_duration_seconds
            if run.started_at is not None:
                seconds = max(0.001, min(seconds, run.started_at+seconds-self.runs.clock()))
            return await PostTurnReadinessService._run_owned(operation,seconds=seconds,name='canonical-procedure-'+run.id)
        except BaseException as exc:
            context = contextvars.copy_context()
            context.run(storage_operation_budget.set,None)
            task = context.run(asyncio.create_task,asyncio.to_thread(self._note,run,
                getattr(exc,'code','procedure_automation_owner:'+type(exc).__name__)))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
            if isinstance(exc,asyncio.CancelledError):
                raise

    def _claim(self, run, selection, automation, artifact):
        a = selection.assignment
        custody = ProcedureAutomationCustody(assignment_id=a.id,worker_id=a.assigned_worker_id,
            execution_id=a.execution_id,execution_workspace_id=a.execution_workspace_id,fence=a.fence,
            definition=automation.target.procedure_ref,input_snapshot_id=artifact.id,
            input_snapshot_sha256=digest(selection.values),
            handoff_requested_at=selection.values['expected_handoff_requested_at'])
        def update(state):
            for index,row in enumerate(state.runs):
                if (row.id == run.id and row.definition_ref == run.definition_ref
                        and row.dedupe_key == run.dedupe_key and row.status == 'admitted'
                        and row.procedure_custody is None):
                    if any(other.id != row.id and other.automation_id == row.automation_id
                            and (other.organization_id,other.workspace_id)==(row.organization_id,row.workspace_id)
                            and other.status=='running' for other in state.runs):
                        return state
                    state.runs[index] = row.model_copy(update={'status':AutomationRunStatus.RUNNING,
                        'procedure_custody':custody,'execution_ids':(a.execution_id,),
                        'started_at':self.runs.clock(),'updated_at':self.runs.clock()})
            return state
        self.runs.store.update(update)
        return self._get(run)

    def _cas_custody(self, run, before, after, **changes):
        changed = False
        def update(state):
            nonlocal changed
            for index,row in enumerate(state.runs):
                if (row.id == run.id and row.definition_ref==run.definition_ref and row.status=='running'
                        and row.procedure_custody==before):
                    state.runs[index] = row.model_copy(update={'procedure_custody':after,
                        'updated_at':self.runs.clock(),**changes})
                    changed = True
            return state
        self.runs.store.update(update)
        if not changed:
            raise ProcedureAutomationError('procedure_automation_run_claim_changed')
        return self._get(run)

    @staticmethod
    def _same_selection(custody, selection, automation):
        a=selection.assignment
        if ((custody.assignment_id,custody.worker_id,custody.execution_id,custody.execution_workspace_id,custody.fence)
                != (a.id,a.assigned_worker_id,a.execution_id,a.execution_workspace_id,a.fence)
                or custody.definition!=automation.target.procedure_ref
                or custody.input_snapshot_sha256!=digest(selection.values)):
            raise ProcedureAutomationError('procedure_automation_original_custody_or_handoff_changed')

    async def _drive(self, original, *, event_id, revision):
        run = await asyncio.to_thread(self._get,original)
        if run.status not in {'admitted','running'}:
            return
        automation,actor,event = await asyncio.to_thread(self._definition,run)
        values = await asyncio.to_thread(self._original_values,run,actor)
        selection = await self._select(automation,actor,event,original_values=values)
        if run.dedupe_key!=self._key(run.definition_ref,automation.target.procedure_ref,selection.values):
            raise ProcedureAutomationError('procedure_automation_original_input_changed')
        if automation.approval_required:
            if run.approval_request_id is None:
                await self.automation_execution._wait_for_approval(run,automation,actor=actor,
                    work_item_ref=selection.values['work_item_ref'])
                return
            request = self.automation_execution.approvals.get(run.approval_request_id,actor=actor)
            if request.status != 'consumed':
                raise ProcedureAutomationError('procedure_automation_approval_required')
        if run.procedure_custody is None:
            artifact = await asyncio.to_thread(self._snapshot,run,selection)
            run = await asyncio.to_thread(self._claim,run,selection,automation,artifact)
        custody = run.procedure_custody
        if run.status!='running' or custody is None:
            return
        self._same_selection(custody,selection,automation)
        assignment = await asyncio.to_thread(self.engine.start,custody.assignment_id,
            definition=custody.definition,input_snapshot_id=custody.input_snapshot_id,
            actor=actor,expected_fence=custody.fence)
        checkpoint = assignment.procedure_checkpoint
        if custody.checkpoint_id is None:
            run = await asyncio.to_thread(self._cas_custody,run,custody,
                custody.model_copy(update={'checkpoint_id':checkpoint.id}))
            custody=run.procedure_custody
        if checkpoint.id != custody.checkpoint_id:
            raise ProcedureAutomationError('procedure_automation_checkpoint_changed')
        if (custody.last_material_observation_sha256==selection.observation
                and checkpoint.status in {'waiting','reconciliation_required','awaiting_approval'}):
            return
        # Pure ready steps drain in one bounded event activation, never a timer.
        while custody.advances_started < automation.target.max_advances:
            automation,actor,event = await asyncio.to_thread(self._definition,run)
            selection = await self._select(automation,actor,event,original_values=selection.values)
            self._same_selection(custody,selection,automation)
            next_custody=custody.model_copy(update={'advances_started':custody.advances_started+1,
                'last_material_observation_sha256':selection.observation,
                'last_activation_event_id':event_id or run.trigger.event_id})
            run = await asyncio.to_thread(self._cas_custody,run,custody,next_custody)
            custody=run.procedure_custody
            result=await self.engine.advance(custody.assignment_id,actor=actor,
                run_id=custody.checkpoint_id,expected_fence=custody.fence)
            checkpoint=result.procedure_checkpoint
            if checkpoint.id!=custody.checkpoint_id:
                raise ProcedureAutomationError('procedure_automation_checkpoint_changed')
            if checkpoint.status in {'completed','recovered'}:
                await asyncio.to_thread(self.reconcile,run.id,actor=actor)
                return
            if checkpoint.status!='ready':
                await asyncio.to_thread(self._note,run,checkpoint.failure_code or checkpoint.status)
                return
        await asyncio.to_thread(self._note,run,'procedure_automation_advance_budget_exhausted')

    def reconcile(self, run_id, *, actor, cancel_unknown=False):
        run=self.runs.store.get(run_id,organization_id=actor.organization_id,workspace_id=actor.workspace_id)
        if cancel_unknown:
            raise ProcedureAutomationError('procedure_automation_unknown_cancellation_unavailable')
        custody=run.procedure_custody
        if run.target_kind!='canonical_procedure' or run.status!='running' or custody is None:
            return run
        _,owner,_=self._definition(run,effects=False)
        assignment=self.engine.get(custody.assignment_id,owner)
        if ((assignment.id,assignment.assigned_worker_id,assignment.execution_id,
                assignment.execution_workspace_id,assignment.fence) !=
                (custody.assignment_id,custody.worker_id,custody.execution_id,
                    custody.execution_workspace_id,custody.fence)):
            raise ProcedureAutomationError('procedure_automation_original_custody_changed')
        checkpoint=assignment.procedure_checkpoint
        if (checkpoint is None or checkpoint.id!=custody.checkpoint_id
                or checkpoint.definition!=custody.definition
                or checkpoint.input_snapshot_id!=custody.input_snapshot_id
                or checkpoint.status not in {'completed','recovered'}):
            return run
        self._original_values(run,owner)
        procedure=self.engine._procedure(assignment,owner)
        for step in procedure.steps:
            if checkpoint.steps[step.id].status=='succeeded':
                self.engine._proof(assignment,step,checkpoint.steps[step.id],owner)
        evidence_ids=tuple(dict.fromkeys(e for step in checkpoint.steps.values() for e in step.evidence.values()))
        return self._cas_custody(run,custody,custody,status=AutomationRunStatus.SUCCEEDED,
            evidence_ids=evidence_ids,result_code='procedure_checkpoint_verified',
            result_reason='Exact governed procedure checkpoint completed',completed_at=self.runs.clock())

    async def recover_pending(self):
        # Called once at actual service startup. No periodic registration.
        state=await asyncio.to_thread(self.runs.store.load)
        active=[r for r in state.runs if r.target_kind=='canonical_procedure' and r.status in {'admitted','running'}]
        for run in sorted(active,key=lambda r:(r.created_at,r.id))[:self.startup_limit]:
            self.schedule_launch(run,revision='startup:'+str(run.updated_at))
        return {'scheduled_candidates':min(len(active),self.startup_limit),'complete':len(active)<=self.startup_limit}
