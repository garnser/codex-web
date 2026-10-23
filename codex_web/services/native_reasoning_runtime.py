"""Opt-in composition of a restricted native session using existing owners.

There is no default activation, credential extraction or operational broker.
The deployment supplies exact canonical Profile/Definition pins and an identity
validator; those are checked by the same credential owner at every lease edge.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from codex_web.execution_workers import ExecutionRuntimeBinding
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


def build_native_reasoning_components(*, binding, local_worker, host, codex_home: Path,
        executable: str, catalog_path: Path, catalog_sha256: str,
        validate_profile, invocation_authorizer, threads, turns, sessions, usage,
        preflight, execution, hub, executives):
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
                or assignment.resource_ids or assignment.secret_refs or assignment.skill_refs
                or assignment.repository_scope is not None or assignment.resource_context is not None
                or assignment.sandbox != 'read-only' or assignment.network.enabled
                or profile is None or profile.profile_id != binding.agent_profile_id
                or profile.profile_revision != binding.agent_profile_revision
                or runtime is None or runtime.provider_id != 'openai'
                or runtime.runtime_id != 'codex-reasoning'
                or runtime.authentication_mode != 'trusted_local_session'
                or context.concrete_model != binding.concrete_model):
            raise NativeReasoningPolicyError('Native reasoning assignment scope changed')
        validate_profile(assignment)

    credentials = TrustedLocalReasoningCredentialProvider(codex_home=codex_home,
        executable=executable, catalog_path=catalog_path, catalog_sha256=catalog_sha256,
        validate_context=validate_scope)
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
        if (assignment != expected or serving.credential_provider is not credentials
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

    bridge = CanonicalNativeReasoningBridge(binding=binding, threads=threads, turns=turns,
        sessions=sessions, usage=usage, preflight=preflight, execution=execution, hub=hub,
        attest_session=attest)
    # Setting an optional authorizer is not activation or an execution grant.
    # Root composition must register all components atomically before exposing
    # the matching canonical provider/Profile/Definition.
    return NativeReasoningComponents(manager, adapter, NativeCodexModelProviderAdapter(bridge), bridge,
                                     invocation_authorizer)


def register_native_reasoning_components(components, *, gateway, registry, telemetry,
        session_managers, thread_service, turn_execution, executives):
    """Only called by reviewed deployment opt-in; never a public configuration API."""
    key = ('openai', 'codex-reasoning')
    if key in session_managers:
        raise ValueError('Native reasoning runtime already configured')
    # Existing registries remain the sole provider/runtime/usage authority.
    from codex_web.agent_runtime import AgentRuntimeAccountingMode
    registry.register(components.runtime_adapter, capability_revision=1,
        sandbox_profiles=('read-only',), network_profiles=('brokered-model-egress',),
        accounting_mode=AgentRuntimeAccountingMode.ALLOCATION)
    gateway.register_adapter(components.model_adapter)
    executives.native_invocation_authorizer = components.invocation_authorizer
    previous_notifier = telemetry.observation_notifier
    def observed(record):
        try:
            components.bridge.observe_usage(record)
        finally:
            if previous_notifier is not None:
                previous_notifier(record)
    telemetry.observation_notifier = observed
    telemetry.subscribe(components.runtime_adapter)
    session_managers[key] = components.manager
    for owner in (thread_service, turn_execution):
        previous = owner.runtime_adapter_factory
        def factory(binding, session, fallback=previous):
            if (binding.provider_id, binding.runtime_id) == key:
                return ReasoningOnlyCodexAdapter(session)
            if fallback is None:
                raise NativeReasoningPolicyError('Existing runtime adapter unavailable')
            return fallback(binding, session)
        owner.runtime_adapter_factory = factory
