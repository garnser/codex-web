"""Typed broker admission to the canonical, non-replayable Home host action."""
import asyncio

from pydantic import BaseModel, ConfigDict, Field

from codex_web.action_intents import ActionDecisionSnapshot, ActionIntentCreate, ActionIntentRetryPolicy
from codex_web.action_providers import ActionRequest
from codex_web.authority import AuthorityLevel
from codex_web.control_plane_broker import ControlPlaneBrokerOperation
from codex_web.services.home_host_action_provider import HOST_COMMAND_ACTION, HostCommand


HOST_COMMAND = ControlPlaneBrokerOperation(id=HOST_COMMAND_ACTION, method='POST',
    path_template='/api/operator/host/commands', capability=HOST_COMMAND_ACTION,
    authority_level=AuthorityLevel.EXECUTE)


class HostCommandParameters(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    argv: tuple[str, ...] = Field(min_length=1, max_length=64)


class HostCommandAdmission(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    parameters: HostCommandParameters
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9._:-]+$')


def admit_host_command(broker, snapshot, worker_actor, payload):
    from codex_web.services.control_plane_broker import ControlPlaneBrokerDeniedError
    data = HostCommandAdmission.model_validate(payload)
    context = broker.home_operator_host.authorize_operation(snapshot,
        operation_id=HOST_COMMAND_ACTION, worker_actor=worker_actor, mutating=True)
    assignment, actor, envelope = context.assignment, context.requester, context.envelope
    intents = broker.action_intents
    if intents is None or envelope is None:
        raise ControlPlaneBrokerDeniedError('Canonical Home host action is unavailable')
    parameters = HostCommand(assignment_id=assignment.id, fence=assignment.fence,
        argv=data.parameters.argv)
    candidates = [b for b in intents.execution.registry.store.load().bindings
        if b.enabled and (b.provider_type, b.provider_instance) == ('home-operator', 'trusted-host')
        and (b.organization_id, b.workspace_id, b.project_id, b.resource_ids) ==
            (actor.organization_id, actor.workspace_id, 'home', (envelope.host_resource_id,))
        and b.credential_ref is None]
    if len(candidates) != 1:
        raise ControlPlaneBrokerDeniedError('Exactly one explicit Home host provider binding is required')
    # Admission deduplication does not imply that the host process can be replayed.
    request = ActionRequest(action_id=HOST_COMMAND_ACTION, organization_id=actor.organization_id,
        workspace_id=actor.workspace_id, project_id='home', resource_ids=(envelope.host_resource_id,),
        requested_by=actor.identity_id, parameters=parameters.model_dump(mode='json'))
    intent = intents.create(ActionIntentCreate(binding_id=candidates[0].id, request=request,
        execution_id=assignment.execution_id, timeout_seconds=envelope.max_command_seconds + 30,
        retry_policy=ActionIntentRetryPolicy(max_attempts=1),
        policy_decision=ActionDecisionSnapshot(outcome='allow', source='policy:assignment-control-plane',
            reason='Current explicitly selected Home administrator and host declaration',
            capabilities=(HOST_COMMAND_ACTION,))), actor=actor, assignment_id=assignment.id,
        admission_idempotency_key=f'home-host:{assignment.id}:{assignment.fence}:{data.idempotency_key}')
    return intent, assignment, actor


async def execute_host_command(broker, snapshot, worker_actor, payload):
    intent, assignment, actor = await asyncio.to_thread(
        admit_host_command, broker, snapshot, worker_actor, payload)
    # Claim covers the configured command bound plus result recording. The normal
    # repository helper's 180s lease is insufficient for a 600s host operation.
    from codex_web.action_intents import ActionIntentClaimRequest
    worker_id = f'control-plane-broker:{assignment.assigned_worker_id}'
    claimed = await asyncio.to_thread(broker.action_intents.claim,
        ActionIntentClaimRequest(worker_id=worker_id,
            lease_seconds=min(900, int(intent.timeout_seconds) + 30)),
        actor=worker_actor, intent_id=intent.id)
    if claimed is None:
        current = await asyncio.to_thread(broker.action_intents.get, intent.id, actor)
        return {'item': current.model_dump(mode='json')}
    completed = await broker.action_intents.execute_claimed(intent.id, worker_id, actor=worker_actor)
    return {'item': completed.model_dump(mode='json')}
