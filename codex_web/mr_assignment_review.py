"""Versioned assignment/Role MR review provenance, never principal independence.

Canonical owners load every referenced record. Untrusted manifests contain review
judgment and selection hints only; none of their identity labels grant authority.
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from codex_web.definitions import DefinitionReference
from codex_web.models import QueuedTurn
from codex_web.owner_routing import OwnerRoutingPin
from codex_web.mr_closure_acceptance import MRClosureScope, MAX_REVIEW_BYTES
from codex_web.services.queued_native_start import validate_attribution

METHOD = 'code-host-mr-assignment-review.v1'
RECEIPT_CONTRACT = 'mr-assignment-review-receipt.v1'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class AssignmentReviewError(ValueError):
    """Missing provenance is inconclusive, never invented independent acceptance."""


class AssignmentMRReviewManifest(MRClosureScope):
    schema_version: Literal['1.0']
    disposition: Literal['accepted', 'rejected', 'needs_input']


class AssignmentReviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    method: Literal['code-host-mr-assignment-review.v1']
    work_item_ref: str = Field(min_length=1, max_length=240)
    manifest_artifact_id: str = Field(min_length=1, max_length=200)
    implementation_intent_id: str = Field(min_length=1, max_length=200)
    implementation_assignment_id: str = Field(min_length=1, max_length=200)
    implementation_preflight_id: str = Field(min_length=1, max_length=200)
    validation_preflight_id: str = Field(min_length=1, max_length=200)
    expected_handoff_requested_at: float = Field(gt=0, allow_inf_nan=False)
    request_id: str = Field(pattern=r'^[a-f0-9]{32}$')


class ReviewAssignmentProvenance(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    assignment_id: str
    execution_id: str
    execution_workspace_id: str
    fence_at_start: StrictInt = Field(ge=1)
    worker_id: str
    worker_identity_id: str
    profile_id: str
    profile_revision: StrictInt = Field(ge=1)
    profile_record_id: str
    role_id: str
    execution_profile_definition: DefinitionReference
    preflight_id: str
    work_item_ref: str
    handoff_requested_at: float | None = None
    routing_pin: OwnerRoutingPin | None = None
    thread_id: str
    repository_resource_id: str | None = None
    writable_repository_resource_ids: tuple[str, ...] = ()
    read_only_repository_resource_ids: tuple[str, ...] = ()
    native_session_id: str
    native_turn_id: str
    started_at: float


class AssignmentMRReviewReceipt(MRClosureScope):
    schema_version: Literal['1.0'] = '1.0'
    contract: Literal['mr-assignment-review-receipt.v1'] = RECEIPT_CONTRACT
    manifest_artifact_id: str
    manifest_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    disposition: Literal['accepted', 'rejected', 'needs_input']
    implementation: ReviewAssignmentProvenance
    validation: ReviewAssignmentProvenance
    implementation_intent_id: str
    publication_receipt_id: str
    publication_verification_id: str
    handoff_requested_at: float
    handoff_acknowledged_at: float
    handoff_from_role: str
    handoff_to_role: str
    policy_definition_ref: DefinitionReference
    basis: Literal['assignment_role'] = 'assignment_role'
    # This is a semantic distinction, not a caller-selected independence flag.
    recorded_at: float


def review_policy(catalog):
    policy = catalog.mr_review_policy
    if policy is None or policy.basis != 'assignment_role':
        raise AssignmentReviewError('mr_assignment_review_explicit_policy_required')
    return policy


def manifest_content(scope, artifact, content, *, now):
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_REVIEW_BYTES:
        raise AssignmentReviewError('mr_assignment_review_content_bounds')
    if (artifact.lifecycle != 'active' or not artifact.governance_record_id
            or artifact.retention_expires_at is not None and artifact.retention_expires_at <= now
            or (artifact.organization_id, artifact.workspace_id, artifact.project_id, artifact.work_item_ref)
                != (scope.organization_id, scope.workspace_id, scope.project_id, scope.work_item_ref)
            or artifact.resource_ids != (scope.repository_resource_id,) or artifact.revision != scope.head_sha
            or artifact.digest is None or artifact.digest.algorithm != 'sha256'
            or artifact.digest.value != hashlib.sha256(content).hexdigest()):
        raise AssignmentReviewError('mr_assignment_review_artifact_scope_or_digest')
    manifest = AssignmentMRReviewManifest.model_validate_json(content)
    if manifest.model_dump(include=set(MRClosureScope.model_fields)) != scope.model_dump():
        raise AssignmentReviewError('mr_assignment_review_manifest_scope')
    return manifest


def start_provenance(preflights, attempt, assignment, worker):
    if (attempt is None or worker is None or assignment.agent_profile is None
            or not assignment.execution_workspace_id):
        raise AssignmentReviewError('mr_assignment_review_start_provenance_missing')
    c, e, r = attempt.queued_start_correlation, attempt.queued_start_evidence, attempt.queued_start_receipt
    if c is None or e is None or r is None:
        raise AssignmentReviewError('mr_assignment_review_positive_start_required')
    validate_attribution(preflights, attempt, c)
    original = QueuedTurn.model_validate(attempt.queued_turn_snapshot)
    if not original.work_item_ref:
        raise AssignmentReviewError('mr_assignment_review_start_work_attribution_missing')
    pin = assignment.agent_profile
    if (c.assignment_id != assignment.id or c.execution_id != assignment.execution_id
            or c.worker_id != assignment.assigned_worker_id or worker.id != c.worker_id
            or (worker.organization_id, worker.workspace_id) != (assignment.organization_id, assignment.workspace_id)
            or not worker.service_identity_id or not 1 <= c.fence <= assignment.fence
            or c.agent_profile_id != pin.profile_id or c.agent_profile_revision != pin.profile_revision
            or c.assignment_resource_ids != assignment.resource_ids
            or c.project_id != assignment.project_id
            or (c.organization_id, c.workspace_id) != (assignment.organization_id, assignment.workspace_id)
            or (e.runtime_generation, e.rpc_request_id, e.provider_native_session_id)
                != (c.runtime_generation, c.rpc_request_id, c.provider_native_session_id)
            or (r.admission_claim_id, r.requested_execution_id, r.execution_id, r.thread_id,
                r.provider_native_session_id, r.provider_native_turn_id, r.observed_at)
                != (c.admission_claim_id, c.requested_execution_id, c.execution_id, c.thread_id,
                    e.provider_native_session_id, e.provider_native_turn_id, e.observed_at)):
        raise AssignmentReviewError('mr_assignment_review_start_attribution_changed')
    return ReviewAssignmentProvenance(assignment_id=assignment.id, execution_id=assignment.execution_id,
        execution_workspace_id=assignment.execution_workspace_id,
        fence_at_start=c.fence, worker_id=worker.id, worker_identity_id=worker.service_identity_id,
        profile_id=pin.profile_id, profile_revision=pin.profile_revision, profile_record_id=pin.profile_record_id,
        role_id=pin.role_id, execution_profile_definition=assignment.execution_profile_definition,
        preflight_id=attempt.id, work_item_ref=original.work_item_ref,
        handoff_requested_at=original.handoff_requested_at, routing_pin=original.owner_routing_pin,
        thread_id=r.thread_id, repository_resource_id=c.repository_resource_id,
        writable_repository_resource_ids=c.writable_repository_resource_ids,
        read_only_repository_resource_ids=c.read_only_repository_resource_ids,
        native_session_id=r.provider_native_session_id,
        native_turn_id=r.provider_native_turn_id, started_at=float(r.observed_at))


def publication_proof(scope, intent, history, implementation, *, now):
    receipt = next((x for x in history.receipts if x.id == intent.last_receipt_id), None)
    verification = next((x for x in history.verifications if x.id == intent.last_verification_id), None)
    if (intent.status != 'succeeded' or intent.action_id != 'code-host.branch.publish'
            or intent.binding_id != scope.provider_binding_id
            or (intent.organization_id, intent.workspace_id, intent.project_id, intent.work_item_ref)
                != (scope.organization_id, scope.workspace_id, scope.project_id, scope.work_item_ref)
            or intent.resource_ids != (scope.repository_resource_id,)
            or intent.execution_id != implementation.execution_id
            or implementation.work_item_ref != scope.work_item_ref
            or intent.request.parameters.get('execution_workspace_id') is None
            or intent.request.parameters.get('head_revision') != scope.head_sha
            or receipt is None or receipt.intent_id != intent.id or receipt.attempt != intent.attempt
            or (receipt.provider_type, receipt.provider_instance, receipt.action_id, receipt.idempotency_key,
                receipt.correlation_id) != (intent.provider_type, intent.provider_instance, intent.action_id,
                    intent.idempotency_key, intent.correlation_id)
            or receipt.outcome != 'completed' or receipt.result is None or receipt.result.status != 'succeeded'
            or receipt.result.provider_binding_id != intent.binding_id or receipt.result.action_id != intent.action_id
            or receipt.result.idempotency_key != intent.idempotency_key
            or receipt.result.external_id != scope.head_sha
            or receipt.result.output.get('head_revision') != scope.head_sha
            or receipt.result.output.get('resource_id') != scope.repository_resource_id
            or receipt.result.output.get('branch') != intent.request.parameters.get('branch')
            or receipt.result.output.get('execution_workspace_id') != intent.request.parameters.get('execution_workspace_id')
            or verification is None or verification.intent_id != intent.id or not verification.verified
            or verification.provider_verification is None or not verification.provider_verification.verified
            or intent.expected_evidence and verification.evidence_satisfied is not True
            or not implementation.started_at <= receipt.received_at <= verification.verified_at <= now):
        raise AssignmentReviewError('mr_assignment_review_original_publication_unproven')
    return receipt, verification


def validation_artifact_origin(artifact, validation, *, now):
    if (artifact.producer_identity_id != validation.worker_identity_id
            or artifact.execution_id != validation.execution_id
            or artifact.execution_workspace_id != validation.execution_workspace_id
            or not validation.started_at <= artifact.created_at <= now
            or not validation.started_at <= artifact.produced_at <= now):
        raise AssignmentReviewError('mr_assignment_review_validation_artifact_origin')


def qualification(scope, artifact, content, implementation, validation, handoff, catalog, *, now):
    policy = review_policy(catalog)
    manifest = manifest_content(scope, artifact, content, now=now)
    validation_artifact_origin(artifact, validation, now=now)
    if (implementation.work_item_ref != scope.work_item_ref or validation.work_item_ref != scope.work_item_ref
            or implementation.assignment_id == validation.assignment_id
            or implementation.execution_id == validation.execution_id
            or implementation.role_id == validation.role_id
            or implementation.role_id not in policy.implementation_role_ids
            or validation.role_id not in policy.validation_role_ids):
        raise AssignmentReviewError('mr_assignment_review_roles_not_separate_or_eligible')
    if (handoff is None or handoff.status != 'accepted' or handoff.acknowledged_at is None
            or handoff.to_agent != validation.role_id or handoff.from_agent == validation.role_id
            or handoff.from_agent not in catalog.role_map
            or catalog.role_map[handoff.from_agent].lifecycle != 'active'
            or not implementation.started_at <= handoff.requested_at <= handoff.acknowledged_at <= now
            or validation.handoff_requested_at != handoff.requested_at
            or not handoff.requested_at <= validation.started_at <= handoff.acknowledged_at):
        raise AssignmentReviewError('mr_assignment_review_accepted_handoff_unproven')
    pin = validation.routing_pin
    if handoff.owner_routing is not None or pin is not None:
        recipient = pin.recipient if pin is not None else None
        delivery = handoff.delivery
        if (pin is None or pin.binding != handoff.owner_routing or recipient is None
                or pin.work_item_ref != scope.work_item_ref or pin.handoff_requested_at != handoff.requested_at
                or (recipient.assignment_id, recipient.fence, recipient.execution_id, recipient.worker_id, recipient.thread_id)
                    != (validation.assignment_id, validation.fence_at_start, validation.execution_id,
                        validation.worker_id, validation.thread_id)
                or delivery is None or delivery.status != 'delivered' or not delivery.scope_verified
                or (delivery.thread_id, delivery.turn_id, delivery.execution_id, delivery.started_at)
                    != (validation.thread_id, validation.native_turn_id, validation.execution_id, validation.started_at)
                or delivery.execution_profile_id != 'repository-write'
                or delivery.repository_resource_id != scope.repository_resource_id
                or delivery.writable_repository_resource_ids != validation.writable_repository_resource_ids
                or delivery.read_only_repository_resource_ids != validation.read_only_repository_resource_ids):
            raise AssignmentReviewError('mr_assignment_review_routed_recipient_unproven')
    # No inference about delivery from an ACK or from absence of routed metadata.
    # Existing ACK owner has enforced routed/manual-specific intake requirements.
    return manifest


def check_assignment_acceptance(scope, artifact, verification, evidence, content, *, receipt,
        current_catalog, current_catalog_ref, now, require_principal_independence=False):
    """Receipt semantic gate; caller freshly validates its referenced source records.

    This does NOT replace check_independent_acceptance or satisfy an existing
    independent_verification requirement. The authenticated writer owns provenance.
    """
    if require_principal_independence:
        raise AssignmentReviewError('mr_assignment_review_principal_independence_required')
    policy = review_policy(current_catalog)
    if current_catalog_ref.definition_schema_version != '1.1':
        raise AssignmentReviewError('mr_assignment_review_policy_schema_required')
    manifest = manifest_content(scope, artifact, content, now=now)
    validation_artifact_origin(artifact, receipt.validation, now=now)
    if (receipt.model_dump(include=set(MRClosureScope.model_fields)) != scope.model_dump()
            or receipt.policy_definition_ref != current_catalog_ref
            or receipt.manifest_artifact_id != artifact.id or receipt.manifest_sha256 != artifact.digest.value
            or receipt.disposition != 'accepted' or manifest.disposition != 'accepted'
            or receipt.implementation.work_item_ref != scope.work_item_ref
            or receipt.validation.work_item_ref != scope.work_item_ref
            or receipt.validation.handoff_requested_at != receipt.handoff_requested_at
            or not receipt.implementation.started_at <= receipt.handoff_requested_at <= receipt.validation.started_at
                <= receipt.handoff_acknowledged_at <= receipt.recorded_at
            or receipt.handoff_to_role != receipt.validation.role_id
            or receipt.implementation.assignment_id == receipt.validation.assignment_id
            or receipt.implementation.execution_id == receipt.validation.execution_id
            or receipt.implementation.role_id not in policy.implementation_role_ids
            or receipt.validation.role_id not in policy.validation_role_ids
            or receipt.implementation.role_id == receipt.validation.role_id
            or receipt.recorded_at > now
            or evidence.lifecycle != 'valid' or evidence.evidence_type != 'review' or evidence.result != 'pass'
            or not evidence.governance_record_id
            or evidence.retention_expires_at is not None and evidence.retention_expires_at <= now
            or evidence.work_item_ref != scope.work_item_ref
            or evidence.project_id != scope.project_id or evidence.resource_ids != (scope.repository_resource_id,)
            or (evidence.organization_id, evidence.workspace_id) != (scope.organization_id, scope.workspace_id)
            or evidence.producer_identity_id != receipt.validation.worker_identity_id
            or evidence.execution_id != receipt.validation.execution_id
            or evidence.metadata.get('receipt_sha256') != digest(receipt.model_dump(mode='json'))
            or evidence.metadata.get('receipt_contract') != RECEIPT_CONTRACT
            or verification.method != METHOD or verification.result != 'verified'
            or verification.artifact_ids != (artifact.id,) or verification.evidence_ids != (evidence.id,)
            or verification.work_item_ref != scope.work_item_ref
            or (verification.organization_id, verification.workspace_id) != (scope.organization_id, scope.workspace_id)
            or verification.execution_id != receipt.validation.execution_id
            or verification.verifier_identity_id != receipt.validation.worker_identity_id
            or verification.independent != (verification.verifier_identity_id != artifact.producer_identity_id
                and verification.verifier_identity_id != evidence.producer_identity_id)
            or verification.findings != ('mr_assignment_review_receipt_sha256:' + digest(receipt.model_dump(mode='json')),)
            or not artifact.produced_at <= verification.verified_at <= now):
        raise AssignmentReviewError('mr_assignment_review_receipt_not_accepted')
    return manifest
