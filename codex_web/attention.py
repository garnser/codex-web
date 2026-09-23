from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec


ATTENTION_STATE_CONTRACT = ContractSpec("attention-state", "1.2", ("1.0", "1.1", "1.2"))


class AttentionSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"


class AttentionStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    SNOOZED = "snoozed"
    RESOLVED = "resolved"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    ESCALATED = "escalated"


TERMINAL_ATTENTION_STATUSES = frozenset({
    AttentionStatus.RESOLVED,
    AttentionStatus.EXPIRED,
    AttentionStatus.SUPERSEDED,
})


class AttentionSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    object_type: str = Field(min_length=1, max_length=200)
    object_id: str = Field(min_length=1, max_length=500)
    event_id: str | None = Field(default=None, max_length=500)


class EscalationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    escalate_at: float | None = None
    mandatory: bool = False
    recipient_identity_ids: tuple[str, ...] = ()
    recipient_team_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def normalize(self) -> "EscalationPolicy":
        object.__setattr__(
            self,
            "recipient_identity_ids",
            tuple(sorted({item.strip() for item in self.recipient_identity_ids if item.strip()})),
        )
        object.__setattr__(
            self,
            "recipient_team_ids",
            tuple(sorted({item.strip() for item in self.recipient_team_ids if item.strip()})),
        )
        return self


class AttentionItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    organization_id: str = Field(min_length=1, max_length=200)
    workspace_id: str = Field(min_length=1, max_length=200)
    project_id: str | None = Field(default=None, max_length=200)
    type: str = Field(min_length=1, max_length=200)
    severity: AttentionSeverity = AttentionSeverity.WARNING
    source: AttentionSource
    reason: str = Field(min_length=1, max_length=4000)
    dedupe_key: str = Field(min_length=1, max_length=500)
    owner_identity_id: str | None = Field(default=None, max_length=500)
    recipient_identity_ids: tuple[str, ...] = ()
    recipient_team_ids: tuple[str, ...] = ()
    due_at: float | None = None
    expires_at: float | None = None
    deep_link: str | None = Field(default=None, max_length=2000)
    requesting_agent_profile_id: str | None = Field(default=None, max_length=500)
    requesting_agent_team_id: str | None = Field(default=None, max_length=500)
    evidence_ids: tuple[str, ...] = ()
    diagnostic_refs: tuple[str, ...] = ()
    escalation: EscalationPolicy | None = None

    @model_validator(mode="after")
    def normalize(self) -> "AttentionItemCreate":
        self.recipient_identity_ids = tuple(
            sorted({item.strip() for item in self.recipient_identity_ids if item.strip()})
        )
        self.recipient_team_ids = tuple(
            sorted({item.strip() for item in self.recipient_team_ids if item.strip()})
        )
        self.evidence_ids = tuple(
            dict.fromkeys(item.strip() for item in self.evidence_ids if item.strip())
        )
        self.diagnostic_refs = tuple(
            dict.fromkeys(item.strip() for item in self.diagnostic_refs if item.strip())
        )
        return self


class AttentionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"attention-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    project_id: str | None = None
    type: str
    severity: AttentionSeverity
    source: AttentionSource
    reason: str
    dedupe_key: str
    owner_identity_id: str | None = None
    recipient_identity_ids: tuple[str, ...] = ()
    recipient_team_ids: tuple[str, ...] = ()
    due_at: float | None = None
    expires_at: float | None = None
    deep_link: str | None = None
    requesting_agent_profile_id: str | None = None
    requesting_agent_team_id: str | None = None
    evidence_ids: tuple[str, ...] = ()
    diagnostic_refs: tuple[str, ...] = ()
    escalation: EscalationPolicy | None = None
    escalation_schedule_id: str | None = None
    status: AttentionStatus = AttentionStatus.OPEN
    acknowledged_by_identity_id: str | None = None
    acknowledged_at: float | None = None
    snoozed_until: float | None = None
    resolved_by_identity_id: str | None = None
    resolved_at: float | None = None
    resolution_reason: str | None = None
    escalation_count: int = Field(default=0, ge=0)
    revision: int = Field(default=1, ge=1)
    created_at: float = Field(default_factory=time.time)
    created_by: str = "system"
    updated_at: float = Field(default_factory=time.time)
    updated_by: str = "system"

    @classmethod
    def from_create(
        cls,
        payload: AttentionItemCreate,
        *,
        actor_id: str,
        now: float | None = None,
    ) -> "AttentionItem":
        timestamp = time.time() if now is None else float(now)
        return cls(
            organization_id=payload.organization_id,
            workspace_id=payload.workspace_id,
            project_id=payload.project_id,
            type=payload.type,
            severity=payload.severity,
            source=payload.source,
            reason=payload.reason,
            dedupe_key=payload.dedupe_key,
            owner_identity_id=payload.owner_identity_id,
            recipient_identity_ids=payload.recipient_identity_ids,
            recipient_team_ids=payload.recipient_team_ids,
            due_at=payload.due_at,
            expires_at=payload.expires_at,
            deep_link=payload.deep_link,
            requesting_agent_profile_id=payload.requesting_agent_profile_id,
            requesting_agent_team_id=payload.requesting_agent_team_id,
            evidence_ids=payload.evidence_ids,
            diagnostic_refs=payload.diagnostic_refs,
            escalation=payload.escalation,
            created_at=timestamp,
            created_by=actor_id,
            updated_at=timestamp,
            updated_by=actor_id,
        )


class AttentionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = ATTENTION_STATE_CONTRACT.current
    items: dict[str, AttentionItem] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        ATTENTION_STATE_CONTRACT.require(self.schema_version)
