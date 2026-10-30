"""Validate the fixed governed observation input; never assert QA/merge consent."""
import hashlib
import json

from codex_web.authority import AuthorityEvaluationRequest
from codex_web.api.authorization import ApiAuthorizationService
from codex_web.services.identity import IdentityError
from codex_web.code_host_merge_requirements import CodeHostMergeRequirementsFact
from codex_web.code_host_mr_observation import dispositions
from codex_web.data_governance import ContextFilterRequest
from codex_web.mr_continuation_input import METHOD, INPUT_METHOD, encoded, input_values
from codex_web.mr_observation_evidence import current_observation
from codex_web.mr_observation_policy import INPUT_V2, CONTINUATION_V2, validate_policy, canonical, expiry_valid


def validate_mr_procedure_input(owner, artifact_id, content, actor, assignment, *, protocol=INPUT_METHOD):
    if protocol not in (INPUT_METHOD, INPUT_V2):
        raise ValueError('mr_input_protocol_unknown')
    method = CONTINUATION_V2 if protocol == INPUT_V2 else METHOD
    service = owner.evidence
    artifact = service.get_artifact(artifact_id, actor=actor)
    worker = owner._live(assignment)
    worker_actor = owner.identity.actor_for_identity(worker.service_identity_id, scope=actor.tenant)
    context_requester_id = (assignment.resource_context.requester_identity_id
        if assignment.resource_context is not None else assignment.created_by)
    try:
        context_requester = owner.identity.actor_for_identity(context_requester_id, scope=actor.tenant)
    except IdentityError as exc:
        raise ValueError('mr_input_context_requester_revoked') from exc
    meta = artifact.metadata
    if (meta.get('input_method') != protocol or artifact.producer_identity_id != worker_actor.identity_id
            or artifact.execution_id != assignment.execution_id
            or artifact.execution_workspace_id != assignment.execution_workspace_id
            or artifact.work_item_ref != assignment.work_item_ref
            or meta.get('assignment_id') != assignment.id or meta.get('assignment_fence') != assignment.fence
            or meta.get('requester_identity_id') != context_requester_id
            or actor.identity_id != assignment.created_by
            or len(artifact.resource_ids) != 1 or not set(artifact.resource_ids).issubset(assignment.resource_ids)
            or not artifact.governance_record_id or service.governance is None):
        raise ValueError('mr_input_scope_or_producer')
    from codex_web.services.artifact_evidence import VerificationNotFoundError
    try:
        record = service.get_verification(meta.get('verification_id'), actor=actor,
            work_item_ref=assignment.work_item_ref)
    except VerificationNotFoundError as exc:
        raise ValueError('mr_input_verification_unavailable') from exc
    if (record.method != method or record.result == 'invalidated' or len(record.evidence_ids) != 3
            or record.verifier_identity_id != worker_actor.identity_id
            or record.execution_id != assignment.execution_id or record.source != artifact.source
            or sorted(record.evidence_ids) != json.loads(meta.get('source_evidence_ids', 'null'))):
        raise ValueError('mr_input_verification_identity')
    proofs = [service.get_evidence(i, actor=actor) for i in record.evidence_ids]
    snapshots = set()
    for proof in proofs:
        pm = proof.metadata
        if (proof.lifecycle != 'valid' or not current_observation(proof, now=owner.clock(),
                configuration=getattr(owner, 'mr_observation_configuration', None))
                or proof.retention_expires_at is not None and proof.retention_expires_at <= owner.clock()
                or proof.producer_identity_id != worker_actor.identity_id
                or proof.project_id != assignment.project_id or proof.work_item_ref != assignment.work_item_ref
                or proof.execution_id != assignment.execution_id
                or proof.execution_workspace_id != assignment.execution_workspace_id
                or proof.resource_ids != artifact.resource_ids or proof.source != artifact.source
                or proof.external_id != artifact.external_id or not proof.governance_record_id
                or pm.get('verification_method') != method or pm.get('assignment_id') != assignment.id
                or pm.get('assignment_fence') != assignment.fence
                or pm.get('requester_identity_id') != context_requester_id
                or pm.get('request_sha256') != meta.get('request_sha256')
                or pm.get('continuation_input_artifact_id') != artifact.id):
            raise ValueError('mr_input_evidence_identity_or_expired')
        raw = pm.get('provider_snapshot_json')
        if (not isinstance(raw, str) or len(raw.encode()) > 262144
                or hashlib.sha256(raw.encode()).hexdigest() != pm.get('provider_snapshot_sha256')):
            raise ValueError('mr_input_original_snapshot')
        snapshots.add(raw)
    if len(snapshots) != 1:
        raise ValueError('mr_input_mixed_snapshots')
    observation = json.loads(snapshots.pop())
    fact = CodeHostMergeRequirementsFact.model_validate(observation['item'])
    scope = observation['scope']
    if protocol == INPUT_V2:
        policy = validate_policy(scope.get('observation_policy'), owner.mr_observation_configuration, now=owner.clock())
        expected_policy = canonical(policy.model_dump(mode='json'))
        if (not expiry_valid(policy, fact.observed_at, fact.expires_at)
                or meta.get('observation_policy') != expected_policy
                or any(p.metadata.get('observation_policy') != expected_policy for p in proofs)):
            raise ValueError('mr_input_policy_provenance_changed')
    expected_scope = {'organization_id':assignment.organization_id, 'workspace_id':assignment.workspace_id,
        'project_id':assignment.project_id, 'repository_id':artifact.resource_ids[0],
        'assignment_id':assignment.id, 'execution_id':assignment.execution_id, 'fence':assignment.fence,
        'requester_identity_id':assignment.created_by, 'inspection_kind':'worker',
        'inspecting_identity_id':worker_actor.identity_id, 'inspecting_worker_identity_id':worker_actor.identity_id,
        'definition':assignment.execution_profile_definition.model_dump(mode='json')}
    if (any(scope.get(k) != v for k,v in expected_scope.items())
            or fact.resource_id != artifact.resource_ids[0] or fact.external_id != artifact.external_id
            or fact.head_revision != artifact.revision):
        raise ValueError('mr_input_snapshot_scope')
    values = input_values(fact, expected_head=artifact.revision, protocol=protocol)
    if encoded(values) != content or meta.get('observed_requirements_fingerprint') != values['observed_requirements_fingerprint']:
        raise ValueError('mr_input_content_not_derived')
    outcomes = dispositions(fact, expected_head=artifact.revision, now=owner.clock())
    if ({p.metadata.get('observation_kind'):(p.evidence_type.value,p.result.value) for p in proofs} != outcomes
            or any(p.observed_at != fact.observed_at or p.metadata.get('observation_expires_at') != fact.expires_at
                or p.metadata.get('provider_read_id') != scope.get('read_id') for p in proofs)):
        raise ValueError('mr_input_disposition_changed')
    results = [value[1] for value in outcomes.values()]
    expected_result = 'rejected' if 'fail' in results else 'verified' if all(x == 'pass' for x in results) else 'inconclusive'
    if record.result != expected_result:
        raise ValueError('mr_input_verification_result_changed')
    binding = owner.intents.execution.registry.binding(fact.binding_id.removeprefix('control-plane-read:'), worker_actor)
    actual_binding = hashlib.sha256(encoded(binding.model_dump(mode='json'))).hexdigest()
    if actual_binding != meta.get('provider_binding_sha256'):
        raise ValueError('mr_input_current_provider_binding_changed')
    governance = service.governance
    governed = governance.get_record(artifact.governance_record_id, actor, context_only=True)
    sources = tuple(p.governance_record_id for p in proofs)
    if not set(sources).issubset(governed.source_record_ids):
        raise ValueError('mr_input_governed_derivation_missing')
    principals = (actor, context_requester, worker_actor)
    request = ContextFilterRequest(record_ids=(governed.id, *sources))
    batch = getattr(governance, 'filter_context_for_actors', None)
    decisions = (tuple(batch(request, actors=principals)) if callable(batch) else
        tuple(governance.filter_context(request, actor=principal) for principal in principals))
    if len(decisions) != len(principals):
        raise ValueError('mr_input_current_governance_denied')
    for principal, decision in zip(principals, decisions):
        if decision.denied_record_ids:
            raise ValueError('mr_input_current_governance_denied')
        ApiAuthorizationService(owner.authority).authorize_operation('GET', '/api/verifications', principal)
        for capability in ('repository.pull-request.merge-requirements.read',):
            decision = owner.authority.evaluate(AuthorityEvaluationRequest(capability=capability, level='read',
                project_id=assignment.project_id, resource_ids=artifact.resource_ids,
                environment=owner._facts(assignment, principal)['environment']), actor=principal)
            if decision.outcome != 'allow':
                raise ValueError('mr_input_current_authority_denied')
