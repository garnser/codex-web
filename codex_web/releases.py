from __future__ import annotations

import hashlib
import json
import time
import uuid
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.artifact_evidence import EvidenceType


class ReleaseStatus(StrEnum):
    DRAFT = "draft"
    QUALIFIED = "qualified"
    PROMOTING = "promoting"
    DEPLOYED = "deployed"
    BLOCKED = "blocked"
    ROLLED_BACK = "rolled_back"
    SUPERSEDED = "superseded"


class PromotionStatus(StrEnum):
    PENDING = "pending"
    BLOCKED = "blocked"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    QUEUED = "queued"
    DEPLOYED = "deployed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class RolloutStrategy(StrEnum):
    ALL_AT_ONCE = "all_at_once"
    CANARY = "canary"
    PERCENTAGE = "percentage"


class ReleaseArtifactSignature(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    algorithm: str = Field(min_length=1, max_length=100)
    key_ref: str = Field(min_length=1, max_length=500)
    signature: str = Field(min_length=1)
    signed_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    signed_at: float = Field(default_factory=time.time)


@runtime_checkable
class ReleaseArtifactSigner(Protocol):
    def sign_digest(
        self,
        digest: str,
        *,
        credential_ref: str,
    ) -> ReleaseArtifactSignature: ...

    def verify_digest(
        self,
        digest: str,
        signature: ReleaseArtifactSignature,
    ) -> bool: ...


class ReleaseEvidenceRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=200)
    evidence_type: EvidenceType
    provider: str | None = Field(default=None, max_length=200)
    max_age_seconds: float | None = Field(default=None, gt=0)
    independent_verification: bool = False


class ReleasePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default="release-policy-default", min_length=1)
    version: str = Field(default="1", min_length=1, max_length=100)
    require_signature: bool = True
    required_evidence: tuple[ReleaseEvidenceRequirement, ...] = (
        ReleaseEvidenceRequirement(
            id="tests",
            evidence_type=EvidenceType.TEST_RESULT,
        ),
        ReleaseEvidenceRequirement(
            id="ci",
            evidence_type=EvidenceType.CI_CHECK,
        ),
        ReleaseEvidenceRequirement(
            id="security",
            evidence_type=EvidenceType.SECURITY_SCAN,
        ),
    )
    approval_quorum: int = Field(default=1, ge=1, le=20)
    require_distinct_humans: bool = True
    require_rollback_target_for_production: bool = True
    require_migration_compatibility: bool = True
    production_environment_labels: tuple[str, ...] = ("production", "prod")

    @model_validator(mode="after")
    def normalize(self) -> "ReleasePolicy":
        object.__setattr__(
            self,
            "production_environment_labels",
            tuple(
                dict.fromkeys(
                    item.strip().casefold()
                    for item in self.production_environment_labels
                    if item.strip()
                )
            ),
        )
        return self

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()


class ReleaseBuild(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    artifact_id: str = Field(min_length=1)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_revision: str = Field(min_length=1, max_length=500)
    build_id: str = Field(min_length=1, max_length=500)
    sbom_artifact_id: str | None = None
    provenance_evidence_id: str | None = None
    signature: ReleaseArtifactSignature | None = None
    signing_credential_ref: str | None = None

    @model_validator(mode="after")
    def validate_signature_ref(self) -> "ReleaseBuild":
        if self.signature is not None and not self.signing_credential_ref:
            raise ValueError(
                "signed release build must retain signing credential reference"
            )
        return self


class RolloutPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy: RolloutStrategy = RolloutStrategy.ALL_AT_ONCE
    initial_percentage: int | None = Field(default=None, ge=1, le=100)
    step_percentages: tuple[int, ...] = ()
    observation_seconds: int = Field(default=0, ge=0, le=86400)
    rollback_on_verification_failure: bool = True

    @model_validator(mode="after")
    def validate_rollout(self) -> "RolloutPlan":
        if self.strategy == RolloutStrategy.PERCENTAGE:
            if self.initial_percentage is None:
                raise ValueError("percentage rollout requires initial_percentage")
            if any(item < 1 or item > 100 for item in self.step_percentages):
                raise ValueError("rollout percentages must be 1..100")
        return self


class ReleasePromotion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"promotion-{uuid.uuid4().hex}")
    environment_resource_id: str
    environment_name: str
    artifact_id: str
    artifact_digest: str
    rollout: RolloutPlan = Field(default_factory=RolloutPlan)
    evidence_ids: tuple[str, ...] = ()
    verification_ids: tuple[str, ...] = ()
    approval_request_id: str | None = None
    deployment_action_intent_id: str | None = None
    rollback_action_intent_id: str | None = None
    status: PromotionStatus = PromotionStatus.PENDING
    blockers: tuple[str, ...] = ()
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    deployed_at: float | None = None
    rolled_back_at: float | None = None


class ReleaseRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"release-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    project_id: str | None = None
    name: str = Field(min_length=1, max_length=500)
    version: str = Field(min_length=1, max_length=200)
    build: ReleaseBuild
    policy: ReleasePolicy
    policy_fingerprint: str
    evidence_ids: tuple[str, ...] = ()
    rollback_release_id: str | None = None
    migration_evidence_id: str | None = None
    release_notes_artifact_id: str | None = None
    promotions: tuple[ReleasePromotion, ...] = ()
    status: ReleaseStatus = ReleaseStatus.DRAFT
    created_by: str
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def immutable_policy(self) -> "ReleaseRecord":
        if self.policy_fingerprint != self.policy.fingerprint():
            raise ValueError("release policy fingerprint mismatch")
        return self


class ReleaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    project_id: str | None = None
    name: str = Field(min_length=1, max_length=500)
    version: str = Field(min_length=1, max_length=200)
    artifact_id: str = Field(min_length=1)
    source_revision: str = Field(min_length=1, max_length=500)
    build_id: str = Field(min_length=1, max_length=500)
    sbom_artifact_id: str | None = None
    provenance_evidence_id: str | None = None
    signing_credential_ref: str | None = None
    evidence_ids: tuple[str, ...] = ()
    rollback_release_id: str | None = None
    migration_evidence_id: str | None = None
    release_notes_artifact_id: str | None = None
    policy: ReleasePolicy = Field(default_factory=ReleasePolicy)


class PromotionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    environment_resource_id: str = Field(min_length=1)
    environment_name: str = Field(min_length=1, max_length=200)
    rollout: RolloutPlan = Field(default_factory=RolloutPlan)
    evidence_ids: tuple[str, ...] = ()


class PromotionGateEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    release_id: str
    promotion_id: str
    satisfied: bool
    blockers: tuple[str, ...] = ()
    matched_evidence_ids: tuple[str, ...] = ()
    policy_fingerprint: str
    evaluated_at: float = Field(default_factory=time.time)


class PromotionQueueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider_binding_id: str = Field(min_length=1)
    deployment_action_id: str = Field(min_length=1)
    credential_ref: str | None = None


class RollbackQueueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider_binding_id: str = Field(min_length=1)
    rollback_action_id: str = Field(min_length=1)
    credential_ref: str | None = None


class ReleaseState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    releases: dict[str, ReleaseRecord] = Field(default_factory=dict)
