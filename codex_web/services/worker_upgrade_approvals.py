"""Request-only Upgrade bridge for genuine, fenced agent executions.

The worker is the requester; its originating human authorizes only the request
operation. Human decisions and guarded Upgrade execution remain separate APIs.
"""
import asyncio
import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from codex_web.approval_requests import ApprovalRequestCreate, ApprovalRequirement
from codex_web.control_plane_broker import ControlPlaneBrokerAuditEvent, ControlPlaneBrokerDecision
from codex_web.services.worker_verifications import WorkerVerificationAdapter
from codex_web.upgrades import UpgradeStepStatus, UpgradeStatus


class AgentUpgradeApprovalRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    plan_id: str = Field(min_length=1, max_length=200)
    step_id: str = Field(min_length=1, max_length=200)
    expected_target_fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')


class _ExistingRequest(Exception):
    def __init__(self, record):
        self.record = record


class WorkerUpgradeApprovalAdapter(WorkerVerificationAdapter):
    def principals(self, *args, **kwargs):
        from codex_web.services.execution_workers import ExecutionWorkerError
        try:
            return super().principals(*args, **kwargs)
        except ExecutionWorkerError:
            self.denied('Agent assignment is no longer live or authorized')

    def _workspace(self, assignment, requester):
        service = self.broker.execution_workspaces
        workspace = service.get(assignment.execution_workspace_id, requester)
        lease = service.store.lease(workspace.lease_id)
        if (workspace.status != 'active' or workspace.execution_id != assignment.execution_id
                or workspace.subject != assignment.subject
                or workspace.owner_identity_id != requester.identity_id
                or lease is None or lease.released_at is not None
                or lease.subject != workspace.subject
                or lease.organization_id != workspace.organization_id
                or lease.workspace_id != workspace.workspace_id
                or lease.expires_at <= self.broker._clock()
                or lease.execution_id != workspace.execution_id
                or lease.execution_workspace_id != workspace.id
                or lease.owner_identity_id != requester.identity_id
                or lease.resource_modes.get(self.broker._writable_repository_id(assignment)) != 'write'):
            self.denied('Agent approval requires its current active writable workspace lease')

    async def request(self, assignment, worker_actor, resolved, payload, validator):
        try:
            data = AgentUpgradeApprovalRequest.model_validate(payload)
        except ValidationError:
            self.denied('Only plan, step and exact target fingerprint may be supplied')
        b, upgrades = self.broker, self.broker.upgrades
        if upgrades is None:
            self.denied('Canonical Upgrade service unavailable')
        worker, authorizer, decision = await asyncio.to_thread(
            self.principals, assignment, worker_actor, resolved, validator, payload)
        await asyncio.to_thread(self._workspace, assignment, authorizer)
        from codex_web.services.upgrades import UpgradeError
        try:
            plan = await asyncio.to_thread(upgrades._open_plan, data.plan_id, actor=worker)
        except UpgradeError:
            self.denied('Upgrade unavailable in the requesting agent tenant')

        def binding(current):
            if current.project_id is None or current.project_id != assignment.project_id:
                self.denied('Upgrade Project is outside the requesting assignment')
            if current.status in {UpgradeStatus.COMPLETED, UpgradeStatus.FAILED, UpgradeStatus.ROLLED_BACK}:
                self.denied('Terminal Upgrade cannot request agent approval')
            step = next((s for s in current.steps if s.id == data.step_id), None)
            if (step is None or not step.irreversible or step.attempts
                    or step.status not in {UpgradeStepStatus.PENDING, UpgradeStepStatus.AWAITING_APPROVAL}
                    or step.requires_backup and current.pre_upgrade_backup_id is None):
                self.denied('An unattempted irreversible step and required backup are mandatory')
            target = upgrades._approval_target(current, step, actor=worker)
            if target.fingerprint() != data.expected_target_fingerprint:
                self.denied('Exact Upgrade approval target changed')
            return step, target

        step, target = await asyncio.to_thread(binding, plan)
        identity = [worker.organization_id, worker.workspace_id, plan.id, step.id,
                    target.fingerprint(), assignment.id, assignment.fence, assignment.execution_id,
                    worker.identity_id]
        request_id = 'approval-' + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]
        store = upgrades.store.store
        selected = await asyncio.to_thread(self.guard_rows, assignment, None, validator)
        for repository in (upgrades.store, upgrades.releases.store, upgrades.approvals.store):
            if repository.store is not store:
                self.denied('Agent Upgrade approval requires one canonical transaction store')
            selected[repository.namespace] = (None,)
        if self.service().store.store is not store:
            self.denied('Assignment admission and Upgrade stores differ')

        def observe(current, approvals):
            existing_step, existing_target = binding(current)
            if existing_step.approval_request_id is not None:
                existing = approvals.requests.get(existing_step.approval_request_id)
                if (existing is None or existing.id != request_id
                        or existing.requester_identity_id != worker.identity_id
                        or existing.requester_principal_kind != worker.principal_kind
                        or existing.target != existing_target):
                    self.denied('Existing approval belongs to another origin; preserve it and use a new plan')
                raise _ExistingRequest(existing)
            if current != plan or request_id in approvals.requests:
                self.denied('Upgrade changed or an unattached historical request exists')

        def commit(request, expiry_schedule):
            if expiry_schedule is not None:
                self.denied('Agent bridge does not create a separate expiry workflow')
            result = []
            def apply(rows):
                self.principals(assignment, worker_actor, resolved, validator, payload)
                self._workspace(assignment, authorizer)
                state = upgrades.store._decode(rows[upgrades.store.namespace][None])
                approvals = upgrades.approvals.store._decode(rows[upgrades.approvals.store.namespace][None])
                current = upgrades._open_plan(plan.id, actor=worker)
                observe(current, approvals)
                approvals.requests[request.id] = request
                state.plans[plan.id] = current.model_copy(update={
                    'steps': tuple(s.model_copy(update={'approval_request_id': request.id,
                        'status': UpgradeStepStatus.AWAITING_APPROVAL}) if s.id == step.id else s
                        for s in current.steps), 'updated_at': float(upgrades.clock())})
                result.append(request)
                return {upgrades.store.namespace: {None: state.model_dump(mode='json')},
                        upgrades.approvals.store.namespace: {None: approvals.model_dump(mode='json')}}
            store.record_mutate_many(selected, apply)
            return result[0]

        event = ControlPlaneBrokerAuditEvent(organization_id=worker.organization_id,
            workspace_id=worker.workspace_id, project_id=assignment.project_id,
            execution_id=assignment.execution_id, assignment_id=assignment.id,
            worker_id=assignment.assigned_worker_id, fence=assignment.fence,
            actor_identity_id=authorizer.identity_id, execution_profile_id=assignment.execution_profile_id,
            operation_id=resolved.operation.id, capability=resolved.operation.capability,
            method='POST', path=resolved.operation.path_template, target_ref=plan.id + ':' + step.id,
            decision=ControlPlaneBrokerDecision.ALLOW, authority_decision_id=decision.id,
            correlation_id=request_id)
        await asyncio.to_thread(b.audit.append, event)
        try:
            record = await upgrades.approvals.create(ApprovalRequestCreate(target=target,
                project_id=plan.project_id,
                reason=(f'Agent {worker.identity_id} requests irreversible Upgrade step {step.id}: '
                    f'{step.description}. Execution {assignment.execution_id}; '
                    f'original request authorizer {authorizer.identity_id}.'),
                policy_source='canonical:safe-upgrade', authority_source='canonical:role-authority',
                requirement=ApprovalRequirement(quorum=1, distinct_humans=True, allow_self_approval=False),
                evidence_refs=((plan.preflight.evidence_id,) if plan.preflight and plan.preflight.evidence_id else ()),
                audit_refs=(event.id,)), requester=worker, request_id=request_id, _commit=commit)
        except _ExistingRequest as existing:
            record = existing.record
        return {'approval_request': record.model_dump(mode='json'),
                'agent_requester': worker.identity_id, 'original_authorizer': authorizer.identity_id,
                'execution_id': assignment.execution_id, 'assignment_id': assignment.id,
                'assignment_fence': assignment.fence, 'step_executed': False}
