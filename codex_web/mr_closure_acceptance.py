"""Exact review-manifest semantics for an existing MR, never merge authority.

The caller must freshly load records through the canonical Artifact/Evidence and
Identity owners. A method name or caller-supplied metadata alone is insufficient.
This protocol describes what an independent reviewer accepted; provider CI,
current policy, custody and the eventual SHA-CAS merge remain separate gates.
"""
from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from codex_web.artifact_evidence import Artifact, Verification

REVIEW_METHOD = "code-host-mr-independent-acceptance.v1"
MAX_REVIEW_BYTES = 16 * 1024
Revision = Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
Reference = Annotated[str, Field(min_length=1, max_length=200)]


class MRClosureScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    organization_id: Reference
    workspace_id: Reference
    project_id: Reference
    work_item_ref: Reference
    repository_resource_id: Reference
    provider_binding_id: Reference
    pull_request_number: int = Field(gt=0)
    head_sha: Revision
    base_sha: Revision


class IndependentMRReviewManifest(MRClosureScope):
    schema_version: Literal["1.0"]
    disposition: Literal["accepted", "rejected", "needs_input"]


class MRClosureAcceptanceError(ValueError):
    """A finite semantic refusal; missing review never becomes a pass."""


def check_independent_acceptance(
    scope: MRClosureScope,
    artifact: Artifact,
    verification: Verification,
    content: bytes,
    *,
    implementation_identity_ids: frozenset[str],
    eligible_reviewer_identity_ids: frozenset[str],
    now: float,
) -> IndependentMRReviewManifest:
    """Check canonical records and managed bytes against freshly resolved scope.

    Eligible identities and implementation provenance must come from current
    canonical Role/Work/Identity owners, never the manifest or procedure inputs.
    This pure predicate performs no approval, persistence or external operation.
    """
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_REVIEW_BYTES:
        raise MRClosureAcceptanceError("mr_acceptance_content_bounds")
    if (not implementation_identity_ids or not eligible_reviewer_identity_ids
            or artifact.producer_identity_id not in implementation_identity_ids
            or verification.verifier_identity_id in implementation_identity_ids
            or verification.verifier_identity_id not in eligible_reviewer_identity_ids):
        raise MRClosureAcceptanceError("mr_acceptance_reviewer_not_independent_or_eligible")
    if (artifact.lifecycle != "active" or not artifact.governance_record_id
            or artifact.retention_expires_at is not None and artifact.retention_expires_at <= now
            or artifact.organization_id != scope.organization_id
            or artifact.workspace_id != scope.workspace_id
            or artifact.project_id != scope.project_id
            or artifact.work_item_ref != scope.work_item_ref
            or artifact.resource_ids != (scope.repository_resource_id,)
            or artifact.revision != scope.head_sha
            or artifact.digest is None or artifact.digest.algorithm != "sha256"
            or artifact.digest.value != hashlib.sha256(content).hexdigest()):
        raise MRClosureAcceptanceError("mr_acceptance_artifact_scope_or_content")
    if (verification.organization_id != scope.organization_id
            or verification.workspace_id != scope.workspace_id
            or verification.work_item_ref != scope.work_item_ref
            or verification.artifact_ids != (artifact.id,)
            or verification.evidence_ids
            or verification.independent is not True
            or verification.method != REVIEW_METHOD or verification.result != "verified"
            or tuple(x for x in verification.findings if x.startswith("mr_review_manifest_sha256:"))
                != ("mr_review_manifest_sha256:" + hashlib.sha256(content).hexdigest(),)
            or verification.verified_at < artifact.produced_at
            or verification.verified_at > now):
        raise MRClosureAcceptanceError("mr_acceptance_verification_provenance")
    manifest = IndependentMRReviewManifest.model_validate_json(content)
    if (manifest.model_dump(include=set(MRClosureScope.model_fields)) != scope.model_dump()
            or manifest.disposition != "accepted"):
        raise MRClosureAcceptanceError("mr_acceptance_not_accepted_for_current_scope")
    return manifest
