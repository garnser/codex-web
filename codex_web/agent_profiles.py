from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.agent_providers import AgentProviderCapability
from codex_web.compatibility import ContractSpec
from codex_web.definitions import DefinitionReference


AGENT_PROFILE_STATE_CONTRACT = ContractSpec(
    "agent-profile-state",
    "1.0",
    ("1.0",),
)


class AgentProfileLifecycle(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    ARCHIVED = "archived"


class AgentProfileAccessMode(StrEnum):
    TENANT = "tenant"
    OWNER = "owner"
    ALLOWLIST = "allowlist"


class AgentProfileAccessPolicy(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    mode: AgentProfileAccessMode = AgentProfileAccessMode.TENANT
    identity_ids: tuple[str, ...] = ()
    role_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def normalize(self) -> "AgentProfileAccessPolicy":
        object.__setattr__(
            self,
            "identity_ids",
            tuple(
                dict.fromkeys(
                    value
                    for value in self.identity_ids
                    if str(value).strip()
                )
            ),
        )
        object.__setattr__(
            self,
            "role_ids",
            tuple(
                dict.fromkeys(
                    value
                    for value in self.role_ids
                    if str(value).strip()
                )
            ),
        )
        if (
            self.mode == AgentProfileAccessMode.ALLOWLIST
            and not self.identity_ids
            and not self.role_ids
        ):
            raise ValueError(
                "allowlist profile access requires identity_ids or role_ids"
            )
        return self


class AgentProfileBudgetDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_concurrency: int = Field(default=1, ge=1, le=100)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_model_calls: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, ge=0.0)


class AgentProfileRuntimePolicy(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    required_capabilities: tuple[AgentProviderCapability, ...] = (
        AgentProviderCapability.AGENT_EXECUTION,
    )
    allowed_provider_ids: tuple[str, ...] = ()
    allowed_runtime_ids: tuple[str, ...] = ()
    preferred_provider_ids: tuple[str, ...] = ()
    preferred_runtime_ids: tuple[str, ...] = ()
    required_residency_tags: tuple[str, ...] = ()
    required_compliance_tags: tuple[str, ...] = ()
    required_sandbox_profile: str | None = None
    required_network_profile: str | None = None
    require_persistent_session: bool = False
    max_runtime_cost_usd: float | None = Field(default=None, ge=0.0)
    allow_fallback: bool = True

    @model_validator(mode="after")
    def normalize(self) -> "AgentProfileRuntimePolicy":
        capabilities = list(dict.fromkeys(self.required_capabilities))
        if AgentProviderCapability.AGENT_EXECUTION not in capabilities:
            capabilities.insert(0, AgentProviderCapability.AGENT_EXECUTION)
        if (
            self.require_persistent_session
            and AgentProviderCapability.PERSISTENT_SESSIONS
            not in capabilities
        ):
            capabilities.append(
                AgentProviderCapability.PERSISTENT_SESSIONS
            )
        object.__setattr__(
            self,
            "required_capabilities",
            tuple(capabilities),
        )
        for field_name in (
            "allowed_provider_ids",
            "allowed_runtime_ids",
            "preferred_provider_ids",
            "preferred_runtime_ids",
            "required_residency_tags",
            "required_compliance_tags",
        ):
            object.__setattr__(
                self,
                field_name,
                tuple(
                    dict.fromkeys(
                        str(value).strip()
                        for value in getattr(self, field_name)
                        if str(value).strip()
                    )
                ),
            )
        return self


class AgentProfileModelPolicy(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    model_class: str | None = None
    preferred_provider_ids: tuple[str, ...] = ()
    allow_fallback: bool = True

    @model_validator(mode="after")
    def normalize(self) -> "AgentProfileModelPolicy":
        object.__setattr__(
            self,
            "preferred_provider_ids",
            tuple(
                dict.fromkeys(
                    str(value).strip()
                    for value in self.preferred_provider_ids
                    if str(value).strip()
                )
            ),
        )
        return self


class AgentProfileRevision(BaseModel):
    """Immutable logical-agent configuration revision.

    Provider/runtime/worker identity is intentionally absent. Those are selected
    per execution and recorded in execution provenance.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    record_id: str = Field(
        default_factory=lambda: f"agent-profile-rev-{uuid.uuid4().hex}",
        min_length=1,
    )
    profile_id: str = Field(
        min_length=1,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    revision: int = Field(ge=1)
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)

    name: str = Field(min_length=1, max_length=120)
    avatar_ref: str | None = Field(default=None, max_length=500)
    description: str = Field(default="", max_length=4000)
    lifecycle: AgentProfileLifecycle = AgentProfileLifecycle.ACTIVE

    owner_identity_id: str = Field(min_length=1)
    created_by: str = Field(min_length=1)
    updated_by: str = Field(min_length=1)

    role_id: str | None = None
    role_definition_ref: DefinitionReference | None = None
    instructions_ref: DefinitionReference | None = None
    skill_refs: tuple[DefinitionReference, ...] = ()

    access: AgentProfileAccessPolicy = Field(
        default_factory=AgentProfileAccessPolicy
    )
    authority_role_id: str | None = None
    authority_definition_ref: DefinitionReference | None = None

    runtime_policy: AgentProfileRuntimePolicy = Field(
        default_factory=AgentProfileRuntimePolicy
    )
    model_policy: AgentProfileModelPolicy = Field(
        default_factory=AgentProfileModelPolicy
    )

    execution_profile_id: str | None = None
    sandbox_requirement: str | None = None
    budgets: AgentProfileBudgetDefaults = Field(
        default_factory=AgentProfileBudgetDefaults
    )

    change_reason: str | None = Field(default=None, max_length=1000)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def validate_profile(self) -> "AgentProfileRevision":
        refs = [
            item.record_id
            for item in self.skill_refs
        ]
        if len(refs) != len(set(refs)):
            raise ValueError("agent profile skill refs must be unique")
        if (
            self.authority_role_id is None
            and self.authority_definition_ref is not None
        ):
            raise ValueError(
                "authority_definition_ref requires authority_role_id"
            )
        if (
            self.role_id is None
            and self.role_definition_ref is not None
        ):
            raise ValueError(
                "role_definition_ref requires role_id"
            )
        return self


class AgentProfileState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = AGENT_PROFILE_STATE_CONTRACT.current
    revisions: list[AgentProfileRevision] = Field(default_factory=list)


class AgentProfileCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    profile_id: str = Field(
        min_length=1,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    name: str = Field(min_length=1, max_length=120)
    avatar_ref: str | None = Field(default=None, max_length=500)
    description: str = Field(default="", max_length=4000)
    owner_identity_id: str | None = None
    role_id: str | None = None
    role_definition_ref: DefinitionReference | None = None
    instructions_ref: DefinitionReference | None = None
    skill_refs: tuple[DefinitionReference, ...] = ()
    access: AgentProfileAccessPolicy = Field(
        default_factory=AgentProfileAccessPolicy
    )
    authority_role_id: str | None = None
    authority_definition_ref: DefinitionReference | None = None
    runtime_policy: AgentProfileRuntimePolicy = Field(
        default_factory=AgentProfileRuntimePolicy
    )
    model_policy: AgentProfileModelPolicy = Field(
        default_factory=AgentProfileModelPolicy
    )
    execution_profile_id: str | None = None
    sandbox_requirement: str | None = None
    budgets: AgentProfileBudgetDefaults = Field(
        default_factory=AgentProfileBudgetDefaults
    )
    reason: str | None = Field(default=None, max_length=1000)


class AgentProfileUpdate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    name: str | None = Field(default=None, min_length=1, max_length=120)
    avatar_ref: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=4000)
    owner_identity_id: str | None = None
    role_id: str | None = None
    role_definition_ref: DefinitionReference | None = None
    instructions_ref: DefinitionReference | None = None
    skill_refs: tuple[DefinitionReference, ...] | None = None
    access: AgentProfileAccessPolicy | None = None
    authority_role_id: str | None = None
    authority_definition_ref: DefinitionReference | None = None
    runtime_policy: AgentProfileRuntimePolicy | None = None
    model_policy: AgentProfileModelPolicy | None = None
    execution_profile_id: str | None = None
    sandbox_requirement: str | None = None
    budgets: AgentProfileBudgetDefaults | None = None
    reason: str = Field(min_length=1, max_length=1000)


class AgentProfileLifecycleChange(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=1, max_length=1000)


class AgentProfileExecutionBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str
    profile_revision: int = Field(ge=1)
    profile_record_id: str
    instructions_ref: DefinitionReference | None = None
    skill_refs: tuple[DefinitionReference, ...] = ()
    role_id: str | None = None
    role_definition_ref: DefinitionReference | None = None
    authority_role_id: str | None = None
    authority_definition_ref: DefinitionReference | None = None
    execution_profile_id: str | None = None
    sandbox_requirement: str | None = None
    selected_provider_id: str | None = None
    selected_runtime_id: str | None = None
    selected_provider_revision: int | None = None
    selected_runtime_capability_revision: int | None = None
    selected_worker_id: str | None = None
    model_provider_id: str | None = None
    model_id: str | None = None


class AgentProfileAccessDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    profile_id: str
    profile_revision: int
    actor_identity_id: str
    actor_role_ids: tuple[str, ...] = ()
    required_authority_role_id: str | None = None
    reasons: tuple[str, ...] = ()
