"""Opt-in composition of a restricted native session using existing owners.

There is no default activation, credential extraction or operational broker.
The deployment supplies exact canonical Profile/Definition pins and an identity
validator; those are checked by the same credential owner at every lease edge.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from pathlib import Path
from types import SimpleNamespace

from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.resources import (RepositoryExecutionScope, RepositoryExecutionTarget,
    RepositoryTargetSource, RepositoryWriteMode)
from codex_web.local_execution_backend import BubblewrapExecutionBackend
from codex_web.services.agent_model_egress import AgentRuntimeModelEgressEndpoint
from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSessionManager
from codex_web.services.codex_worker_session import AssignmentBoundCodexSession
from codex_web.services.codex_reasoning_credentials import TrustedLocalReasoningCredentialProvider
from codex_web.services.codex_reasoning_policy import (ReasoningOnlyCodexRuntime,
    ReasoningOnlyCodexAdapter, NativeReasoningPolicyError, QUALIFIED_CODEX_VERSION)
from codex_web.services.native_model_provider import (NativeCodexModelProviderAdapter,
    current_native_model_invocation)
from codex_web.services.native_reasoning_bridge import CanonicalNativeReasoningBridge


def _empty_repository_scope(assignment):
    """Accept canonical repository-free provenance without granting a repository."""
    scope, target = assignment.repository_scope, assignment.repository_target
    # Retain the older absent-envelope representation, but never let an absent
    # scope hide a target or an orphan scope bypass target consistency.
    if scope is None or target is None:
        return scope is None and target is None
    tenant = (assignment.organization_id, assignment.workspace_id, assignment.project_id)
    return (
        isinstance(scope, RepositoryExecutionScope)
        and isinstance(target, RepositoryExecutionTarget)
        and (scope.organization_id, scope.workspace_id, scope.project_id) == tenant
        and (target.organization_id, target.workspace_id, target.project_id) == tenant
        and scope.source == target.source == RepositoryTargetSource.ORCHESTRATION_ONLY
        and scope.source_ref == target.source_ref == 'explicit'
        and scope.write_mode == RepositoryWriteMode.SINGLE
        and not scope.writable_repository_ids and not scope.read_only_repository_ids
        and target.mutable_repository_id is None and not target.read_only_repository_ids
        and not scope.selection_evidence and not target.selection_evidence
        and scope == RepositoryExecutionScope.from_target(target)
    )


class ReasoningCodexSession(AssignmentBoundCodexSession):
    def _spawn_delegated_process(self, assignment, workspace_path, broker, control_broker):
        if control_broker is not None or self.control_plane_broker_factory is not None:
            raise NativeReasoningPolicyError('Reasoning session cannot expose operational broker')
        backend = self.local_worker.backend
        if type(backend) is not BubblewrapExecutionBackend:
            raise NativeReasoningPolicyError('Reasoning requires canonical isolated backend')
        result = super()._spawn_delegated_process(assignment, workspace_path, broker, None)
        self._reasoning_launch_backend = backend
        return result


class ReasoningSessionManager(AssignmentBoundAgentProcessSessionManager):
    async def start(self, assignment_id):
        context = current_native_model_invocation()
        if context.process_owner is None:
            raise NativeReasoningPolicyError('Native process join owner unavailable')
        context.process_owner.attach(assignment_id, lambda: self.stop(assignment_id))
        return await super().start(assignment_id)


@dataclass(frozen=True, slots=True)
class NativeReasoningComponents:
    manager: AssignmentBoundAgentProcessSessionManager
    runtime_adapter: ReasoningOnlyCodexAdapter
    model_adapter: NativeCodexModelProviderAdapter
    bridge: CanonicalNativeReasoningBridge
    invocation_authorizer: object
    preparation_context: object


def build_native_reasoning_components(*, binding, local_worker, host, codex_home: Path,
        executable: str, catalog_path: Path, catalog_sha256: str,
        validate_profile, invocation_authorizer, agent_profiles, execution_profiles, threads, turns, sessions, usage,
        preflight, execution, hub, executives, authentication_preparation=None):
    """Build inert components; registration is explicit and never enables a Profile.

    validate_profile resolves the current configured canonical scratch Definition
    and exact AgentProfile snapshot; invocation_authorizer revalidates the actual
    authenticated Executive caller, including original broker custody if used.
    """
    if not callable(validate_profile) or not callable(invocation_authorizer):
        raise ValueError('Current Profile and caller authority owners required')

    def validate_scope(assignment):
        context = current_native_model_invocation()
        context.validate_current()
        profile, runtime = assignment.agent_profile, assignment.runtime_binding
        if ((assignment.organization_id, assignment.workspace_id, assignment.project_id,
             assignment.assigned_worker_id, assignment.execution_profile_id) !=
            (context.organization_id, context.workspace_id, context.project_id,
             binding.worker_id, binding.execution_profile_id)
                or assignment.resource_ids or assignment.secret_refs
                or not _empty_repository_scope(assignment) or assignment.resource_context is not None
                or assignment.sandbox != 'read-only' or assignment.network.enabled
                or profile is None or profile.profile_id != binding.agent_profile_id
                or profile.profile_revision != binding.agent_profile_revision
                or runtime is None or runtime.provider_id != 'openai'
                or runtime.runtime_id != 'codex-reasoning'
                or runtime.authentication_mode != 'trusted_local_session'
                or context.concrete_model != binding.concrete_model):
            raise NativeReasoningPolicyError('Native reasoning assignment scope changed')
        if context.requester is None:
            raise NativeReasoningPolicyError('Actual consultation requester unavailable')
        current_profile, _ = agent_profiles.resolve_for_execution(binding.agent_profile_id,
            actor=context.requester, project_id=context.project_id,
            revision=binding.agent_profile_revision)
        if current_profile.record_id != profile.profile_record_id:
            raise NativeReasoningPolicyError('Native reasoning AgentProfile pin changed')
        contract, reference = execution_profiles.resolve(binding.execution_profile_id,
            organization_id=context.organization_id, workspace_id=context.workspace_id,
            project_id=context.project_id)
        workspace = local_worker.workspace_service.get(assignment.execution_workspace_id,
                                                       local_worker.control_actor)
        lease = local_worker.workspace_service.store.lease(workspace.lease_id)
        if (reference != assignment.execution_profile_definition
                or contract.workspace_mode != 'scratch' or contract.repository_access != 'none'
                or contract.control_plane_operations or contract.administrative_repository_ids
                or contract.project_coordination or contract.home_operator
                or contract.network_enabled or contract.host_mutation
                or workspace.kind != 'scratch' or workspace.status != 'active'
                or (workspace.organization_id, workspace.workspace_id, workspace.project_id,
                    workspace.execution_id, workspace.subject) !=
                   (assignment.organization_id, assignment.workspace_id, assignment.project_id,
                    assignment.execution_id, assignment.subject)
                or workspace.resource_ids or workspace.read_only_resource_ids
                or workspace.writable_repository_ids or workspace.resource_context is not None
                or lease is None or lease.released_at is not None or lease.expires_at <= time.time()
                or lease.resource_ids or lease.resource_modes
                or lease.execution_workspace_id != workspace.id
                or lease.execution_id != assignment.execution_id):
            raise NativeReasoningPolicyError('Native reasoning empty physical scope unavailable')
        if context.process_owner is None or context.process_owner.assignment_id != assignment.id:
            raise NativeReasoningPolicyError('Original native process owner unavailable')
        context.process_owner.bind_workspace(workspace.path)
        validate_profile(assignment)

    credentials = TrustedLocalReasoningCredentialProvider(codex_home=codex_home,
        executable=executable, catalog_path=catalog_path, catalog_sha256=catalog_sha256,
        validate_context=validate_scope, concrete_model=binding.concrete_model)
    runtime_binding = ExecutionRuntimeBinding(provider_id='openai', runtime_id='codex-reasoning',
        capability_revision=1, sandbox_profiles=('read-only',),
        authentication_mode='trusted_local_session')
    manager = ReasoningSessionManager(local_worker, host,
        runtime_factory=ReasoningOnlyCodexRuntime, credential_provider=credentials,
        runtime_binding=runtime_binding, session_factory=ReasoningCodexSession,
        # Same existing assignment-bound CONNECT proxy and credential owner;
        # this permits only the native inference protocol's first-party hosts.
        egress_endpoints_resolver=lambda: (AgentRuntimeModelEgressEndpoint('chatgpt.com', 443),),
        control_plane_broker_factory=None)
    adapter = ReasoningOnlyCodexAdapter(SimpleNamespace(host=host))

    def attest(serving, expected, context, model):
        if not isinstance(serving, ReasoningCodexSession):
            raise NativeReasoningPolicyError('Actual serving reasoning session required')
        assignment = serving.validate_current()
        validate_scope(assignment)
        runtime = serving.runtime
        backend = local_worker.backend
        status = serving.status()
        if ((assignment.id, assignment.execution_id, assignment.fence,
                assignment.assigned_worker_id, assignment.execution_workspace_id,
                assignment.execution_profile_definition, assignment.agent_profile,
                assignment.resource_ids, assignment.repository_scope, assignment.runtime_binding) !=
               (expected.id, expected.execution_id, expected.fence, expected.assigned_worker_id,
                expected.execution_workspace_id, expected.execution_profile_definition,
                expected.agent_profile, expected.resource_ids, expected.repository_scope,
                expected.runtime_binding) or serving.credential_provider is not credentials
                or type(backend) is not BubblewrapExecutionBackend
                or getattr(serving, '_reasoning_launch_backend', None) is not backend
                or serving.control_plane_broker is not None
                or not status.running or not status.ready
                or not isinstance(runtime, ReasoningOnlyCodexRuntime)
                or runtime.native_runtime_version != QUALIFIED_CODEX_VERSION
                or runtime.restart_on_timeout or runtime.reasoning_refusal
                or model.concrete_model != binding.concrete_model):
            raise NativeReasoningPolicyError('Actual native reasoning preparation unavailable')
        isolation = backend.probe()
        if not isolation.ready or not isolation.supports_network_disabled or not isolation.supports_resource_limits:
            raise NativeReasoningPolicyError('Actual native isolation unavailable')
        backend.validate_assignment(assignment)

    def preparation_context(*, assignment, actor, project, sandbox, request_checksum):
        from codex_web.services.procedure_discovery import OperationContext, checksum
        context = current_native_model_invocation()
        serving = manager.get(assignment.id)
        if (serving is None or context.requester is None
                or (actor.identity_id,actor.organization_id,actor.workspace_id,actor.principal_kind) !=
                   (context.actor_id,context.organization_id,context.workspace_id,context.requester.principal_kind)
                or project.id != context.project_id
                or sandbox != 'read-only' or not request_checksum
                or request_checksum != context.input_message_sha256):
            raise NativeReasoningPolicyError('Native reasoning original input proof unavailable')
        attest(serving, assignment, context, SimpleNamespace(concrete_model=binding.concrete_model))
        proof = checksum({'assignment':assignment.id,'fence':assignment.fence,
            'execution':assignment.execution_id,'workspace':assignment.execution_workspace_id,
            'activation':context.activation_id,'role':context.role_id,
            'invocation':context.execution_id,'input':request_checksum,
            'profile':assignment.agent_profile.model_dump(mode='json'),
            'definition':assignment.execution_profile_definition.model_dump(mode='json'),
            'catalog':catalog_sha256})
        return OperationContext(actor=actor,project_id=project.id,resource_ids=(),
            operation='model.reasoning',provider_capabilities=(),worker_capabilities=(),
            operation_capabilities=(),policy_source='canonical:native-reasoning:' + proof,
            capability_sources=('canonical:actual-native-reasoning-tools-empty:' + proof,),
            policy_allows=True,enforcement='bounded_read_only',request_checksum=request_checksum,
            required_skill_refs=tuple(dict.fromkeys((*assignment.skill_refs,
                                                     *assignment.agent_profile.skill_refs))))

    bridge = CanonicalNativeReasoningBridge(binding=binding, threads=threads, turns=turns,
        sessions=sessions, usage=usage, preflight=preflight, execution=execution, hub=hub,
        attest_session=attest, authentication_preparation=authentication_preparation)
    # Setting an optional authorizer is not activation or an execution grant.
    # Root composition must register all components atomically before exposing
    # the matching canonical provider/Profile/Definition.
    return NativeReasoningComponents(manager, adapter, NativeCodexModelProviderAdapter(bridge), bridge,
                                     invocation_authorizer, preparation_context)


def register_native_reasoning_components(components, *, gateway, registry, telemetry,
        session_managers, thread_service, turn_execution, executives):
    """Only called by reviewed deployment opt-in; never a public configuration API."""
    key = ('openai', 'codex-reasoning')
    # Consumers defensively copy the composition map at construction time.
    # Opt-in registration must reach those active owners as well as the map
    # retained by application composition, using the same canonical manager.
    manager_maps = (session_managers, thread_service.session_managers,
                    turn_execution.session_managers)
    if any(key in managers for managers in manager_maps):
        raise ValueError('Native reasoning runtime already configured')
    # Existing registries remain the sole provider/runtime/usage authority.
    from codex_web.agent_runtime import AgentRuntimeAccountingMode
    registry.register(components.runtime_adapter, capability_revision=1,
        sandbox_profiles=('read-only',), network_profiles=('brokered-model-egress',),
        accounting_mode=AgentRuntimeAccountingMode.ALLOCATION)
    gateway.register_adapter(components.model_adapter)
    executives.native_invocation_authorizer = components.invocation_authorizer
    if turn_execution.procedure_admission is None:
        raise NativeReasoningPolicyError('Canonical native procedure admission unavailable')
    turn_execution.procedure_admission.reasoning_preparation = components.preparation_context
    previous_notifier = telemetry.observation_notifier
    def observed(record):
        try:
            components.bridge.observe_usage(record)
        finally:
            if previous_notifier is not None:
                previous_notifier(record)
    telemetry.observation_notifier = observed
    telemetry.subscribe(components.runtime_adapter)
    for managers in manager_maps:
        managers[key] = components.manager
    for owner in (thread_service, turn_execution):
        previous = owner.runtime_adapter_factory
        def factory(binding, session, fallback=previous):
            if (binding.provider_id, binding.runtime_id) == key:
                return ReasoningOnlyCodexAdapter(session)
            if fallback is None:
                raise NativeReasoningPolicyError('Existing runtime adapter unavailable')
            return fallback(binding, session)
        owner.runtime_adapter_factory = factory
