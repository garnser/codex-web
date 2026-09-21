from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.agent_profiles import AgentProfileAccessPolicy
from codex_web.compatibility import ContractSpec
from codex_web.definitions import DefinitionReference


AGENT_TEAM_STATE_CONTRACT = ContractSpec(
    "agent-team-state",
    "1.0",
    ("1.0",),
)
AGENT_TEAM_ROUTING_KIND = "agent.team-routing"
AGENT_TEAM_ROUTING_SCHEMA_VERSION = "1.0"


class AgentTeamLifecycle(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    ARCHIVED = "archived"


class AgentTeamMemberKind(StrEnum):
    AGENT = "agent"
    HUMAN = "human"


class AgentTeamDelegationMode(StrEnum):
    DIRECT = "direct"
    COORDINATOR = "coordinator"


class AgentTeamDelegationStatus(StrEnum):
    PLANNED = "planned"
    COORDINATOR_PENDING = "coordinator_pending"
    DISPATCHED = "dispatched"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    ESCALATED = "escalated"


class AgentTeamRoutingDefinition(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    team_id: str = Field(min_length=1, max_length=200)
    instructions: str = Field(min_length=1, max_length=12000)
    decision_contract: str = Field(
        default=(
            "Return a structured delegation decision only. Select eligible "
            "member profile IDs; do not grant authority or change budgets."
        ),
        min_length=1,
        max_length=2000,
    )


class AgentTeamMember(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    member_id: str = Field(min_length=1, max_length=200)
    kind: AgentTeamMemberKind = AgentTeamMemberKind.AGENT
    profile_id: str | None = Field(default=None, max_length=200)
    profile_revision: int | None = Field(default=None, ge=1)
    identity_id: str | None = Field(default=None, max_length=500)
    capability_tags: tuple[str, ...] = ()
    role_description: str = Field(default="", max_length=2000)
    enabled: bool = True

    @model_validator(mode="after")
    def normalize(self) -> "AgentTeamMember":
        object.__setattr__(
            self,
            "capability_tags",
            tuple(
                sorted(
                    {
                        str(value).strip().casefold()
                        for value in self.capability_tags
                        if str(value).strip()
                    }
                )
            ),
        )
        if self.kind == AgentTeamMemberKind.AGENT:
            if not self.profile_id or self.profile_revision is None:
                raise ValueError(
                    "agent team member requires profile_id and profile_revision"
                )
            if self.identity_id is not None:
                raise ValueError("agent team member cannot carry identity_id")
        else:
            if not self.identity_id:
                raise ValueError("human team member requires identity_id")
            if self.profile_id is not None or self.profile_revision is not None:
                raise ValueError("human team member cannot carry Agent Profile fields")
        return self


class AgentTeamBudgets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_handoffs: int = Field(default=4, ge=1, le=32)
    max_participants: int = Field(default=3, ge=1, le=16)
    max_coordinator_rounds: int = Field(default=1, ge=1, le=8)
    max_parallel_executions: int = Field(default=3, ge=1, le=16)

    @model_validator(mode="after")
    def validate_parallelism(self) -> "AgentTeamBudgets":
        if self.max_parallel_executions > self.max_participants:
            raise ValueError(
                "max_parallel_executions cannot exceed max_participants"
            )
        return self


class AgentTeamRevision(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    record_id: str = Field(
        default_factory=lambda: f"agent-team-rev-{uuid.uuid4().hex}",
        min_length=1,
    )
    team_id: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    revision: int = Field(ge=1)
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    lifecycle: AgentTeamLifecycle = AgentTeamLifecycle.ACTIVE
    owner_identity_id: str = Field(min_length=1)
    created_by: str = Field(min_length=1)
    updated_by: str = Field(min_length=1)
    leader_profile_id: str = Field(min_length=1, max_length=200)
    leader_profile_revision: int = Field(ge=1)
    members: tuple[AgentTeamMember, ...]
    routing_definition_ref: DefinitionReference
    access: AgentProfileAccessPolicy = Field(
        default_factory=AgentProfileAccessPolicy
    )
    budgets: AgentTeamBudgets = Field(default_factory=AgentTeamBudgets)
    escalation_identity_ids: tuple[str, ...] = ()
    escalation_team_ids: tuple[str, ...] = ()
    change_reason: str | None = Field(default=None, max_length=1000)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def normalize(self) -> "AgentTeamRevision":
        ids = [member.member_id for member in self.members]
        if len(ids) != len(set(ids)):
            raise ValueError("agent team member IDs must be unique")
        profile_keys = [
            (member.profile_id, member.profile_revision)
            for member in self.members
            if member.kind == AgentTeamMemberKind.AGENT
        ]
        if len(profile_keys) != len(set(profile_keys)):
            raise ValueError("agent team Agent Profile members must be unique")
        object.__setattr__(
            self,
            "escalation_identity_ids",
            tuple(
                sorted(
                    {
                        str(value).strip()
                        for value in self.escalation_identity_ids
                        if str(value).strip()
                    }
                )
            ),
        )
        object.__setattr__(
            self,
            "escalation_team_ids",
            tuple(
                sorted(
                    {
                        str(value).strip()
                        for value in self.escalation_team_ids
                        if str(value).strip()
                    }
                )
            ),
        )
        if self.routing_definition_ref.kind != AGENT_TEAM_ROUTING_KIND:
            raise ValueError("agent team routing definition kind is invalid")
        return self


class AgentTeamMemberInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    member_id: str = Field(min_length=1, max_length=200)
    kind: AgentTeamMemberKind = AgentTeamMemberKind.AGENT
    profile_id: str | None = Field(default=None, max_length=200)
    profile_revision: int | None = Field(default=None, ge=1)
    identity_id: str | None = Field(default=None, max_length=500)
    capability_tags: tuple[str, ...] = ()
    role_description: str = Field(default="", max_length=2000)
    enabled: bool = True


class AgentTeamCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    team_id: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    owner_identity_id: str | None = None
    leader_profile_id: str = Field(min_length=1, max_length=200)
    leader_profile_revision: int | None = Field(default=None, ge=1)
    members: tuple[AgentTeamMemberInput, ...]
    routing_instructions: str = Field(min_length=1, max_length=12000)
    access: AgentProfileAccessPolicy = Field(
        default_factory=AgentProfileAccessPolicy
    )
    budgets: AgentTeamBudgets = Field(default_factory=AgentTeamBudgets)
    escalation_identity_ids: tuple[str, ...] = ()
    escalation_team_ids: tuple[str, ...] = ()
    reason: str | None = Field(default=None, max_length=1000)


class AgentTeamUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    owner_identity_id: str | None = None
    leader_profile_id: str | None = Field(default=None, min_length=1, max_length=200)
    leader_profile_revision: int | None = Field(default=None, ge=1)
    members: tuple[AgentTeamMemberInput, ...] | None = None
    routing_instructions: str | None = Field(
        default=None,
        min_length=1,
        max_length=12000,
    )
    access: AgentProfileAccessPolicy | None = None
    budgets: AgentTeamBudgets | None = None
    escalation_identity_ids: tuple[str, ...] | None = None
    escalation_team_ids: tuple[str, ...] | None = None
    reason: str = Field(min_length=1, max_length=1000)


class AgentTeamLifecycleChange(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=1, max_length=1000)


class AgentTeamAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    work_item_ref: str = Field(min_length=1, max_length=500)
    project_id: str = Field(min_length=1, max_length=500)
    objective: str = Field(min_length=1, max_length=12000)
    required_capabilities: tuple[str, ...] = ()
    event_id: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def normalize(self) -> "AgentTeamAssignmentRequest":
        self.required_capabilities = tuple(
            sorted(
                {
                    str(value).strip().casefold()
                    for value in self.required_capabilities
                    if str(value).strip()
                }
            )
        )
        return self


class AgentTeamCoordinatorDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    delegation_id: str = Field(min_length=1)
    team_revision: int = Field(ge=1)
    source_profile_id: str = Field(min_length=1)
    selected_member_ids: tuple[str, ...]
    reason: str = Field(min_length=1, max_length=4000)
    observed_event_id: str | None = Field(default=None, max_length=500)


class AgentTeamExecutionLink(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    member_id: str
    profile_id: str
    profile_revision: int
    thread_id: str | None = None
    execution_id: str | None = None
    assignment_id: str | None = None


class AgentTeamDelegationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delegation_id: str = Field(
        default_factory=lambda: f"team-delegation-{uuid.uuid4().hex}"
    )
    organization_id: str
    workspace_id: str
    team_id: str
    team_revision: int = Field(ge=1)
    team_record_id: str
    work_item_ref: str
    project_id: str
    objective: str
    required_capabilities: tuple[str, ...] = ()
    mode: AgentTeamDelegationMode
    status: AgentTeamDelegationStatus
    reason_codes: tuple[str, ...] = ()
    selected_member_ids: tuple[str, ...] = ()
    active_member_ids: tuple[str, ...] = ()
    coordinator_profile_id: str | None = None
    coordinator_profile_revision: int | None = None
    coordinator_round: int = Field(default=0, ge=0)
    handoff_count: int = Field(default=0, ge=0)
    event_id: str | None = None
    equivalent_decision_key: str | None = None
    execution_links: tuple[AgentTeamExecutionLink, ...] = ()
    coordinator_input_tokens: int = Field(default=0, ge=0)
    coordinator_output_tokens: int = Field(default=0, ge=0)
    coordinator_cost_usd: float = Field(default=0.0, ge=0.0)
    worker_input_tokens: int = Field(default=0, ge=0)
    worker_output_tokens: int = Field(default=0, ge=0)
    worker_cost_usd: float = Field(default=0.0, ge=0.0)
    blocker: str | None = None
    created_by: str
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class AgentTeamUsageUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coordinator_input_tokens: int = Field(default=0, ge=0)
    coordinator_output_tokens: int = Field(default=0, ge=0)
    coordinator_cost_usd: float = Field(default=0.0, ge=0.0)
    worker_input_tokens: int = Field(default=0, ge=0)
    worker_output_tokens: int = Field(default=0, ge=0)
    worker_cost_usd: float = Field(default=0.0, ge=0.0)


class AgentTeamState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = AGENT_TEAM_STATE_CONTRACT.current
    revisions: list[AgentTeamRevision] = Field(default_factory=list)
    delegations: list[AgentTeamDelegationRecord] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        AGENT_TEAM_STATE_CONTRACT.require(self.schema_version)
