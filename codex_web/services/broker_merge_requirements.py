"""Assignment-bound admission for the existing CodeHost read projection."""
import asyncio
import uuid

from codex_web.services.control_plane_broker import ControlPlaneBrokerDeniedError, ControlPlaneBrokerRequestError


async def read_merge_requirements(broker, assignment, worker_actor, resolved, validator, *, inspection_kind='worker'):
    operation = resolved.operation
    if validator is None or broker.execution_profiles is None:
        raise ControlPlaneBrokerDeniedError("Merge-requirement reads require live assignment and Definition validation")
    if set(resolved.query) - {'repository_resource_id'}:
        raise ControlPlaneBrokerRequestError("Merge-requirement reads accept only the canonical repository selector")

    def identity(item):
        return (item.id, item.execution_id, item.organization_id, item.workspace_id, item.project_id,
            item.created_by, item.assigned_worker_id, item.fence, item.execution_profile_id,
            item.execution_profile_definition, item.agent_profile, item.repository_scope, tuple(item.resource_ids))

    pinned_assignment = identity(assignment)

    def scope():
        current = validator()
        lease = current.lease
        now = broker._clock()
        if (identity(current) != pinned_assignment or current.status not in {'claimed', 'running'}
                or lease is None or lease.worker_id != current.assigned_worker_id or lease.fence != current.fence
                or lease.expires_at <= now or current.deadline_at is None or current.deadline_at <= now):
            raise ControlPlaneBrokerDeniedError("Merge-requirement assignment, fence or lease is no longer current")
        actor, requester, _, _ = broker._dispatch_scope(current, worker_actor, resolved)
        try:
            profile, reference = broker.execution_profiles.resolve(current.execution_profile_id,
                organization_id=current.organization_id, workspace_id=current.workspace_id, project_id=current.project_id)
        except (ValueError, LookupError) as exc:
            raise ControlPlaneBrokerDeniedError("Current merge-requirement declaration unavailable") from exc
        if reference != current.execution_profile_definition or operation.id not in profile.control_plane_operations:
            raise ControlPlaneBrokerDeniedError("Merge-requirement read is not declared in the current exact Definition")
        binding_id, repository_id = broker._code_host_binding(assignment=current, actor=actor,
            query=resolved.query, operation_id=operation.id)
        # Both principals need their own current read authority. Only the
        # inspecting worker's identity may use its provider credential.
        broker._authorize(current, operation, actor=requester, resource_ids=(repository_id,))
        registry = broker.action_intents.execution.registry
        action_binding = registry.binding(binding_id.removeprefix('control-plane-read:'), actor)
        provider = registry.provider(action_binding.provider_type, action_binding.provider_instance, actor=actor)
        return current, actor, requester, binding_id, repository_id, action_binding.model_dump(mode='json'), provider

    original = await asyncio.to_thread(scope)
    latest = [original]
    read_id = uuid.uuid4().hex

    async def checkpoint():
        current = await asyncio.to_thread(scope)
        if current[3:6] != original[3:6] or current[6] is not original[6]:
            raise ControlPlaneBrokerDeniedError("Merge-requirement provider binding changed")
        latest[0] = current

    item = await broker.code_hosts.merge_requirements(original[3], original[4], resolved.target_ref,
        actor=original[1], checkpoint=checkpoint, audit_context={
            'assignment_id': assignment.id, 'execution_id': assignment.execution_id,
            'project_id': assignment.project_id, 'fence': assignment.fence,
            'requester_identity_id': original[2].identity_id, 'inspection_kind': inspection_kind,
            'read_id': read_id, 'external_id': resolved.target_ref,
            'definition_record_id': assignment.execution_profile_definition.record_id})
    await checkpoint()
    current, actor, requester = latest[0][:3]
    return {'item': item.model_dump(mode='json'), 'scope': {
        'organization_id': current.organization_id, 'workspace_id': current.workspace_id,
        'project_id': current.project_id, 'repository_id': original[4],
        'assignment_id': current.id, 'execution_id': current.execution_id, 'fence': current.fence,
        'read_id': read_id,
        'lease_expires_at': current.lease.expires_at,
        'requester_identity_id': requester.identity_id,
        'inspection_kind': inspection_kind, 'inspecting_identity_id': actor.identity_id,
        'inspecting_worker_identity_id': actor.identity_id if inspection_kind == 'worker' else None,
        'definition': current.execution_profile_definition.model_dump(mode='json'),
    }}


async def inspect_merge_requirements(broker, *, assignment_id, project_id, repository_id, external_id, actor):
    """Operator content reads use the operator's own authority and secret rights."""
    if broker.execution_workers is None:
        raise ControlPlaneBrokerDeniedError('Current assignment inspection unavailable')
    worker_pin = []

    def current():
        item = broker.execution_workers.store.assignment(assignment_id)
        if item is None or (item.organization_id, item.workspace_id, item.project_id) != (
                actor.organization_id, actor.workspace_id, project_id):
            raise ControlPlaneBrokerDeniedError('Assignment inspection is outside current Project scope')
        worker = broker.execution_workers.store.worker(item.assigned_worker_id)
        if worker is None or item.lease is None or (worker.organization_id, worker.workspace_id) != (
                item.organization_id, item.workspace_id):
            raise ControlPlaneBrokerDeniedError('Assignment worker inspection unavailable')
        signature = (worker.id, worker.service_identity_id, worker.organization_id, worker.workspace_id)
        if worker_pin and worker_pin[0] != signature:
            raise ControlPlaneBrokerDeniedError('Assignment worker identity changed')
        if not worker_pin:
            worker_pin.append(signature)
        try:
            from codex_web.identity import PrincipalKind, TenantScope
            worker_identity = broker.identity.actor_for_identity(worker.service_identity_id,
                scope=TenantScope(organization_id=item.organization_id, workspace_id=item.workspace_id))
            # Custody metadata is inspected without authenticating as the worker
            # or synthesizing its token scopes. The shared guard checks the lease.
            if worker_identity.principal_kind != PrincipalKind.SERVICE or worker.lifecycle not in {'active', 'draining'}:
                raise ControlPlaneBrokerDeniedError('Assignment worker custody is no longer current')
        except (ValueError, RuntimeError) as exc:
            raise ControlPlaneBrokerDeniedError('Assignment worker custody is no longer current') from exc
        return item

    assignment = await asyncio.to_thread(current)
    from urllib.parse import quote, urlencode
    resolved = broker._resolve_operation('GET', '/api/repository-facts/pull-requests/'
        + quote(external_id, safe='') + '/merge-requirements?'
        + urlencode({'repository_resource_id': repository_id}))
    return await read_merge_requirements(broker, assignment, actor, resolved, current, inspection_kind='operator')
