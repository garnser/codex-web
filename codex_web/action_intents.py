from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.action_providers import ActionDefinition, ActionRequest, ActionResult, ActionVerification
from codex_web.artifact_evidence import EvidenceRequirement


class ActionIntentStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"
    REQUIRES_RECONCILIATION = "requires_reconciliation"
    ROLLED_BACK = "rolled_back"


TERMINAL_ACTION_INTENT_STATUSES = frozenset(
    {
        ActionIntentStatus.SUCCEEDED,
        ActionIntentStatus.FAILED,
        ActionIntentStatus.CANCELLED,
        ActionIntentStatus.ROLLED_BACK,
    }
)


class ActionDecisionOutcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    NOT_EVALUATED = "not_evaluated"


class ActionDecisionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    decision_id: str | None = None
    outcome: ActionDecisionOutcome = ActionDecisionOutcome.NOT_EVALUATED
    source: str = Field(default="not-evaluated", min_length=1)
    reason: str | None = None
    evaluated_at: float | None = None


class ActionIntentRetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_attempts: int = Field(default=3, ge=1, le=100)
    backoff_seconds: float = Field(default=0.0, ge=0.0)


class ActionIntentLease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: str
    acquired_at: float
    expires_at: float
    renewed_at: float | None = None


class ActionIntentWorkItemSuccess(BaseModel):
    """Optional canonical state update applied only after intent verification."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    current_stage: str | None = None
    next_action: str | None = None
    next_owner: str | None = None
    note: str | None = None


class ActionIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"action-intent-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    project_id: str | None = None
    work_item_ref: str | None = None
    goal_id: str | None = None
    decision_id: str | None = None
    execution_id: str | None = None
    binding_id: str
    provider_type: str
    provider_instance: str
    action_id: str
    action_definition: ActionDefinition
    request: ActionRequest
    authority_decision: ActionDecisionSnapshot
    policy_decision: ActionDecisionSnapshot
    credential_ref: str | None = None
    resource_ids: tuple[str, ...] = ()
    idempotency_key: str
    provider_idempotency_supported: bool = False
    expected_evidence: tuple[EvidenceRequirement, ...] = ()
    verification_required: bool = False
    rollback_required: bool = False
    timeout_seconds: float
    retry_policy: ActionIntentRetryPolicy
    status: ActionIntentStatus = ActionIntentStatus.PENDING
    attempt: int = 0
    not_before: float | None = None
    lease: ActionIntentLease | None = None
    correlation_id: str
    causation_id: str | None = None
    requested_by: str
    created_at: float
    updated_at: float
    execution_started_at: float | None = None
    completed_at: float | None = None
    last_error: str | None = None
    last_receipt_id: str | None = None
    last_verification_id: str | None = None
    work_item_success: ActionIntentWorkItemSuccess | None = None

    @model_validator(mode="after")
    def normalize(self) -> "ActionIntent":
        self.resource_ids = tuple(dict.fromkeys(self.resource_ids))
        return self


class ActionIntentReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"action-receipt-{uuid.uuid4().hex}")
    intent_id: str
    attempt: int
    provider_type: str
    provider_instance: str
    action_id: str
    idempotency_key: str
    correlation_id: str
    provider_external_id: str | None = None
    result: ActionResult | None = None
    outcome: Literal["completed", "failed", "unknown", "callback"] = "completed"
    received_at: float = Field(default_factory=time.time)
    details: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class ActionIntentVerificationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"action-verification-{uuid.uuid4().hex}")
    intent_id: str
    provider_verification: ActionVerification | None = None
    evidence_satisfied: bool | None = None
    evidence_evaluation: dict[str, Any] | None = None
    verified: bool
    findings: tuple[str, ...] = ()
    verified_at: float = Field(default_factory=time.time)


class ActionInboxMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"action-inbox-{uuid.uuid4().hex}")
    provider_type: str
    provider_instance: str
    delivery_id: str
    intent_id: str | None = None
    idempotency_key: str | None = None
    provider_external_id: str | None = None
    event_type: str
    outcome: ActionIntentStatus | None = None
    correlation_id: str | None = None
    payload: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    received_at: float = Field(default_factory=time.time)
    processed_at: float | None = None
    duplicate: bool = False


class ActionIntentState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    intents: list[ActionIntent] = Field(default_factory=list)
    receipts: list[ActionIntentReceipt] = Field(default_factory=list)
    verifications: list[ActionIntentVerificationReceipt] = Field(default_factory=list)
    inbox: list[ActionInboxMessage] = Field(default_factory=list)


class ActionIntentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: str
    request: ActionRequest
    work_item_ref: str | None = None
    goal_id: str | None = None
    decision_id: str | None = None
    execution_id: str | None = None
    authority_decision: ActionDecisionSnapshot = Field(default_factory=ActionDecisionSnapshot)
    policy_decision: ActionDecisionSnapshot = Field(default_factory=ActionDecisionSnapshot)
    expected_evidence: tuple[EvidenceRequirement, ...] = ()
    verification_required: bool | None = None
    rollback_required: bool = False
    timeout_seconds: float | None = Field(default=None, gt=0.0)
    retry_policy: ActionIntentRetryPolicy = Field(default_factory=ActionIntentRetryPolicy)
    work_item_success: ActionIntentWorkItemSuccess | None = None


class ActionIntentClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    worker_id: str = Field(min_length=1)
    lease_seconds: int = Field(default=120, ge=10, le=3600)


class ActionIntentRenewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    worker_id: str = Field(min_length=1)
    lease_seconds: int = Field(default=120, ge=10, le=3600)


class ActionIntentRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str | None = None


class ActionIntentCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str | None = None


class ActionInboxCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider_type: str = Field(min_length=1)
    provider_instance: str = Field(min_length=1)
    delivery_id: str = Field(min_length=1)
    intent_id: str | None = None
    idempotency_key: str | None = None
    provider_external_id: str | None = None
    event_type: str = Field(min_length=1)
    outcome: ActionIntentStatus | None = None
    correlation_id: str | None = None
    payload: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class ActionIntentReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retry_if_idempotent: bool = True


class ActionIntentRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str | None = None
