from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec
from codex_web.definitions import DefinitionReference


EXECUTIVE_ROLE_CATALOG_KIND = "executive-role-catalog"
EXECUTIVE_ROLE_CATALOG_ID = "executive.roles.default"
EXECUTIVE_ROLE_CATALOG_SCHEMA_VERSION = "1.0"

EXECUTIVE_ACTIVATION_CONTRACT = ContractSpec(
    "executive-activation-state",
    "1.0",
    ("1.0",),
)

MAX_EXECUTIVE_ROLES_PER_ACTIVATION = 5
MAX_EXECUTIVE_CONTEXT_REFS = 30
MAX_EXECUTIVE_PROPOSALS = 20


class ExecutiveRoleLifecycle(StrEnum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"


class ExecutiveObjectType(StrEnum):
    GOAL = "goal"
    DECISION = "decision"
    WORK_ITEM = "work_item"
    WORK_GRAPH = "work_graph"
    METRIC = "metric"
    EVIDENCE = "evidence"
    ATTENTION = "attention"
    APPROVAL = "approval"
    EVENT = "event"


class ExecutiveProposalKind(StrEnum):
    GOAL = "goal"
    DECISION = "decision"
    WORK = "work"
    ESCALATION = "escalation"


class ExecutiveTriggerKind(StrEnum):
    REQUEST = "request"
    EVENT = "event"
    SCHEDULE = "schedule"
    REVIEW = "review"


class ExecutiveActivationStatus(StrEnum):
    PLANNED = "planned"
    CONSULTING = "consulting"
    COMPLETED = "completed"
    ESCALATED = "escalated"
    FAILED = "failed"


class ExecutiveProposalStatus(StrEnum):
    PROPOSED = "proposed"
    MATERIALIZED = "materialized"
    REJECTED = "rejected"


class ExecutiveRoleAuthorityContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_proposal_kinds: tuple[ExecutiveProposalKind, ...] = (
        ExecutiveProposalKind.GOAL,
        ExecutiveProposalKind.DECISION,
        ExecutiveProposalKind.ESCALATION,
    )
    can_materialize: bool = False
    external_side_effects: Literal[False] = False
    required_materialization_capabilities: dict[ExecutiveProposalKind, str] = Field(
        default_factory=lambda: {
            ExecutiveProposalKind.GOAL: "executive.goal.materialize",
            ExecutiveProposalKind.DECISION: "executive.decision.materialize",
            ExecutiveProposalKind.WORK: "executive.work.materialize",
        }
    )

    @model_validator(mode="after")
    def normalize(self) -> "ExecutiveRoleAuthorityContract":
        object.__setattr__(
            self,
            "allowed_proposal_kinds",
            tuple(dict.fromkeys(self.allowed_proposal_kinds)),
        )
        return self


class ExecutiveRoleDefinition(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    lifecycle: ExecutiveRoleLifecycle = ExecutiveRoleLifecycle.ACTIVE
    responsibilities: tuple[str, ...]
    observable_information: tuple[ExecutiveObjectType, ...]
    event_subscriptions: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    consultation_roles: tuple[str, ...] = ()
    authority: ExecutiveRoleAuthorityContract = Field(
        default_factory=ExecutiveRoleAuthorityContract
    )
    max_context_items: int = Field(default=20, ge=1, le=100)
    max_consultations: int = Field(default=3, ge=0, le=MAX_EXECUTIVE_ROLES_PER_ACTIVATION)
    instructions: str = Field(min_length=1)

    @model_validator(mode="after")
    def normalize(self) -> "ExecutiveRoleDefinition":
        for field_name in (
            "responsibilities",
            "event_subscriptions",
            "keywords",
            "consultation_roles",
        ):
            values = tuple(
                dict.fromkeys(
                    value.strip()
                    for value in getattr(self, field_name)
                    if value.strip()
                )
            )
            object.__setattr__(self, field_name, values)
        object.__setattr__(
            self,
            "observable_information",
            tuple(dict.fromkeys(self.observable_information)),
        )
        if not self.responsibilities:
            raise ValueError("Executive role requires at least one responsibility")
        return self


class ExecutiveRoleCatalogDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    roles: tuple[ExecutiveRoleDefinition, ...]
    fallback_role_id: str = "chief-of-staff"
    max_roles_per_activation: int = Field(
        default=3,
        ge=1,
        le=MAX_EXECUTIVE_ROLES_PER_ACTIVATION,
    )

    @model_validator(mode="after")
    def validate_catalog(self) -> "ExecutiveRoleCatalogDefinition":
        ids = [role.id for role in self.roles]
        if len(ids) != len(set(ids)):
            raise ValueError("Executive role ids must be unique")
        known = set(ids)
        required = {"chief-of-staff", "cto", "cpo", "coo", "cfo"}
        missing = required - known
        if missing:
            raise ValueError(
                "Executive role catalog missing core roles: "
                + ", ".join(sorted(missing))
            )
        if self.fallback_role_id not in known:
            raise ValueError("Executive fallback role is not defined")
        disabled = {
            role.id
            for role in self.roles
            if role.lifecycle == ExecutiveRoleLifecycle.DISABLED
        }
        if self.fallback_role_id in disabled:
            raise ValueError("Executive fallback role cannot be disabled")
        for role in self.roles:
            unknown = set(role.consultation_roles) - known
            if unknown:
                raise ValueError(
                    f"Executive role {role.id} consults unknown roles: "
                    + ", ".join(sorted(unknown))
                )
        return self

    @property
    def role_map(self) -> dict[str, ExecutiveRoleDefinition]:
        return {role.id: role for role in self.roles}


def validate_executive_role_catalog(payload: dict[str, Any]) -> dict[str, Any]:
    return ExecutiveRoleCatalogDefinition.model_validate(payload).model_dump(
        mode="json"
    )


class ExecutiveReasoningBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_input_tokens: int = Field(default=40000, ge=1000, le=500000)
    max_output_tokens: int = Field(default=12000, ge=256, le=100000)
    max_model_calls: int = Field(default=5, ge=1, le=20)
    max_cost_usd: float = Field(default=5.0, gt=0.0, le=1000.0)


class ExecutiveActivationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    subject: str = Field(min_length=1, max_length=500)
    request: str = Field(min_length=1, max_length=50000)
    trigger_kind: ExecutiveTriggerKind = ExecutiveTriggerKind.REQUEST
    trigger_ref: str | None = Field(default=None, max_length=1000)
    event_type: str | None = Field(default=None, max_length=300)
    project_id: str | None = Field(default=None, max_length=500)
    goal_ids: tuple[str, ...] = ()
    decision_ids: tuple[str, ...] = ()
    work_item_refs: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    requested_role_ids: tuple[str, ...] = ()
    max_roles: int | None = Field(
        default=None,
        ge=1,
        le=MAX_EXECUTIVE_ROLES_PER_ACTIVATION,
    )
    budget: ExecutiveReasoningBudget = Field(
        default_factory=ExecutiveReasoningBudget
    )

    @model_validator(mode="after")
    def normalize(self) -> "ExecutiveActivationCreate":
        for field_name in (
            "goal_ids",
            "decision_ids",
            "work_item_refs",
            "evidence_ids",
            "requested_role_ids",
        ):
            values = tuple(
                dict.fromkeys(
                    value.strip()
                    for value in getattr(self, field_name)
                    if value.strip()
                )
            )
            if len(values) > MAX_EXECUTIVE_CONTEXT_REFS:
                raise ValueError(
                    f"{field_name} exceeds {MAX_EXECUTIVE_CONTEXT_REFS} references"
                )
            object.__setattr__(self, field_name, values)
        if (
            self.trigger_kind == ExecutiveTriggerKind.EVENT
            and not self.event_type
        ):
            raise ValueError("event-triggered Executive activation requires event_type")
        return self


class ExecutiveRoleSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role_id: str
    score: int = Field(ge=0)
    reasons: tuple[str, ...]
    explicit: bool = False


class ExecutiveCanonicalContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    goals: tuple[dict[str, Any], ...] = ()
    decisions: tuple[dict[str, Any], ...] = ()
    work_items: tuple[dict[str, Any], ...] = ()
    work_graphs: tuple[dict[str, Any], ...] = ()
    evidence: tuple[dict[str, Any], ...] = ()


class ExecutiveProposalDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ExecutiveProposalKind
    title: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=12000)
    payload: dict[str, Any] = Field(default_factory=dict)


class ExecutiveRoleOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=16000)
    recommendation: str = Field(min_length=1, max_length=16000)
    risks: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    disagreement: tuple[str, ...] = ()
    proposals: tuple[ExecutiveProposalDraft, ...] = ()

    @model_validator(mode="after")
    def normalize(self) -> "ExecutiveRoleOutput":
        for field_name in ("risks", "assumptions", "disagreement"):
            object.__setattr__(
                self,
                field_name,
                tuple(dict.fromkeys(getattr(self, field_name))),
            )
        if len(self.proposals) > MAX_EXECUTIVE_PROPOSALS:
            raise ValueError(
                f"Executive output exceeds {MAX_EXECUTIVE_PROPOSALS} proposals"
            )
        return self


class ExecutiveConsultation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"executive-consultation-{uuid.uuid4().hex}")
    role_id: str
    role_definition: DefinitionReference
    output: ExecutiveRoleOutput
    model_invocation_id: str
    started_at: float
    completed_at: float = Field(default_factory=time.time)


class ExecutiveSynthesis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recommendation: str = Field(min_length=1, max_length=20000)
    rationale: str = Field(min_length=1, max_length=20000)
    disagreement: tuple[str, ...] = ()
    escalation_required: bool = False
    escalation_reason: str | None = None
    model_invocation_id: str | None = None


class ExecutiveProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"executive-proposal-{uuid.uuid4().hex}")
    role_id: str
    kind: ExecutiveProposalKind
    title: str
    rationale: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: ExecutiveProposalStatus = ExecutiveProposalStatus.PROPOSED
    authority_capability: str | None = None
    authority_reasons: tuple[str, ...] = ()
    resulting_ref: str | None = None
    materialized_by: str | None = None
    materialized_at: float | None = None


class ExecutiveActivation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"executive-activation-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    initiated_by: str
    subject: str
    request: str
    trigger_kind: ExecutiveTriggerKind
    trigger_ref: str | None = None
    event_type: str | None = None
    project_id: str | None = None
    goal_ids: tuple[str, ...] = ()
    decision_ids: tuple[str, ...] = ()
    work_item_refs: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    role_catalog: DefinitionReference
    selections: tuple[ExecutiveRoleSelection, ...]
    budget: ExecutiveReasoningBudget
    status: ExecutiveActivationStatus = ExecutiveActivationStatus.PLANNED
    context: ExecutiveCanonicalContext = Field(
        default_factory=ExecutiveCanonicalContext
    )
    consultations: tuple[ExecutiveConsultation, ...] = ()
    synthesis: ExecutiveSynthesis | None = None
    proposals: tuple[ExecutiveProposal, ...] = ()
    failure_reason: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    completed_at: float | None = None
    revision: int = Field(default=1, ge=1)


class ExecutiveActivationRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    activation_id: str
    revision: int = Field(ge=1)
    snapshot: ExecutiveActivation
    reason: str
    revised_by: str
    revised_at: float = Field(default_factory=time.time)


class ExecutiveActivationState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = EXECUTIVE_ACTIVATION_CONTRACT.current
    activations: list[ExecutiveActivation] = Field(default_factory=list)
    revisions: list[ExecutiveActivationRevision] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        EXECUTIVE_ACTIVATION_CONTRACT.require(self.schema_version)
