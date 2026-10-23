"""Explicit, reference-only ConfigurationRegistry opt-in for native reasoning.

Configuration selects existing canonical objects; it creates neither models,
Profiles, credentials nor caller authority. Restart is required to register the
runtime, and every invocation still checks current configuration revocation.
"""
from pathlib import Path
from pydantic import BaseModel,ConfigDict,Field

from codex_web.configuration import ConfigurationScope,ConfigurationSpec,ConfigurationValueKind
from codex_web.services.codex_reasoning_policy import NativeReasoningPolicyError
from codex_web.services.native_reasoning_bridge import NativeReasoningBinding

PREFIX='model.native_reasoning.'
FIELDS={
    'enabled':(ConfigurationValueKind.BOOLEAN,False),
    'provider_id':(ConfigurationValueKind.STRING,None),
    'model_id':(ConfigurationValueKind.STRING,None),
    'concrete_model':(ConfigurationValueKind.STRING,None),
    'agent_profile_id':(ConfigurationValueKind.STRING,None),
    'agent_profile_revision':(ConfigurationValueKind.INTEGER,None),
    'execution_profile_id':(ConfigurationValueKind.STRING,None),
    'worker_id':(ConfigurationValueKind.STRING,None),
    'catalog_path':(ConfigurationValueKind.STRING,None),
    'catalog_sha256':(ConfigurationValueKind.STRING,None),
}


class NativeReasoningSettings(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    enabled: bool
    provider_id:str=Field(min_length=1)
    model_id:str=Field(min_length=1)
    concrete_model:str=Field(min_length=1)
    agent_profile_id:str=Field(min_length=1)
    agent_profile_revision:int=Field(ge=1)
    execution_profile_id:str=Field(min_length=1)
    worker_id:str=Field(min_length=1)
    catalog_path:str=Field(min_length=1)
    catalog_sha256:str=Field(pattern=r'^[0-9a-f]{64}$')

    def binding(self):
        return NativeReasoningBinding(**self.model_dump(exclude={'enabled','catalog_path','catalog_sha256'}))


def install_native_reasoning_configuration(service):
    for name,(kind,default) in FIELDS.items():
        service.register_spec(ConfigurationSpec(key=PREFIX+name,value_kind=kind,default=default,
            allowed_scopes=[ConfigurationScope.DEPLOYMENT,ConfigurationScope.GLOBAL],
            category='Model & Provider',startup_only=True,hot_reloadable=False,grants_authority=False,
            description='Explicit restricted native reasoning '+name.replace('_',' ')+'. No credential values.'))


def resolve_native_reasoning_settings(service):
    enabled=service.resolve(PREFIX+'enabled')
    if enabled.value is False:
        return None
    if enabled.value is not True or enabled.source!='published':
        raise NativeReasoningPolicyError('Explicit published native reasoning opt-in required')
    rows={name:service.resolve(PREFIX+name) for name in FIELDS}
    if any(row.source!='published' for row in rows.values()):
        raise NativeReasoningPolicyError('Complete published native reasoning configuration required')
    settings=NativeReasoningSettings(**{name:row.value for name,row in rows.items()})
    if not Path(settings.catalog_path).is_absolute():
        raise NativeReasoningPolicyError('Absolute reviewed public catalog path required')
    pins=tuple((name,row.record_id,row.revision) for name,row in rows.items())
    return settings,pins


def compose_native_reasoning(*,configuration,identity,agent_profiles,execution_profiles,
        local_worker,host,codex_home,executable,threads,turns,sessions,usage,preflight,
        execution,hub,executives,gateway,registry,telemetry,session_managers):
    """Application startup hook. Disabled default performs no native launch."""
    install_native_reasoning_configuration(configuration)
    resolved=resolve_native_reasoning_settings(configuration)
    if resolved is None:
        return None
    settings,pins=resolved
    if settings.worker_id!=local_worker.worker.id or codex_home is None or not executable:
        raise NativeReasoningPolicyError('Configured native credential/worker owner unavailable')
    def current_settings():
        if resolve_native_reasoning_settings(configuration)!=(settings,pins):
            raise NativeReasoningPolicyError('Native reasoning configuration changed; restart/review required')
    def authorize(activation,actor,*,origin_guard=None):
        def current():
            current_settings()
            identity.revalidate_authenticated_origin(actor,origin_guard=origin_guard)
            if not activation.project_id or (activation.organization_id,activation.workspace_id)!=\
                    (actor.organization_id,actor.workspace_id):
                raise NativeReasoningPolicyError('Native consultation tenant/project changed')
        return current
    from codex_web.services.native_reasoning_runtime import (build_native_reasoning_components,
        register_native_reasoning_components)
    components=build_native_reasoning_components(binding=settings.binding(),local_worker=local_worker,
        host=host,codex_home=Path(codex_home),executable=executable,
        catalog_path=Path(settings.catalog_path),catalog_sha256=settings.catalog_sha256,
        validate_profile=lambda assignment:current_settings(),invocation_authorizer=authorize,
        agent_profiles=agent_profiles,execution_profiles=execution_profiles,threads=threads,turns=turns,
        sessions=sessions,usage=usage,preflight=preflight,execution=execution,hub=hub,executives=executives)
    register_native_reasoning_components(components,gateway=gateway,registry=registry,
        telemetry=telemetry,session_managers=session_managers,thread_service=threads,
        turn_execution=execution,executives=executives)
    return components
