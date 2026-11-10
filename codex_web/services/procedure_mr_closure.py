"""Read-only closure prerequisites from canonical original owners.

The CI original may belong to a preceding procedure. This owner never executes,
reconciles, replaces, or imports it. A successful observation is not a CI pass.
Application composition supplies the existing current-policy acceptance owner
and the existing assignment-authorized CodeHost merge-requirement read facade.
"""
from __future__ import annotations

import inspect
import json
import math
import time

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from codex_web.code_host_merge_requirements import CodeHostMergeRequirementsFact
from codex_web.definitions import DefinitionReference
from codex_web.execution_role_models import MRCIReadinessQualificationPolicy
from codex_web.models import WorkItemState
from codex_web.mr_closure_acceptance import MRClosureScope
from codex_web.procedure_control_plane import CanonicalOperationContract, CanonicalOperationResult
from codex_web.services.code_host_ci_observation import CI_OBSERVATION_ACTION_ID, parameters
from codex_web.services.procedure_provider_results import CodeHostProcedureResultAdapter
from codex_web.skill_procedures import ProcedureValueContract


OPERATION = 'code-host.mr-closure.prerequisites'


def provider_pull_request_is_open(fact: CodeHostMergeRequirementsFact) -> bool:
    """Recognize exact provider protocol states, not an arbitrary casefold.

    Readers preserve GitLab OPENED and GitHub OPEN. The lower-case forms
    remain supported for existing typed observations; none of these spellings
    represents closed, merged or an unknown provider's state. This predicate
    supplies no CI, review, policy, scope or merge authority.
    """
    return ((fact.provider_type == 'gitlab' and fact.pull_request_state in ('OPENED', 'opened'))
        or (fact.provider_type == 'github' and fact.pull_request_state in ('OPEN', 'open')))


class MRClosurePrerequisites(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    pull_request_number: StrictInt = Field(ge=1, le=2147483647)
    expected_head_sha: str = Field(pattern=r'^[0-9a-f]{40}$')
    expected_base_sha: str = Field(pattern=r'^[0-9a-f]{40}$')
    ci_intent_id: str = Field(pattern=r'^action-intent-[0-9a-f]{32}$')
    ci_receipt_id: str = Field(pattern=r'^action-receipt-[0-9a-f]{32}$')
    ci_verification_id: str = Field(pattern=r'^action-verification-[0-9a-f]{32}$')
    review_artifact_id: str = Field(pattern=r'^artifact-[0-9a-f]{32}$')
    review_verification_id: str = Field(pattern=r'^verification-[0-9a-f]{32}$')


CONTRACT = CanonicalOperationContract(operation_id=OPERATION, contract_version='1.0',
    resource_types=('repository',), required_capability='repository.read.metadata',
    authority_level='read', input_model=MRClosurePrerequisites,
    output_contracts=(
        ProcedureValueContract(name='expected_head_sha', type='string', description='Exact qualified MR head; no merge grant.'),
        ProcedureValueContract(name='expected_base_sha', type='string', description='Exact freshly observed target tip.'),
    ), effect='read', timeout_seconds=120, max_attempts=1)


async def _resolved(value):
    return await value if inspect.isawaitable(value) else value


class MRClosurePrerequisiteOwner:
    """Injected canonical owners, with no default acceptance or provider client.

    acceptance(scope, artifact_id, verification_id, *, actor) must invoke the
    actual current-policy receipt owner and return its accepted MRClosureScope
    subclass. It must preserve stricter principal independence requirements.
    read_requirements(context, scope) must reuse the existing authorized CodeHost
    metadata facade, returning a CodeHostMergeRequirementsFact. ci_requirement
    must resolve the actual current Role policy and exact DefinitionReference;
    unavailable is never absent. None of these callbacks
    is selected by stored Definition/input data.
    """
    def __init__(self, intents, work_items, *, acceptance=None, read_requirements=None, ci_requirement=None, clock=time.time):
        self.intents = intents
        self.work_items = work_items
        self.acceptance = acceptance
        self.read_requirements = read_requirements
        self.ci_requirement = ci_requirement
        self.clock = clock

    async def _ci_requirement(self, scope, actor):
        selected = await _resolved(self.ci_requirement(scope, actor=actor))
        if (type(selected) is not tuple or len(selected) != 2
                or not isinstance(selected[1], DefinitionReference)
                or selected[1].kind != 'execution-role-catalog'
                or selected[1].definition_id != 'execution-roles.default'
                or selected[1].definition_schema_version not in {'1.0', '1.1'}
                or selected[0] is not None and (type(selected[0]) is not MRCIReadinessQualificationPolicy
                    or selected[1].definition_schema_version != '1.1')):
            raise ValueError('mr_closure_current_ci_policy_invalid')
        return selected

    def _original(self, context, request):
        context.validate_current()
        actor, assignment = context.requester_actor, context.assignment
        intent = self.intents.get(request.ci_intent_id, actor)
        original = intent.request
        p = parameters(original)
        tenant = (assignment.organization_id, assignment.workspace_id, assignment.project_id)
        if (len(context.resource_ids) != 1 or intent.resource_ids != context.resource_ids
                or intent.action_id != CI_OBSERVATION_ACTION_ID or intent.provider_type != 'gitlab'
                or intent.status != 'succeeded' or intent.requested_by != actor.identity_id
                or (intent.organization_id, intent.workspace_id, intent.project_id) != tenant
                or (original.organization_id, original.workspace_id, original.project_id) != tenant
                or original.action_id != intent.action_id or original.resource_ids != intent.resource_ids
                or original.requested_by != intent.requested_by or original.idempotency_key != intent.idempotency_key
                or intent.work_item_ref not in {None, p.work_item_ref}
                or assignment.work_item_ref is not None and assignment.work_item_ref != p.work_item_ref):
            raise ValueError('mr_closure_original_scope_or_ownership')
        if (p.pull_request_number != request.pull_request_number
                or p.expected_head_sha != request.expected_head_sha):
            raise ValueError('mr_closure_original_request_mismatch')
        registry = self.intents.execution.registry
        binding = registry.binding(intent.binding_id, actor)
        provider = registry.provider(intent.provider_type, intent.provider_instance, actor=actor)
        if (binding.provider_type, binding.provider_instance) != (intent.provider_type, intent.provider_instance):
            raise ValueError('mr_closure_provider_binding_changed')
        resource = provider.contract.resource(original)
        repository = provider.contract.locator_for_resource(resource)
        work = self.work_items.state_machine._work_item_state(p.work_item_ref)
        if not isinstance(work, WorkItemState):
            raise ValueError('mr_closure_canonical_work_unavailable')
        source = work.source_identity
        if (work.ref != p.work_item_ref or work.kind != 'merge_request'
                or (work.organization_id, work.workspace_id, work.project_id) != tenant
                or tuple(work.resource_ids) != intent.resource_ids or work.project_path != repository
                or work.ref != repository + '!' + str(request.pull_request_number)
                or source is None or source.source_type != 'gitlab' or source.external_id != work.ref
                or source.source_instance.rstrip('/').removesuffix('/api/v4')
                    != provider.api_base.rstrip('/').removesuffix('/api/v4')
                or work.closed_at is not None or work.current_stage == 'closed'):
            raise ValueError('mr_closure_work_source_mismatch')
        history = self.intents.store.load(intent_id=intent.id)
        receipts = [r for r in history.receipts if r.id == request.ci_receipt_id]
        verifications = [v for v in history.verifications if v.id == request.ci_verification_id]
        if len(receipts) != 1 or len(verifications) != 1:
            raise ValueError('mr_closure_original_receipt_ambiguous')
        receipt, verification = receipts[0], verifications[0]
        result = receipt.result
        if (intent.last_receipt_id != receipt.id or intent.last_verification_id != verification.id
                or receipt.intent_id != intent.id or receipt.attempt != intent.attempt
                or receipt.outcome != 'completed' or result is None or result.status != 'succeeded'
                or (receipt.provider_type, receipt.provider_instance, receipt.action_id,
                    receipt.idempotency_key, receipt.correlation_id) != (intent.provider_type,
                    intent.provider_instance, intent.action_id, intent.idempotency_key, intent.correlation_id)
                or result.provider_binding_id != intent.binding_id
                or verification.intent_id != intent.id or not verification.verified
                or verification.provider_verification is None or not verification.provider_verification.verified
                or intent.expected_evidence and verification.evidence_satisfied is not True
                or not all(math.isfinite(t) for t in (result.started_at, result.completed_at,
                    receipt.received_at, verification.verified_at))
                or not result.started_at <= result.completed_at <= receipt.received_at <= verification.verified_at <= self.clock()):
            raise ValueError('mr_closure_original_positive_proof_required')
        projection = CodeHostProcedureResultAdapter(provider_instance=intent.provider_instance).project(original, result)
        scope = MRClosureScope(organization_id=assignment.organization_id, workspace_id=assignment.workspace_id,
            project_id=assignment.project_id, work_item_ref=work.ref, repository_resource_id=resource.id,
            provider_binding_id=binding.id, pull_request_number=request.pull_request_number,
            head_sha=request.expected_head_sha, base_sha=request.expected_base_sha)
        snapshot = tuple(x.model_dump(mode='json') for x in (intent, receipt, verification, binding, resource, work))
        return scope, intent, receipt, projection, snapshot

    async def execute(self, context, request):
        request = MRClosurePrerequisites.model_validate(request, strict=True)
        if self.acceptance is None or self.read_requirements is None or self.ci_requirement is None:
            return CanonicalOperationResult(disposition='waiting', code='mr_closure_canonical_owner_unavailable')
        scope, intent, receipt, outputs, snapshot = self._original(context, request)
        references = (intent.id, receipt.id, request.ci_verification_id,
                      request.review_artifact_id, request.review_verification_id)
        facts = {'work_item_ref':scope.work_item_ref, 'ci_disposition':outputs['ci_disposition'],
            'readiness':outputs['readiness'], 'next_owner_identity_id':outputs['next_owner_identity_id'],
            'privileged_operation_authorized':False, 'external_ci_runtime_attested':False}
        requirement = await self._ci_requirement(scope, context.requester_actor)
        policy, reference = requirement
        # Exact-head green provider CI is always mandatory. Dependency/runtime
        # readiness is a separate, explicitly published optional requirement.
        ready = ((policy is None and outputs['readiness'] in {'needs_input', 'ready_for_validation'})
            or (policy is not None and outputs['readiness'] == 'ready_for_validation'
            and outputs.get('execution_target_kind') == policy.execution_target_kind
            and json.loads(outputs['execution_target_requirement_ref_json']) == reference.model_dump(mode='json')))
        if outputs['ci_disposition'] != 'passed' or not ready:
            return CanonicalOperationResult(disposition='waiting', code='mr_closure_ci_not_ready',
                facts=facts, original_references=references)
        accepted = await _resolved(self.acceptance(scope, request.review_artifact_id,
            request.review_verification_id, actor=context.requester_actor))
        if (not isinstance(accepted, MRClosureScope) or getattr(accepted, 'disposition', None) != 'accepted'
                or accepted.model_dump(include=set(MRClosureScope.model_fields)) != scope.model_dump()):
            raise ValueError('mr_closure_acceptance_unproven')
        context.validate_current()
        fact = await _resolved(self.read_requirements(context, scope))
        if not isinstance(fact, CodeHostMergeRequirementsFact):
            raise ValueError('mr_closure_current_metadata_unavailable')
        if (fact.resource_id != scope.repository_resource_id
                or fact.binding_id not in (scope.provider_binding_id, 'control-plane-read:' + scope.provider_binding_id)
                or fact.provider_type != intent.provider_type or fact.provider_instance != intent.provider_instance
                or fact.external_id != str(scope.pull_request_number)
                or fact.head_revision != scope.head_sha or fact.base_revision != scope.base_sha
                or not provider_pull_request_is_open(fact) or fact.draft or fact.changed
                or not fact.observed_at <= self.clock() < fact.expires_at):
            return CanonicalOperationResult(disposition='waiting', code='mr_closure_current_mr_changed',
                facts=facts, original_references=references)
        # Existing verification uses the registered provider and credential
        # custody to freshly read CI and revalidate actual current Role receipts.
        # This does not append/execute/reconcile an ActionIntent.
        fresh = await self.intents.execution.verify(intent.binding_id, receipt.result, actor=context.requester_actor)
        if not fresh.verified:
            return CanonicalOperationResult(disposition='waiting', code='mr_closure_ci_original_no_longer_verified',
                facts=facts, original_references=references)
        context.validate_current()
        if self._original(context, request)[4] != snapshot:
            raise ValueError('mr_closure_canonical_records_changed')
        final = await _resolved(self.acceptance(scope, request.review_artifact_id,
            request.review_verification_id, actor=context.requester_actor))
        if final != accepted:
            raise ValueError('mr_closure_current_acceptance_changed')
        context.validate_current()
        if self._original(context, request)[4] != snapshot:
            raise ValueError('mr_closure_canonical_records_changed')
        if await self._ci_requirement(scope, context.requester_actor) != requirement:
            raise ValueError('mr_closure_current_ci_policy_changed')
        context.validate_current()
        return CanonicalOperationResult(disposition='succeeded',
            outputs={'expected_head_sha':scope.head_sha, 'expected_base_sha':scope.base_sha},
            facts=facts, original_references=references)
