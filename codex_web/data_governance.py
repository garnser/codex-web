from __future__ import annotations

import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec


DATA_GOVERNANCE_CONTRACT = ContractSpec("data-governance-state", "1.0", ("1.0",))


class DataClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"
    SECRET = "secret"


CLASSIFICATION_RANK: dict[DataClassification, int] = {
    DataClassification.PUBLIC: 0,
    DataClassification.INTERNAL: 1,
    DataClassification.CONFIDENTIAL: 2,
    DataClassification.RESTRICTED: 3,
    DataClassification.SECRET: 4,
}


class DataCategory(StrEnum):
    PROMPT = "prompt"
    THREAD = "thread"
    WORK_ITEM = "work_item"
    GOAL = "goal"
    DECISION = "decision"
    MEMORY = "memory"
    AUDIT = "audit"
    ARTIFACT = "artifact"
    EVIDENCE = "evidence"
    LOG = "log"
    CREDENTIAL = "credential"
    INTEGRATION_PAYLOAD = "integration_payload"
    CONFIGURATION = "configuration"
    DEFINITION = "definition"
    OTHER = "other"


class GovernedDataLifecycle(StrEnum):
    ACTIVE = "active"
    REDACTED = "redacted"
    ANONYMIZED = "anonymized"
    DELETED = "deleted"
    SUPERSEDED = "superseded"


class GovernanceAction(StrEnum):
    REDACT = "redact"
    ANONYMIZE = "anonymize"
    DELETE = "delete"


class GovernanceRequestStatus(StrEnum):
    PENDING = "pending"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class GovernedDataRecord(BaseModel):
    """Canonical governance metadata for one durable data object.

    This record contains ownership/classification/retention metadata only. It
    never stores the governed object's sensitive payload.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"data-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    project_id: str | None = None
    object_type: str = Field(min_length=1)
    object_id: str = Field(min_length=1)
    category: DataCategory
    requested_classification: DataClassification
    classification: DataClassification
    retention_policy_ref: str | None = None
    retention_expires_at: float | None = None
    retention_action: GovernanceAction = GovernanceAction.REDACT
    residency_tags: tuple[str, ...] = ()
    source_record_ids: tuple[str, ...] = ()
    deny_model_context: bool = False
    legal_hold_at: float | None = None
    legal_hold_by: str | None = None
    legal_hold_reason: str | None = None
    lifecycle: GovernedDataLifecycle = GovernedDataLifecycle.ACTIVE
    action_completed_at: float | None = None
    superseded_by_record_id: str | None = None
    created_by: str = Field(min_length=1)
    create_reason: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def normalize(self) -> "GovernedDataRecord":
        self.residency_tags = tuple(sorted({value for value in self.residency_tags if value}))
        self.source_record_ids = tuple(dict.fromkeys(self.source_record_ids))
        if self.classification_rank(self.classification) < self.classification_rank(
            self.requested_classification
        ):
            raise ValueError("effective classification cannot be below requested classification")
        if self.legal_hold_at is None:
            self.legal_hold_by = None
            self.legal_hold_reason = None
        return self

    @staticmethod
    def classification_rank(value: DataClassification) -> int:
        return CLASSIFICATION_RANK[value]


class GovernedDataCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    project_id: str | None = None
    object_type: str = Field(min_length=1)
    object_id: str = Field(min_length=1)
    category: DataCategory
    classification: DataClassification = DataClassification.INTERNAL
    retention_policy_ref: str | None = None
    retention_expires_at: float | None = None
    retention_action: GovernanceAction = GovernanceAction.REDACT
    residency_tags: tuple[str, ...] = ()
    source_record_ids: tuple[str, ...] = ()
    deny_model_context: bool = False
    reason: str | None = None


class LegalHoldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=1)


class GovernanceActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    record_id: str = Field(min_length=1)
    action: GovernanceAction
    reason: str = Field(min_length=1)


class GovernedDeletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"govreq-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    record_id: str = Field(min_length=1)
    action: GovernanceAction
    status: GovernanceRequestStatus = GovernanceRequestStatus.PENDING
    requested_by: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    requested_at: float = Field(default_factory=time.time)
    blocked_reason: str | None = None
    completed_by: str | None = None
    completed_at: float | None = None
    adapter_receipt_ref: str | None = None


class GovernanceAuditEvent(BaseModel):
    """Metadata-only governance audit event.

    No governed content, prompt text, secret value, or deleted payload may be
    copied into this event.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"govevt-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    outcome: str = Field(min_length=1)
    record_id: str | None = None
    request_id: str | None = None
    object_type: str | None = None
    object_id: str | None = None
    classification: DataClassification | None = None
    reason_code: str | None = None
    occurred_at: float = Field(default_factory=time.time)


class ContextFilterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_ids: tuple[str, ...]
    max_classification: DataClassification = DataClassification.CONFIDENTIAL


class ContextFilterDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: str
    allowed: bool
    classification: DataClassification
    reason: str


class ContextFilterResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_record_ids: tuple[str, ...]
    denied_record_ids: tuple[str, ...]
    decisions: tuple[ContextFilterDecision, ...]


class ExportAuthorizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_ids: tuple[str, ...]
    max_classification: DataClassification = DataClassification.RESTRICTED


class ExportManifestItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: str
    object_type: str
    object_id: str
    classification: DataClassification
    project_id: str | None = None
    residency_tags: tuple[str, ...] = ()


class ExportAuthorizationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[ExportManifestItem, ...]
    denied: tuple[ContextFilterDecision, ...]


class RetentionSweepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    now: float | None = None
    execute: bool = False


class RetentionSweepResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    due_record_ids: tuple[str, ...]
    held_record_ids: tuple[str, ...]
    request_ids: tuple[str, ...]
    completed_request_ids: tuple[str, ...]


class DataGovernanceState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = DATA_GOVERNANCE_CONTRACT.current
    records: list[GovernedDataRecord] = Field(default_factory=list)
    requests: list[GovernedDeletionRequest] = Field(default_factory=list)
    events: list[GovernanceAuditEvent] = Field(default_factory=list)
