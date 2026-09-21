from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec
from codex_web.definitions import DefinitionReference


AGENT_TEAM_STATE_CONTRACT = ContractSpec("agent-team-state", "1.0", ("1.0",))
TEAM_INSTRUCTIONS_KIND = "agent.team.instructions"
TEAM_INSTRUCTIONS_SCHEMA_VERSION = "1.0"


class AgentTeamLifecycle(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    ARCHIVED = "archived"


class AgentTeamMember(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    profile_id: str = Field(min_length=1)
    role: str = Field(default="member", max_length=120)
    capability_tags: tuple[str, ...] = ()
    required: bool = False

    @model_validator(mode="after")
    def normalize(self) -> "AgentTeamMember":
        object.__setattr__(
            self,
            "capability_tags",
            tuple(dict.fromkeys(
                str(item).strip().casefold()
                for item in self.capability_tags
                if str(item).strip()
            )),
        )
        return self


class AgentTeamBudgets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_handoffs: int = Field(default=6, ge=1, le=100)
    max_participants: int = Field(default=4, ge=1, le=50)
    max_coordinator_rounds: int = Field(default=3, ge=1, le=20)
    max_parallel_executions: int = Field(default=2, ge=1, le=20)


class AgentTeamRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    record_id: str = Field(
        default_factory=lambda: f"agent-team-rev-{uuid.uuid4().hex}",
        min_length=1,
    )
    team_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    revision: int = Field(ge=1)
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    lifecycle: AgentTeamLifecycle = AgentTeamLifecycle.ACTIVE
    owner_identity_id: str = Field(min_length=1)
    leader_profile_id: str = Field(min_length=1)
    members: tuple[AgentTeamMember, ...] = ()
    instructions_ref: DefinitionReference | None = None
    budgets: AgentTeamBudgets = Field(default_factory=AgentTeamBudgets)
    allowed_identity_ids: tuple[str, ...] = ()
    allowed_role_ids: tuple[str, ...] = ()
    escalation_target: str | None = Field(default=None, max_length=500)
    created_by: str = Field(min_length=1)
    updated_by: str = Field(min_length=1)
    change_reason: str | None = Field(default=None, max_length=1000)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def validate_team(self) -> "AgentTeamRevision":
        member_ids = [item.profile_id for item in self.members]
        if self.leader_profile_id in member_ids:
            raise ValueError("leader profile must not be duplicated in members")
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("agent team member profile ids must be unique")
        if self.budgets.max_participants > len(member_ids) + 1:
            object.__setattr__(
                self,
                "budgets",
                self.budgets.model_copy(
                    update={"max_participants": max(1, len(member_ids) + 1)}
                ),
            )
        object.__setattr__(
            self,
            "allowed_identity_ids",
            tuple(dict.fromkeys(
                str(item).strip()
                for item in self.allowed_identity_ids
                if str(item).strip()
            )),
        )
        object.__setattr__(
            self,
            "allowed_role_ids",
            tuple(dict.fromkeys(
                str(item).strip()
                for item in self.allowed_role_ids
                if str(item).strip()
            )),
        )
        return self


class AgentTeamState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = AGENT_TEAM_STATE_CONTRACT.current
    revisions: list[AgentTeamRevision] = Field(default_factory=list)


class AgentTeamCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    team_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    owner_identity_id: str | None = None
    leader_profile_id: str = Field(min_length=1)
    members: tuple[AgentTeamMember, ...] = ()
    instructions_ref: DefinitionReference | None = None
    budgets: AgentTeamBudgets = Field(default_factory=AgentTeamBudgets)
    allowed_identity_ids: tuple[str, ...] = ()
    allowed_role_ids: tuple[str, ...] = ()
    escalation_target: str | None = Field(default=None, max_length=500)
    reason: str | None = Field(default=None, max_length=1000)


class AgentTeamUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    owner_identity_id: str | None = None
    leader_profile_id: str | None = None
    members: tuple[AgentTeamMember, ...] | None = None
    instructions_ref: DefinitionReference | None = None
    budgets: AgentTeamBudgets | None = None
    allowed_identity_ids: tuple[str, ...] | None = None
    allowed_role_ids: tuple[str, ...] | None = None
    escalation_target: str | None = Field(default=None, max_length=500)
    reason: str = Field(min_length=1, max_length=1000)


class AgentTeamLifecycleChange(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=1, max_length=1000)


class TeamDelegationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    work_item_id: str = Field(min_length=1)
    project_id: str | None = None
    required_capabilities: tuple[str, ...] = ()
    active_profile_ids: tuple[str, ...] = ()
    unavailable_profile_ids: tuple[str, ...] = ()
    prior_participant_ids: tuple[str, ...] = ()
    handoff_count: int = Field(default=0, ge=0)
    coordinator_round: int = Field(default=0, ge=0)
    trigger_id: str | None = None

    @model_validator(mode="after")
    def normalize(self) -> "TeamDelegationRequest":
        for field_name in (
            "required_capabilities",
            "active_profile_ids",
            "unavailable_profile_ids",
            "prior_participant_ids",
        ):
            values = getattr(self, field_name)
            object.__setattr__(
                self,
                field_name,
                tuple(dict.fromkeys(
                    str(item).strip().casefold()
                    if field_name == "required_capabilities"
                    else str(item).strip()
                    for item in values
                    if str(item).strip()
                )),
            )
        return self


class TeamCoordinatorDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    selected_profile_ids: tuple[str, ...]
    reason: str = Field(min_length=1, max_length=2000)
    decision_key: str = Field(min_length=1, max_length=500)


class TeamDelegationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    team_id: str
    team_revision: int
    work_item_id: str
    mode: str
    selected_profile_ids: tuple[str, ...] = ()
    leader_profile_id: str | None = None
    instructions_ref: DefinitionReference | None = None
    reason: str
    handoff_count: int = 0
    coordinator_round: int = 0
    blocked: bool = False
    attention_required: bool = False
    dedupe_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
