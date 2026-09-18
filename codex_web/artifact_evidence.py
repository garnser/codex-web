from __future__ import annotations

import hashlib
import time
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ArtifactType(StrEnum):
    COMMIT = "commit"
    BRANCH = "branch"
    PULL_REQUEST = "pull_request"
    PATCH = "patch"
    REPORT = "report"
    SCREENSHOT = "screenshot"
    BUILD = "build"
    PACKAGE = "package"
    DEPLOYMENT = "deployment"
    LOG = "log"
    GENERATED_FILE = "generated_file"


class ArtifactLifecycle(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"


class EvidenceType(StrEnum):
    TEST_RESULT = "test_result"
    CI_CHECK = "ci_check"
    REVIEW = "review"
    SECURITY_SCAN = "security_scan"
    DEPLOYMENT_VERIFICATION = "deployment_verification"
    POLICY_EVALUATION = "policy_evaluation"
    APPROVAL_RECORD = "approval_record"
    ARTIFACT_VERIFICATION = "artifact_verification"


class EvidenceLifecycle(StrEnum):
    VALID = "valid"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"


class EvidenceResult(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    INFO = "info"


class VerificationResult(StrEnum):
    VERIFIED = "verified"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"
    INVALIDATED = "invalidated"


class ArtifactDigest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    algorithm: Literal["sha256"] = "sha256"
    value: str = Field(pattern=r"^[0-9a-f]{64}$")


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"artifact-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    project_id: str | None = None
    work_item_ref: str | None = None
    execution_id: str | None = None
    execution_workspace_id: str | None = None
    resource_ids: tuple[str, ...] = ()
    artifact_type: ArtifactType
    name: str = Field(min_length=1)
    producer_identity_id: str
    provider: str | None = None
    source: str | None = None
    external_id: str | None = None
    external_url: str | None = None
    revision: str | None = None
    digest: ArtifactDigest | None = None
    lifecycle: ArtifactLifecycle = ArtifactLifecycle.ACTIVE
    supersedes_id: str | None = None
    superseded_by_id: str | None = None
    invalidated_at: float | None = None
    invalidation_reason: str | None = None
    retention_expires_at: float | None = None
    produced_at: float = Field(default_factory=time.time)
    created_at: float = Field(default_factory=time.time)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize(self) -> "Artifact":
        self.resource_ids = tuple(dict.fromkeys(self.resource_ids))
        return self


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"evidence-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    project_id: str | None = None
    work_item_ref: str | None = None
    execution_id: str | None = None
    execution_workspace_id: str | None = None
    evidence_type: EvidenceType
    artifact_ids: tuple[str, ...] = ()
    producer_identity_id: str
    provider: str | None = None
    source: str | None = None
    external_id: str | None = None
    deep_link: str | None = None
    result: EvidenceResult
    summary: str | None = None
    digest: ArtifactDigest | None = None
    lifecycle: EvidenceLifecycle = EvidenceLifecycle.VALID
    observed_at: float = Field(default_factory=time.time)
    created_at: float = Field(default_factory=time.time)
    invalidated_at: float | None = None
    invalidation_reason: str | None = None
    retention_expires_at: float | None = None
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize(self) -> "Evidence":
        self.artifact_ids = tuple(dict.fromkeys(self.artifact_ids))
        return self


class Verification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"verification-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    work_item_ref: str | None = None
    execution_id: str | None = None
    artifact_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    verifier_identity_id: str
    independent: bool
    method: str = Field(min_length=1)
    result: VerificationResult
    findings: tuple[str, ...] = ()
    provider: str | None = None
    source: str | None = None
    deep_link: str | None = None
    verified_at: float = Field(default_factory=time.time)
    created_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def normalize(self) -> "Verification":
        self.artifact_ids = tuple(dict.fromkeys(self.artifact_ids))
        self.evidence_ids = tuple(dict.fromkeys(self.evidence_ids))
        if not self.artifact_ids and not self.evidence_ids:
            raise ValueError("verification must reference artifact or evidence")
        return self


class EvidenceRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(min_length=1)
    evidence_type: EvidenceType
    artifact_type: ArtifactType | None = None
    min_count: int = Field(default=1, ge=1)
    accepted_results: tuple[EvidenceResult, ...] = (EvidenceResult.PASS,)
    independent_verification: bool = False
    max_age_seconds: float | None = Field(default=None, gt=0.0)
    provider: str | None = None

    @model_validator(mode="after")
    def normalize(self) -> "EvidenceRequirement":
        if not self.accepted_results:
            raise ValueError("evidence requirement requires at least one accepted result")
        return self


class EvidenceRequirementOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_id: str
    satisfied: bool
    matching_evidence_ids: tuple[str, ...] = ()
    verification_ids: tuple[str, ...] = ()
    reason: str | None = None


class EvidenceGateEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    work_item_ref: str
    satisfied: bool
    evaluated_at: float
    outcomes: tuple[EvidenceRequirementOutcome, ...]


class ArtifactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    project_id: str | None = None
    work_item_ref: str | None = None
    execution_id: str | None = None
    execution_workspace_id: str | None = None
    resource_ids: tuple[str, ...] = ()
    artifact_type: ArtifactType
    name: str = Field(min_length=1)
    provider: str | None = None
    source: str | None = None
    external_id: str | None = None
    external_url: str | None = None
    revision: str | None = None
    digest: ArtifactDigest | None = None
    supersedes_artifact_id: str | None = None
    retention_expires_at: float | None = None
    produced_at: float | None = None
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class EvidenceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    project_id: str | None = None
    work_item_ref: str | None = None
    execution_id: str | None = None
    execution_workspace_id: str | None = None
    evidence_type: EvidenceType
    artifact_ids: tuple[str, ...] = ()
    provider: str | None = None
    source: str | None = None
    external_id: str | None = None
    deep_link: str | None = None
    result: EvidenceResult
    summary: str | None = None
    digest: ArtifactDigest | None = None
    observed_at: float | None = None
    retention_expires_at: float | None = None
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class VerificationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    work_item_ref: str | None = None
    execution_id: str | None = None
    artifact_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    method: str = Field(min_length=1)
    result: VerificationResult
    findings: tuple[str, ...] = ()
    provider: str | None = None
    source: str | None = None
    deep_link: str | None = None


class InvalidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=1)


class EvidenceEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_item_ref: str = Field(min_length=1)
    requirements: tuple[EvidenceRequirement, ...] = ()


class ArtifactEvidenceState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    artifacts: list[Artifact] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    verifications: list[Verification] = Field(default_factory=list)


def sha256_digest(data: bytes) -> ArtifactDigest:
    return ArtifactDigest(value=hashlib.sha256(data).hexdigest())
