"""Current canonical identity, binding and credential custody for SCM reads."""
import asyncio
import time

from codex_web.code_hosts import CodeHostCapability, CodeHostError, CodeHostUnsupportedCapabilityError
from codex_web.code_host_merge_requirements import CodeHostMergeRequirementsFact
from codex_web.identity import TenantScope
from codex_web.secrets import SecretStatus
from codex_web.services.secrets import SecretUseDeniedError


async def read_merge_requirements(service, binding_id, resource_id, external_id, *, actor, checkpoint=None,
                                 audit_context=None, observation_policy=None):
    if observation_policy is not None:
        from codex_web.mr_observation_policy import ObservationPolicy
        if not isinstance(observation_policy, ObservationPolicy):
            raise CodeHostError('Canonical observation policy required')
    context = dict(audit_context or {})
    if (set(context) - {'assignment_id', 'execution_id', 'project_id', 'fence', 'requester_identity_id',
                        'inspection_kind', 'read_id', 'external_id', 'definition_record_id'}
            or any(type(value) not in {str, int} or len(str(value)) > 1024 for value in context.values())):
        raise CodeHostError('Merge-requirement audit accepts only bounded canonical scope metadata')
    def scope():
        binding, provider = service.registry.resolve(binding_id, actor=actor)
        service._require_capability(binding, provider, CodeHostCapability.MERGE_REQUIREMENTS_READ)
        if not callable(getattr(provider, 'merge_requirements', None)):
            raise CodeHostUnsupportedCapabilityError('Installed provider does not implement merge requirements')
        if service.identity is None or service.secrets is None or not binding.credential_ref:
            raise CodeHostError("Merge-requirement reads need current identity and canonical credentials")
        current = service.identity.actor_for_identity(actor.identity_id, scope=TenantScope(
            organization_id=actor.organization_id, workspace_id=actor.workspace_id))
        if current.principal_kind != actor.principal_kind or current.tenant != actor.tenant:
            raise CodeHostError("Code-host read principal changed")
        current = current.model_copy(update={key: getattr(actor, key) for key in
            ('assurance', 'service_token_id', 'service_scopes', 'authenticated_at')})
        resource = service._resource(resource_id, actor=current, binding=binding)
        reference = service.secrets.metadata(binding.credential_ref, actor=current, require_use=True)
        if reference.status() != SecretStatus.ACTIVE:
            raise SecretUseDeniedError("Code-host read credential is not active")
        return binding, provider, resource, reference, current

    if checkpoint is not None:
        await checkpoint()
    original = await asyncio.to_thread(scope)
    binding, provider, resource, reference, current_actor = original
    pinned = tuple(item.model_dump(mode='json') for item in (binding, resource, reference))

    async def check():
        if checkpoint is not None:
            await checkpoint()
        current = await asyncio.to_thread(scope)
        if (current[1] is not provider or tuple(item.model_dump(mode='json')
                for item in (current[0], current[2], current[3])) != pinned):
            raise CodeHostError("Code-host read binding, Resource or credential changed")

    async def consume(credential):
        await check()
        options = ({'observation_seconds': observation_policy.seconds} if observation_policy is not None else {})
        if options and provider.provider_type not in {'gitlab', 'github'}:
            raise CodeHostUnsupportedCapabilityError('Versioned observation policy unsupported by provider')
        result = await provider.merge_requirements(binding, resource, external_id,
                                                    credential=credential, checkpoint=check, **options)
        await check()
        return result

    result = await service._with_credential(binding, actor=current_actor, resource=resource,
        operation='code-host.pull-request.merge-requirements.read', callback=consume, audit_context=context)
    await check()
    if not isinstance(result, CodeHostMergeRequirementsFact) or (
            result.resource_id, result.binding_id, result.provider_type, result.provider_instance, result.external_id
    ) != (resource_id, binding_id, binding.provider_type, binding.provider_instance, str(external_id)):
        raise CodeHostError("Code-host merge-requirement result is outside its exact scope")
    if time.time() >= result.expires_at:
        result = result.model_copy(update={'complete': False,
            'blockers': tuple(dict.fromkeys((*result.blockers, 'observation_stale')))})
    return result
