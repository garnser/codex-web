"""Opt-in Home host commands: canonical intents, existing fenced local worker."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
import time

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.action_intents import ActionDecisionOutcome
from codex_web.action_providers import (ACTION_PROVIDER_CONTRACT, ActionCapability,
    ActionDefinition, ActionEvidence, ActionResult, ActionRiskClass, UnsupportedActionCapabilityError)
from codex_web.artifact_evidence import ArtifactCreate
from codex_web.crypto import EncryptionContext
from codex_web.identity import PrincipalKind
from codex_web.resources import ResourceType
from codex_web.services.action_providers import ActionRequirementError
from codex_web.services.identity import IdentityService

HOST_COMMAND_ACTION = 'operator.host.command'
_executor = ContextVar('canonical_home_host_executor', default=None)


class HostActionOutcomeUnknown(RuntimeError):
    """Possible host effects: retain the original; never automatically execute again."""


@contextmanager
def home_host_executor(service, intent_id, actor):
    token = _executor.set((service, intent_id, actor))
    try:
        yield
    finally:
        _executor.reset(token)


class HostCommand(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    assignment_id: str = Field(min_length=1, max_length=128)
    fence: int = Field(ge=1, strict=True)
    argv: tuple[str, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode='after')
    def bounded_argv(self):
        if (not Path(self.argv[0]).is_absolute() or any('\x00' in s or len(s) > 8192 for s in self.argv)
                or sum(len(s.encode('utf-8')) for s in self.argv) > 32768):
            raise ValueError('Host command requires bounded argv and an absolute executable')
        return self


class HomeHostActionProvider:
    contract_version = ACTION_PROVIDER_CONTRACT.current
    provider_type = 'home-operator'
    provider_instance = 'trusted-host'

    def __init__(self, *, home_host, worker_runtime, artifacts, crypto=None):
        self.home_host = home_host
        self.runtime = worker_runtime
        self.artifacts = artifacts
        self.crypto = crypto

    def actions(self):
        return (ActionDefinition(action_id=HOST_COMMAND_ACTION, title='Run trusted Home host command',
            description='Explicit trusted worker operation; process completion is not host-effect verification',
            capabilities=ActionCapability(prepare=True, execute=True, idempotency=False,
                verification=False, evidence=True),
            risk_class=ActionRiskClass.CRITICAL, required_authority=(HOST_COMMAND_ACTION,),
            required_resource_types=(ResourceType.ENVIRONMENT,),
            filesystem_access='write', process_access=True, network_access=True,
            timeout_seconds=630, retry_max_attempts=1, credential_required=False,
            expected_evidence=('host-command-process',)),)

    def _input(self, request, binding):
        if (request.action_id != HOST_COMMAND_ACTION or request.dry_run or request.credential_ref
                or binding.credential_ref or request.project_id != 'home' or binding.project_id != 'home'
                or binding.provider_type != self.provider_type or binding.provider_instance != self.provider_instance
                or (binding.organization_id, binding.workspace_id) != (request.organization_id, request.workspace_id)):
            raise ActionRequirementError('Exact credential-free Home host binding required')
        payload = HostCommand.model_validate(request.parameters)
        broker = self.home_host.broker
        from codex_web.identity import TenantScope
        actor = broker.identity.actor_for_identity(request.requested_by,
            scope=TenantScope(organization_id=request.organization_id, workspace_id=request.workspace_id))
        if actor.principal_kind != PrincipalKind.HUMAN:
            raise ActionRequirementError('Current human administrator required')
        IdentityService.require_admin(actor)
        assignment = broker.execution_workers.store.assignment(payload.assignment_id)
        if assignment is None or assignment.fence != payload.fence:
            raise ActionRequirementError('Current exact Home assignment required')
        origin = self.home_host.authorize_operation(assignment, operation_id=HOST_COMMAND_ACTION,
            worker_actor=self.runtime.worker_actor, requester_id=actor.identity_id, mutating=True)
        assignment, envelope = origin.assignment, origin.envelope
        if (envelope is None
                or assignment.assigned_worker_id not in envelope.trusted_worker_ids
                or assignment.assigned_worker_id != self.runtime.worker.id
                or request.resource_ids != (envelope.host_resource_id,)
                or binding.resource_ids != (envelope.host_resource_id,)):
            raise ActionRequirementError('Exact current Home host declaration and worker required')
        resource = broker.action_intents.execution.resources.get(envelope.host_resource_id, actor)
        if resource.lifecycle != 'active' or str(resource.resource_type) != 'environment':
            raise ActionRequirementError('Active canonical host Environment required')
        policy = binding.security_policy
        if (not policy.network.enabled or not policy.process.allow_process_execution
                or not policy.process.allow_shell
                or '/' not in policy.filesystem.allowed_read_roots
                or '/' not in policy.filesystem.allowed_write_roots):
            raise ActionRequirementError('Explicit full host network/process/filesystem security policy required')
        backend = self.runtime.trusted_host_backend
        if (backend is None or not backend.probe().ready or self.artifacts is None
                or self.artifacts.content is None or self.artifacts.governance is None
                or self.crypto is None or not envelope.host_output_key_id):
            raise ActionRequirementError('Trusted worker or governed command output storage unavailable')
        key = self.crypto.get_key(envelope.host_output_key_id, actor)
        if key.status != 'active' or key.purpose != 'artifact':
            raise ActionRequirementError('Current canonical Artifact output key required')
        self.crypto._ensure_key_context(key, self._output_context(assignment, request.resource_ids[0], 'preparation'))
        return payload, actor, assignment, envelope, origin.binding

    @staticmethod
    def _output_context(assignment, resource_id, artifact_id):
        return EncryptionContext(organization_id=assignment.organization_id, workspace_id=assignment.workspace_id,
            project_id='home', resource_id=resource_id, object_type='home-host-command-output', object_id=artifact_id)

    async def prepare(self, request, *, binding):
        payload, _, assignment, envelope, _ = self._input(request, binding)
        return {'operation': HOST_COMMAND_ACTION, 'assignment_id': assignment.id,
            'fence': payload.fence, 'worker_id': assignment.assigned_worker_id,
            'host_resource_id': envelope.host_resource_id,
            'max_seconds': envelope.max_command_seconds, 'max_output_bytes': envelope.max_output_bytes,
            'host_filesystem_and_network': True, 'native_sandbox_unchanged': True,
            'effect_verification': False, 'unknown_outcome_replay': False}

    def _refuse_held_duplicate(self, service, initial):
        from codex_web.storage.home_intent_query import HomeIntentRecoveryQuery, HomeCommandIdentity
        query = HomeIntentRecoveryQuery(organization_id=initial.organization_id,
            workspace_id=initial.workspace_id, command=HomeCommandIdentity(
                requested_by=initial.requested_by,provider_type=initial.provider_type,
                provider_instance=initial.provider_instance,resource_ids=initial.resource_ids,
                argv=tuple(initial.request.parameters['argv'])))
        try:
            candidates = service.store.home_recovery_candidates(query)
            for prior in candidates:
                if prior.id == initial.id:
                    continue
                held = prior.status not in {'succeeded','failed','cancelled','expired'}
                if not held and prior.last_receipt_id:
                    held = service.store.recovery_last_receipt(prior).outcome == 'unknown'
                elif prior.execution_started_at is not None:
                    held = True
                if held:
                    raise ActionRequirementError('Original matching host command is held; reconcile intent ' + prior.id)
        except ValueError:
            raise ActionRequirementError('Canonical original host command/receipt inventory unavailable') from None

    def _execution_guard(self, request, binding):
        context = _executor.get()
        if context is None:
            raise ActionRequirementError('Canonical host ActionIntent execution required')
        service, intent_id, executor = context
        initial = service._intent(intent_id, executor)
        if (initial.action_id != HOST_COMMAND_ACTION or initial.request != request
                or initial.binding_id != binding.id or initial.attempt != 1
                or initial.retry_policy.max_attempts != 1):
            raise ActionRequirementError('Host command original one-attempt intent required')

        self._refuse_held_duplicate(service, initial)
        _, _, _, _, selected_home = self._input(request, binding)

        def guard():
            current = service._intent(intent_id, executor)
            service._require_worker(executor)
            service.identity.actor_for_identity(executor.identity_id, scope=executor.tenant)
            if executor.service_token_id:
                token = next((t for t in service.identity.store.load().service_tokens
                    if t.id == executor.service_token_id), None)
                if (token is None or token.revoked_at is not None
                        or token.expires_at is not None and token.expires_at <= time.time()
                        or token.service_identity_id != executor.identity_id
                        or set(token.scopes) != set(executor.service_scopes)
                        or (token.organization_id, token.workspace_id) !=
                            (executor.organization_id, executor.workspace_id)):
                    raise ActionRequirementError('Host action executor credential changed')
            if (current.status != 'executing' or current.request != initial.request
                    or current.attempt != initial.attempt or current.lease is None
                    or initial.lease is None or current.lease.owner != initial.lease.owner
                    or current.lease.expires_at <= time.time()
                    or current.action_definition != initial.action_definition
                    or current.policy_decision != initial.policy_decision
                    or current.policy_decision.outcome != ActionDecisionOutcome.ALLOW
                    or current.security_policy != binding.security_policy
                    or current.security_decision.outcome != 'allow'):
                raise ActionRequirementError('Original host execution is no longer current')
            resolved, _, definition, _ = service.execution.resolve_contract(binding.id, request, actor=executor)
            if resolved != binding or definition != initial.action_definition:
                raise ActionRequirementError('Host binding or action contract changed')
            payload, actor, assignment, envelope, current_home = self._input(request, binding)
            if current_home != selected_home:
                raise ActionRequirementError("Home operator selection changed during host action")
            if current.execution_id != assignment.execution_id:
                raise ActionRequirementError('Host action native execution origin changed')
            authority = service._canonical_authority_snapshot(definition=definition,
                request=request, actor=actor, execution_id=assignment.execution_id)
            if (authority.outcome != ActionDecisionOutcome.ALLOW
                    or authority.definition_refs != initial.authority_decision.definition_refs):
                raise ActionRequirementError('Current host Role authority changed')
            return payload, actor, assignment, envelope

        return intent_id, guard

    async def execute(self, request, *, binding, credential=None):
        if credential is not None:
            raise ActionRequirementError('Host command accepts no credential material')
        from codex_web.services.post_turn_readiness import PostTurnReadinessService
        def prepare():
            intent_id, guard = self._execution_guard(request, binding)
            return intent_id, guard, guard()
        async def prepare_owned():
            return await asyncio.to_thread(prepare)
        intent_id, guard, current = await PostTurnReadinessService._run_owned(
            prepare_owned, seconds=10, name='home-host-admission')
        payload, actor, assignment, envelope = current
        started = time.time()

        def operation():
            guard()
            # Governed output stays outside the native workspace/model context.
            artifact = self.artifacts.create_artifact(ArtifactCreate(project_id='home',
                execution_id=assignment.execution_id, execution_workspace_id=assignment.execution_workspace_id,
                resource_ids=request.resource_ids, artifact_type='log', name='Trusted host command output',
                provider=self.provider_type, source='action-intent:' + intent_id,
                classification='secret', deny_model_context=True,
                metadata={'intent_id': intent_id, 'worker_id': assignment.assigned_worker_id,
                    'assignment_id': assignment.id, 'fence': assignment.fence}), actor=self.runtime.worker_actor)
            result = self.runtime.execute_trusted_host_operation(assignment, payload.argv,
                admission_guard=guard, timeout_seconds=envelope.max_command_seconds,
                max_output_bytes=envelope.max_output_bytes)
            try:
                guard()
                raw = json.dumps({'stdout': result.stdout, 'stderr': result.stderr}, ensure_ascii=True).encode()
                encrypted = self.crypto.encrypt(envelope.host_output_key_id, raw,
                    self._output_context(assignment, request.resource_ids[0], artifact.id), actor=actor)
                del raw
                encoded = encrypted.model_dump_json().encode()
                self.artifacts.attach_artifact_content(artifact.id, (encoded,), actor=self.runtime.worker_actor,
                    media_type='application/vnd.codex-web.encrypted-envelope+json',
                    expected_sha256=hashlib.sha256(encoded).hexdigest(), encryption_key_ref=encrypted.key_id)
                guard()
            except Exception as error:
                raise HostActionOutcomeUnknown('Host process may have effects; inspect original intent and output artifact') from error
            return result, artifact.id

        from codex_web.services.post_turn_readiness import PostTurnReadinessService
        async def owned():
            return await asyncio.to_thread(operation)
        result, artifact_id = await PostTurnReadinessService._run_owned(owned,
            seconds=min(envelope.max_command_seconds + 30, 630), name='host-command-' + intent_id)
        return ActionResult(provider_binding_id=binding.id, action_id=HOST_COMMAND_ACTION,
            status='succeeded' if result.succeeded else 'failed', started_at=started,
            completed_at=time.time(), idempotency_key=request.idempotency_key,
            output={'process_completed': True, 'exit_code': result.exit_code,
                'command_digest': result.command_digest, 'output_artifact_id': artifact_id,
                'effect_verified': False, 'output_truncated': result.output_truncated,
                'timed_out': result.timed_out, 'limit_breach': result.limit_breach},
            evidence=(ActionEvidence(evidence_type='host-command-process', reference=artifact_id,
                summary='Bounded worker process completion only; requested host effects remain unverified'),))

    async def verify(self, result, *, binding, credential=None):
        raise UnsupportedActionCapabilityError('verification')

    async def rollback(self, result, *, binding, credential=None):
        raise UnsupportedActionCapabilityError('rollback')
