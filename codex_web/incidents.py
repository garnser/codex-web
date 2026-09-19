from __future__ import annotations

import hashlib
import json
import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IncidentSeverity(StrEnum):
    SEV1 = "sev1"
    SEV2 = "sev2"
    SEV3 = "sev3"
    SEV4 = "sev4"


class IncidentStatus(StrEnum):
    DETECTED = "detected"
    TRIAGED = "triaged"
    ACTIVE = "active"
    CONTAINED = "contained"
    MONITORING = "monitoring"
    RESOLVED = "resolved"
    POSTMORTEM = "postmortem"
    CLOSED = "closed"


class IncidentTimelineKind(StrEnum):
    DETECTION = "detection"
    TRIAGE = "triage"
    COMMAND = "command"
    HANDOFF = "handoff"
    CONTAINMENT = "containment"
    RECOVERY = "recovery"
    EVIDENCE = "evidence"
    STATUS = "status"
    NOTE = "note"
    POSTMORTEM = "postmortem"


class IncidentTimelineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"incident-event-{uuid.uuid4().hex}")
    kind: IncidentTimelineKind
    summary: str = Field(min_length=1, max_length=4000)
    actor_id: str | None = None
    action_intent_id: str | None = None
    evidence_ids: tuple[str, ...] = ()
    resource_ids: tuple[str, ...] = ()
    occurred_at: float = Field(default_factory=time.time)


class IncidentReasoningBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_reasoning_attempts: int = Field(default=2, ge=0, le=10)
    max_model_tokens: int = Field(default=16000, ge=0)
    max_model_cost_usd: float = Field(default=2.0, ge=0.0)
    max_agent_handoffs: int = Field(default=3, ge=0, le=20)


class IncidentPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resolution_evidence_types: tuple[str, ...] = (
        "deployment_verification",
        "runtime_result",
    )
    require_independent_resolution_verification_for: tuple[IncidentSeverity, ...] = (
        IncidentSeverity.SEV1,
    )
    reasoning_budgets: dict[IncidentSeverity, IncidentReasoningBudget] = Field(
        default_factory=lambda: {
            IncidentSeverity.SEV1: IncidentReasoningBudget(
                max_reasoning_attempts=3,
                max_model_tokens=24000,
                max_model_cost_usd=5.0,
                max_agent_handoffs=4,
            ),
            IncidentSeverity.SEV2: IncidentReasoningBudget(),
            IncidentSeverity.SEV3: IncidentReasoningBudget(
                max_reasoning_attempts=1,
                max_model_tokens=8000,
                max_model_cost_usd=1.0,
                max_agent_handoffs=2,
            ),
            IncidentSeverity.SEV4: IncidentReasoningBudget(
                max_reasoning_attempts=0,
                max_model_tokens=0,
                max_model_cost_usd=0,
                max_agent_handoffs=0,
            ),
        }
    )


class IncidentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"incident-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    project_id: str | None = None
    title: str = Field(min_length=1, max_length=1000)
    severity: IncidentSeverity
    status: IncidentStatus = IncidentStatus.DETECTED
    dedupe_key: str = Field(min_length=1, max_length=500)
    detected_source: str = Field(min_length=1, max_length=500)
    detected_event_id: str | None = None
    commander_identity_id: str | None = None
    owner_identity_ids: tuple[str, ...] = ()
    affected_resource_ids: tuple[str, ...] = ()
    impact_summary: str | None = Field(default=None, max_length=8000)
    customer_effect: str | None = Field(default=None, max_length=8000)
    goal_ids: tuple[str, ...] = ()
    decision_ids: tuple[str, ...] = ()
    work_item_refs: tuple[str, ...] = ()
    release_ids: tuple[str, ...] = ()
    action_intent_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    runbook_knowledge_ids: tuple[str, ...] = ()
    attention_item_id: str | None = None
    occurrence_count: int = Field(default=1, ge=1)
    policy: IncidentPolicy = Field(default_factory=IncidentPolicy)
    timeline: tuple[IncidentTimelineEntry, ...] = ()
    postmortem_id: str | None = None
    detected_at: float = Field(default_factory=time.time)
    resolved_at: float | None = None
    closed_at: float | None = None
    updated_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def normalize(self) -> "IncidentRecord":
        for name in (
            "owner_identity_ids",
            "affected_resource_ids",
            "goal_ids",
            "decision_ids",
            "work_item_refs",
            "release_ids",
            "action_intent_ids",
            "evidence_ids",
            "runbook_knowledge_ids",
        ):
            object.__setattr__(
                self,
                name,
                tuple(dict.fromkeys(getattr(self, name))),
            )
        return self


class IncidentDetect(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    project_id: str | None = None
    title: str = Field(min_length=1, max_length=1000)
    severity: IncidentSeverity
    source: str = Field(min_length=1, max_length=500)
    source_event_id: str | None = None
    dedupe_key: str | None = Field(default=None, max_length=500)
    affected_resource_ids: tuple[str, ...] = ()
    impact_summary: str | None = Field(default=None, max_length=8000)
    customer_effect: str | None = Field(default=None, max_length=8000)
    owner_identity_ids: tuple[str, ...] = ()
    recipient_team_ids: tuple[str, ...] = ()
    goal_ids: tuple[str, ...] = ()
    decision_ids: tuple[str, ...] = ()
    work_item_refs: tuple[str, ...] = ()
    release_ids: tuple[str, ...] = ()
    runbook_knowledge_ids: tuple[str, ...] = ()
    policy: IncidentPolicy = Field(default_factory=IncidentPolicy)

    def fingerprint(self, organization_id: str, workspace_id: str) -> str:
        if self.dedupe_key:
            return self.dedupe_key
        payload = {
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "project_id": self.project_id,
            "source": self.source,
            "title": self.title,
            "affected_resource_ids": sorted(set(self.affected_resource_ids)),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class IncidentTriage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    severity: IncidentSeverity | None = None
    impact_summary: str | None = Field(default=None, max_length=8000)
    customer_effect: str | None = Field(default=None, max_length=8000)
    commander_identity_id: str | None = None
    owner_identity_ids: tuple[str, ...] | None = None


class IncidentHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    commander_identity_id: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=4000)


class IncidentActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider_binding_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    resource_ids: tuple[str, ...] = ()
    credential_ref: str | None = None
    parameters: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=4000)
    recovery: bool = False


class IncidentEvidenceAttach(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_ids: tuple[str, ...]


class IncidentResolve(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    evidence_ids: tuple[str, ...]
    summary: str = Field(min_length=1, max_length=8000)


class IncidentPostmortemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    summary: str = Field(min_length=1, max_length=8000)
    contributing_factors: tuple[str, ...] = ()
    corrective_work_item_refs: tuple[str, ...] = ()
    corrective_goal_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    publish_to_memory: bool = True


class IncidentPostmortem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"postmortem-{uuid.uuid4().hex}")
    incident_id: str
    summary: str
    contributing_factors: tuple[str, ...] = ()
    corrective_work_item_refs: tuple[str, ...] = ()
    corrective_goal_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    knowledge_id: str | None = None
    created_by: str
    created_at: float = Field(default_factory=time.time)


class IncidentState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    incidents: dict[str, IncidentRecord] = Field(default_factory=dict)
    postmortems: dict[str, IncidentPostmortem] = Field(default_factory=dict)
