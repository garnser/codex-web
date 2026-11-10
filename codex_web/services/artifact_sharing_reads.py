"""Current lifecycle/policy checks for existing exact-ID shared-input reads."""
from codex_web.artifact_sharing_policy import METHOD, SOURCE, SOURCE_METHOD
from codex_web.authority import AuthorityEvaluationRequest, AuthorityLevel
from codex_web.data_governance import ContextFilterRequest
from codex_web.identity import TenantScope
from codex_web.services.worker_verifications import WorkerVerificationAdapter
from codex_web.services.artifact_sharing import scoped_work


def validate_shared_input(broker, record, assignment, actor):
    from codex_web.services.artifact_evidence import ArtifactEvidenceError
    from codex_web.services.identity import IdentityError
    from codex_web.services.resources import ResourceCatalogError
    try:
        return _validate_shared_input(broker,record,assignment,actor)
    except (ArtifactEvidenceError,IdentityError,ResourceCatalogError):
        WorkerVerificationAdapter(broker).denied('Shared-input canonical source/identity/Resource unavailable')


def _validate_shared_input(broker, record, assignment, actor):
    """Metadata-only parent reads confer no source-repository execution access.

    Source assignment retirement does not expire immutable captured data. Source
    lifecycle/governance, original principals' data authority and the exact
    published sharing policy remain current, while the normal reader owns the
    receiving assignment's live custody and target Work authorization.
    """
    import json
    from codex_web.services.definitions import DefinitionError, DefinitionNotFoundError, DefinitionConflictError, DefinitionCompatibilityError
    owner = WorkerVerificationAdapter(broker)
    policies = getattr(broker,'artifact_sharing_policies',None)
    if policies is None:
        owner.denied('Current shared-input policy owner unavailable')
    service = owner.service()
    if service.governance is None:
        owner.denied('Current shared-input governance unavailable')
    artifact = (service.get_artifact(record.artifact_ids[0],actor=actor)
        if hasattr(record,'artifact_ids') and len(record.artifact_ids)==1 else record)
    if (artifact.provider != METHOD or artifact.artifact_type != 'report' or artifact.lifecycle != 'active'
            or record.lifecycle not in {'active','valid'} or artifact.digest is None
            or artifact.work_item_ref != record.work_item_ref or artifact.resource_ids != record.resource_ids):
        owner.denied('Shared-input derivative identity or lifecycle changed')
    meta = artifact.metadata
    source = service.get_artifact(meta.get('source_artifact_id'),actor=actor)
    proof = service.get_evidence(meta.get('source_evidence_id'),actor=actor)
    if (source.provider != SOURCE_METHOD or proof.provider != SOURCE_METHOD
            or source.source != SOURCE or proof.source != SOURCE
            or source.lifecycle != 'active' or proof.lifecycle != 'valid'
            or proof.result != 'info' or proof.evidence_type != 'artifact_verification'
            or proof.artifact_ids != (source.id,) or source.digest != proof.digest
            or source.digest != artifact.digest or meta.get('source_content_sha256') != source.digest.value
            or source.producer_identity_id != artifact.producer_identity_id
            or source.work_item_ref != meta.get('source_work_item_ref')
            or (source.organization_id,source.workspace_id,source.project_id) !=
                (assignment.organization_id,assignment.workspace_id,assignment.project_id)):
        owner.denied('Shared-input canonical source or original digest revoked/changed')
    target = scoped_work(broker,assignment,artifact.work_item_ref)
    source_work = scoped_work(broker,assignment,source.work_item_ref)
    if (set(target.resource_ids) != set(artifact.resource_ids)
            or not set(source.resource_ids).issubset(source_work.resource_ids)):
        owner.denied('Shared-input canonical Work/resource scope changed')
    try:
        policy,reference = policies.resolve(assignment,actor,now=broker._clock())
    except (DefinitionError,DefinitionNotFoundError,DefinitionConflictError,DefinitionCompatibilityError,ValueError):
        owner.denied('Current shared-input policy unavailable')
    if not any(set(source.resource_ids).issubset(r.source_resource_ids)
                and set(artifact.resource_ids).issubset(r.target_resource_ids)
                and artifact.content is not None and artifact.content.size_bytes <= r.max_content_bytes
                for r in policy.rules):
        owner.denied('Shared-input current sharing direction revoked or denied')
    tenant = TenantScope(organization_id=assignment.organization_id,workspace_id=assignment.workspace_id)
    original_requester = broker.identity.actor_for_identity(meta.get('requester_identity_id'),scope=tenant)
    producer = broker.identity.actor_for_identity(source.producer_identity_id,scope=tenant)
    if (service.find_idempotent_artifact(actor=producer,source=SOURCE,idempotency_key=source.external_id) != source
            or service.find_idempotent_evidence(actor=producer,source=SOURCE,idempotency_key=source.external_id) != proof
            or proof.external_id != source.external_id):
        owner.denied('Shared-input canonical capture identity changed')
    from codex_web.services.worker_artifact_sharing import DELIVERY_SOURCE
    original_info = service.find_idempotent_evidence(actor=producer,source=DELIVERY_SOURCE,idempotency_key=artifact.external_id)
    if (service.find_idempotent_artifact(actor=producer,source=DELIVERY_SOURCE,idempotency_key=artifact.external_id) != artifact
            or original_info is None or original_info.lifecycle != 'valid' or original_info.result != 'info'
            or original_info.artifact_ids != (artifact.id,) or original_info.digest != artifact.digest
            or hasattr(record,'artifact_ids') and record != original_info):
        owner.denied('Shared-input is not the canonical INFO delivery identity')
    captured = broker.execution_workers.store.assignment(source.metadata.get('assignment_id'))
    registered = broker.execution_workers.store.worker(source.metadata.get('worker_id'))
    if (captured is None or registered is None or registered.lifecycle in {'revoked','quarantined'}
            or registered.revoked_at is not None or registered.service_identity_id != producer.identity_id
            or (captured.organization_id,captured.workspace_id,captured.project_id,captured.id,
                captured.assigned_worker_id,captured.execution_id,captured.execution_workspace_id) !=
                (source.organization_id,source.workspace_id,source.project_id,meta.get('assignment_id'),
                 source.metadata.get('worker_id'),source.execution_id,source.execution_workspace_id)
            or not set(source.resource_ids).issubset(captured.resource_ids)
            or source.metadata.get('assignment_fence') != meta.get('assignment_fence')):
        owner.denied('Shared-input original producer/custody no longer attributable')
    if captured.agent_profile is None:
        owner.denied('Shared-input original Profile provenance unavailable')
    current_profile = broker.execution_profiles.agent_profiles.get(captured.agent_profile.profile_id,actor=original_requester)
    if current_profile.lifecycle != 'active':
        owner.denied('Shared-input source Profile revoked')
    for resource_id in set(source.resource_ids)|set(artifact.resource_ids):
        resource = policies.resources.get(resource_id,actor)
        if resource.lifecycle != 'active' or resource.resource_type != 'repository':
            owner.denied('Shared-input repository Resource revoked')
    for principal in (original_requester,producer):
        decision = broker.authority.evaluate(AuthorityEvaluationRequest(capability='api.artifacts.share',
            level=AuthorityLevel.EXECUTE,project_id=assignment.project_id,
            resource_ids=tuple(sorted(set(source.resource_ids)|set(artifact.resource_ids)))),actor=principal,now=broker._clock())
        if decision.outcome != 'allow' or decision.definition_ref is None:
            owner.denied('Shared-input original data-sharing authority revoked')
    for item in (record,artifact,original_info,source,proof):
        if (not item.governance_record_id or item.retention_expires_at is not None
                and item.retention_expires_at <= broker._clock()):
            owner.denied('Shared-input record expired or lacks governance')
        if service.governance.filter_context(ContextFilterRequest(
                record_ids=(item.governance_record_id,)),actor=actor).denied_record_ids:
            owner.denied('Shared-input source/derivative governance denied content')
