from __future__ import annotations

import hashlib
import json
import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec
from codex_web.identity import AuthenticationAssurance, MembershipRole, PrincipalKind


APPROVAL_REQUEST_STATE_CONTRACT = ContractSpec(
    "approval-request-state",
    "1.0",
    ("1.0",),
)


class ApprovalRequestStatus(StrEnum):
    PENDING = "pending"
    PARTIALLY_APPROVED = "partially_approved"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    CONSUMED = "consumed"
    INVALIDATED = "invalidated"


TERMINAL_APPROVAL_REQUEST_STATUSES = frozenset(
    {
        ApprovalRequestStatus.REJECTED,
        ApprovalRequestStatus.EXPIRED,
        ApprovalRequestStatus.CANCELLED,
        ApprovalRequestStatus.SUPERSEDED,
        ApprovalRequestStatus.CONSUMED,
        ApprovalRequestStatus.INVALIDATED,
    }
)


class ApprovalDecisionOutcome(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class ApprovalTarget(BaseModel):
    """Exact operation/object binding reviewed by approvers."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    operation: str = Field(min_length=1, max_length=200)
    object_type: str = Field(min_length=1, max_length=200)
    object_id: str = Field(min_length=1, max_length=500)
    target_version: str | None = Field(default=None, max_length=500)
    target_digest: str | None = Field(default=None, max_length=500)
    resource_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def normalize(self) -> "ApprovalTarget":
        object.__setattr__(
            self,
            "resource_ids",
            tuple(sorted({item.strip() for item in self.resource_ids if item.strip()})),
        )
        return self

    def fingerprint(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class ApprovalRequirement(BaseModel):
    """Deterministic approver eligibility and quorum policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    quorum: int = Field(default=1, ge=1, le=20)
    required_assurance: AuthenticationAssurance = AuthenticationAssurance.MFA
    membership_roles: tuple[MembershipRole, ...] = (
        MembershipRole.OWNER,
        MembershipRole.ADMIN,
        MembershipRole.APPROVER,
    )
    team_ids: tuple[str, ...] = ()
    authority_role_ids: tuple[str, ...] = ()
    distinct_humans: bool = True
    allow_self_approval: bool = False

    @model_validator(mode="after")
    def normalize(self) -> "ApprovalRequirement":
        object.__setattr__(
            self,
            "membership_roles",
            tuple(dict.fromkeys(self.membership_roles)),
        )
        object.__setattr__(
            self,
            "team_ids",
            tuple(sorted({item.strip() for item in self.team_ids if item.strip()})),
        )
        object.__setattr__(
            self,
            "authority_role_ids",
            tuple(
                sorted(
                    {
                        item.strip()
                        for item in self.authority_role_ids
                        if item.strip()
                    }
                )
            ),
        )
        return self


class ApprovalRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target: ApprovalTarget
    reason: str = Field(min_length=1, max_length=4000)
    policy_source: str | None = Field(default=None, max_length=500)
    authority_source: str | None = Field(default=None, max_length=500)
    requirement: ApprovalRequirement = Field(default_factory=ApprovalRequirement)
    expires_at: float | None = None
    evidence_refs: tuple[str, ...] = ()
    audit_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_expiry(self) -> "ApprovalRequestCreate":
        if self.expires_at is not None and self.expires_at <= 0:
            raise ValueError("approval expiry must be a positive timestamp")
        return self


class ApprovalDecisionSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    outcome: ApprovalDecisionOutcome
    reason: str | None = Field(default=None, max_length=4000)
    idempotency_key: str = Field(min_length=1, max_length=500)


class ApprovalDecisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"approval-decision-{uuid.uuid4().hex}")
    request_id: str
    identity_id: str
    principal_kind: PrincipalKind
    outcome: ApprovalDecisionOutcome
    reason: str | None = None
    idempotency_key: str
    assurance: AuthenticationAssurance
    session_id: str | None = None
    membership_roles: tuple[MembershipRole, ...] = ()
    team_ids: tuple[str, ...] = ()
    authority_role_ids: tuple[str, ...] = ()
    decided_at: float = Field(default_factory=time.time)


class ApprovalConsumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target: ApprovalTarget
    idempotency_key: str = Field(min_length=1, max_length=500)
    resulting_operation_reference: str = Field(min_length=1, max_length=1000)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"approval-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    requester_identity_id: str
    requester_principal_kind: PrincipalKind
    requester_session_id: str | None = None
    target: ApprovalTarget
    target_fingerprint: str
    reason: str
    policy_source: str | None = None
    authority_source: str | None = None
    requirement: ApprovalRequirement
    status: ApprovalRequestStatus = ApprovalRequestStatus.PENDING
    decisions: tuple[ApprovalDecisionRecord, ...] = ()
    expires_at: float | None = None
    expiry_schedule_id: str | None = None
    evidence_refs: tuple[str, ...] = ()
    audit_refs: tuple[str, ...] = ()
    resulting_operation_reference: str | None = None
    consumption_idempotency_key: str | None = None
    consumed_by_identity_id: str | None = None
    consumed_at: float | None = None
    cancelled_by_identity_id: str | None = None
    cancelled_at: float | None = None
    superseded_by_request_id: str | None = None
    invalidation_reason: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    revision: int = Field(default=1, ge=1)

    @classmethod
    def from_create(
        cls,
        payload: ApprovalRequestCreate,
        *,
        organization_id: str,
        workspace_id: str,
        requester_identity_id: str,
        requester_principal_kind: PrincipalKind,
        requester_session_id: str | None,
        now: float,
        request_id: str | None = None,
    ) -> "ApprovalRequest":
        return cls(
            id=request_id or f"approval-{uuid.uuid4().hex}",
            organization_id=organization_id,
            workspace_id=workspace_id,
            requester_identity_id=requester_identity_id,
            requester_principal_kind=requester_principal_kind,
            requester_session_id=requester_session_id,
            target=payload.target,
            target_fingerprint=payload.target.fingerprint(),
            reason=payload.reason,
            policy_source=payload.policy_source,
            authority_source=payload.authority_source,
            requirement=payload.requirement,
            expires_at=payload.expires_at,
            evidence_refs=tuple(dict.fromkeys(payload.evidence_refs)),
            audit_refs=tuple(dict.fromkeys(payload.audit_refs)),
            created_at=now,
            updated_at=now,
        )


class ApprovalRequestState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = APPROVAL_REQUEST_STATE_CONTRACT.current
    requests: dict[str, ApprovalRequest] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        APPROVAL_REQUEST_STATE_CONTRACT.require(self.schema_version)
