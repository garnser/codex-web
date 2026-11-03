"""Explicit scheduling disposition for settled, canonically retired custody (#2001)."""
from __future__ import annotations

import json

from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.identity import IdentityService


def cancel_retired_run(service, run, actor):
    # The request's authenticated origin is retained; an identity lookup alone
    # must never turn a credential-free delegated caller into an MFA operator.
    def operator():
        current = service.identity.revalidate_authenticated_origin(actor)
        IdentityService.require_admin(current)
        if current.principal_kind == PrincipalKind.SERVICE:
            if 'automation:execute' not in current.service_scopes:
                raise ValueError('procedure_retirement_operator_scope_required')
        else:
            IdentityService.require_assurance(current, AuthenticationAssurance.MFA)
        return current

    operator()
    custody = run.procedure_custody
    if run.target_kind != 'canonical_procedure' or run.status != 'running' or custody is None:
        raise ValueError('procedure_retirement_running_custody_required')

    def validate():
        operator()
        automation, owner, _ = service._definition(run, effects=False)
        assignment = service.engine.get(custody.assignment_id, owner)
        if ((assignment.organization_id, assignment.workspace_id, assignment.project_id) !=
                (run.organization_id, run.workspace_id, run.project_id)
                or (assignment.id, assignment.assigned_worker_id, assignment.execution_id,
                    assignment.execution_workspace_id, assignment.fence) !=
                   (custody.assignment_id, custody.worker_id, custody.execution_id,
                    custody.execution_workspace_id, custody.fence + 1)
                or assignment.subject is None or assignment.subject.kind != 'thread_bootstrap'
                or assignment.status != 'cancelled' or assignment.lease is not None
                or assignment.failure_code != 'thread_bootstrap_cancelled'):
            raise ValueError('procedure_retirement_cancelled_original_required')
        checkpoint = assignment.procedure_checkpoint
        if (checkpoint is None or checkpoint.id != custody.checkpoint_id
                or checkpoint.definition != custody.definition
                or checkpoint.definition != automation.target.procedure_ref
                or checkpoint.input_snapshot_id != custody.input_snapshot_id
                or checkpoint.status not in {'ready', 'waiting', 'awaiting_approval', 'recovery_required'}):
            raise ValueError('procedure_retirement_checkpoint_unsettled_or_changed')
        service.engine._content(checkpoint.input_snapshot_id, owner, assignment, checkpoint.input_snapshot_checksum)
        service._original_values(run, owner)
        procedure = service.engine._procedure(assignment, owner)
        if set(checkpoint.steps) != {step.id for step in procedure.steps}:
            raise ValueError('procedure_retirement_step_set_changed')
        work = service.get_work(run.work_item_ref)
        if ((work.organization_id, work.workspace_id, work.project_id) !=
                (run.organization_id, run.workspace_id, run.project_id)
                or not work.resource_ids or set(work.resource_ids) - set(assignment.resource_ids)):
            raise ValueError('procedure_retirement_work_scope_changed')
        # The complete canonical Work index owns native UNKNOWN classification;
        # missing or truncated inventories cannot be interpreted as no effects.
        service.routing.require_resolved_originals(work)
        for step in procedure.steps:
            state = checkpoint.steps[step.id]
            if state.invocation_ids or state.status in {'running', 'reconciliation_required'}:
                raise ValueError('procedure_retirement_effect_unresolved')
            if state.action_intent_id:
                intent = service.engine.intents.get(state.action_intent_id, service.engine.control_actor)
                receipt = service.engine.intents.store.recovery_last_receipt(intent)
                if (state.status != 'succeeded' or intent.status != 'succeeded' or intent.lease is not None
                        or (intent.organization_id, intent.workspace_id, intent.project_id,
                            intent.execution_id, intent.requested_by) !=
                           (run.organization_id, run.workspace_id, run.project_id,
                            assignment.execution_id, checkpoint.actor_identity_id)
                        or intent.idempotency_key != service.engine._step_origin(assignment, step.id, intent=True)
                        or receipt is None or receipt.outcome != 'completed'
                        or receipt.result is None or receipt.result.status != 'succeeded'):
                    raise ValueError('procedure_retirement_provider_outcome_unresolved')
            if state.status == 'succeeded':
                if (step.kind in {'mutation', 'recovery'} and (not step.verification_step_id
                        or checkpoint.steps[step.verification_step_id].status != 'succeeded')):
                    raise ValueError('procedure_retirement_verification_incomplete')
                service.engine._proof(assignment, step, state, owner)
            elif state.status in {'pending', 'skipped'}:
                if (state.attempts or state.started_at is not None or state.action_intent_id
                        or state.evidence or state.outputs or state.output_snapshot_id
                        or state.output_snapshot_checksum or state.approval_id or state.rollback_origin_receipt_id):
                    raise ValueError('procedure_retirement_step_reservation_unresolved')
            elif state.status in {'waiting', 'failed'} and step.control_plane is not None:
                registry = service.engine.control_plane
                registration = registry.validate_step(procedure, step) if registry is not None else None
                # A mutation's waiting diagnostic alone is not a no-effect
                # receipt; its original canonical owner must settle it below.
                if registration is None:
                    raise ValueError('procedure_retirement_canonical_owner_unavailable')
                if set(state.evidence) != {'canonical_disposition'}:
                    raise ValueError('procedure_retirement_diagnostic_required')
                proof = service.engine.evidence.get_evidence(state.evidence['canonical_disposition'], actor=owner)
                if (proof.lifecycle != 'valid' or proof.result != 'info'
                        or proof.provider != 'canonical-procedure-owner'
                        or proof.execution_workspace_id != assignment.execution_workspace_id
                        or proof.producer_identity_id != owner.identity_id
                        or proof.source != service.engine._step_origin(assignment, step.id)
                        or (proof.project_id, proof.execution_id, proof.work_item_ref) !=
                           (assignment.project_id, assignment.execution_id, assignment.work_item_ref)
                        or set(proof.resource_ids) - set(assignment.resource_ids)
                        or proof.metadata.get('canonical_operation') != registration.contract.operation_id
                        or state.started_at is None or proof.observed_at < state.started_at
                        or proof.retention_expires_at is not None and proof.retention_expires_at <= service.runs.clock()):
                    raise ValueError('procedure_retirement_diagnostic_changed')
                diagnostic = json.loads(proof.metadata.get('canonical_result', '{}'))
                if not isinstance(diagnostic, dict):
                    raise ValueError('procedure_retirement_diagnostic_changed')
                disposition = 'waiting' if state.status == 'waiting' else 'refused'
                raw_code = diagnostic.get('code')
                if raw_code is not None and not isinstance(raw_code, str):
                    raise ValueError('procedure_retirement_diagnostic_changed')
                code = raw_code or 'procedure_canonical_' + disposition
                expected = (code + ':canonical_work_owner_resolution_required'
                    if step.failure.escalation_owner_policy else code)
                if (diagnostic.get('operation') != registration.contract.operation_id
                        or diagnostic.get('disposition') != ('waiting' if state.status == 'waiting' else 'refused')
                        or not isinstance(code, str) or expected != state.failure_code):
                    raise ValueError('procedure_retirement_diagnostic_changed')
                if registration.contract.effect != 'read':
                    from codex_web.services.procedure_custody_recovery import CustodyInput, CustodyOperation
                    adapter = registration.adapter
                    if (state.status != 'waiting' or type(adapter) is not CustodyOperation
                            or registration.contract.operation_id != 'work.handoff.dispatch-existing.v1'):
                        raise ValueError('procedure_retirement_canonical_effect_unresolved')
                    # Reload only canonical retained Work/START/ACK originals.
                    # A protected operator journal never substitutes for them.
                    original = CustodyInput.model_validate(service._original_values(run, owner), strict=True)
                    adapter.settled_retired_dispatch(assignment, original)
            else:
                raise ValueError('procedure_retirement_step_outcome_unresolved')
        return assignment

    assignment = validate()
    # Existing canonical SQLite transaction: compare exact original Run and
    # assignment after acquiring the writer, then revalidate read-only owners.
    # No assignment, checkpoint, receipt or native process is changed here.
    def under_writer():
        if validate() != assignment:
            raise ValueError('procedure_retirement_original_changed')
    return service.runs.store.cancel_retired_procedure(run, assignment,
        workers=service.engine.workers.store, validate=under_writer, now=service.runs.clock(),
        operator_identity_id=actor.identity_id)
