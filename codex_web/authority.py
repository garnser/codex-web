from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.definitions import DefinitionReference
from codex_web.resources import (
    ResourceRisk,
    ResourceSensitivity,
    ResourceType,
)


AUTHORITY_ROLE_CATALOG_KIND = "authority-role-catalog"
AUTHORITY_ROLE_CATALOG_ID = "authority.roles.default"
AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION = "1.0"


class AuthorityLevel(StrEnum):
    READ = "read"
    RECOMMEND = "recommend"
    PREPARE = "prepare"
    EXECUTE = "execute"
    APPROVE = "approve"


AUTHORITY_LEVEL_RANK: dict[AuthorityLevel, int] = {
    AuthorityLevel.READ: 10,
    AuthorityLevel.RECOMMEND: 20,
    AuthorityLevel.PREPARE: 30,
    AuthorityLevel.EXECUTE: 40,
    AuthorityLevel.APPROVE: 50,
}


class AuthorityEnvironment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class AuthorityAutonomyRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


AUTHORITY_AUTONOMY_RISK_RANK: dict[AuthorityAutonomyRisk, int] = {
    AuthorityAutonomyRisk.LOW: 10,
    AuthorityAutonomyRisk.MEDIUM: 20,
    AuthorityAutonomyRisk.HIGH: 30,
    AuthorityAutonomyRisk.CRITICAL: 40,
}


class AuthorityDecisionOutcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class AuthorityApprovalRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    count: int = Field(default=0, ge=0, le=20)
    role_ids: tuple[str, ...] = ()


class AuthorityGrant(BaseModel):
    """One atomic permission grant.

    A request must satisfy every constraint on one complete grant. The evaluator
    never combines a capability from one grant with scope/budget from another.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    capability: str = Field(min_length=1, pattern=r"^(\*|[a-z0-9][a-z0-9._:-]*)$")
    level: AuthorityLevel
    project_ids: tuple[str, ...] = ()
    resource_ids: tuple[str, ...] = ()
    resource_types: tuple[ResourceType, ...] = ()
    resource_risks: tuple[ResourceRisk, ...] = ()
    resource_sensitivities: tuple[ResourceSensitivity, ...] = ()
    environments: tuple[AuthorityEnvironment, ...] = ()
    max_amount_usd: float | None = Field(default=None, ge=0.0)
    max_input_tokens: int | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, ge=0)
    max_model_calls: int | None = Field(default=None, ge=0)
    max_autonomous_risk: AuthorityAutonomyRisk = AuthorityAutonomyRisk.LOW
    approvals: AuthorityApprovalRequirement = Field(
        default_factory=AuthorityApprovalRequirement
    )

    @model_validator(mode="after")
    def normalize(self) -> "AuthorityGrant":
        for field_name in (
            "project_ids",
            "resource_ids",
            "resource_types",
            "resource_risks",
            "resource_sensitivities",
            "environments",
        ):
            values = tuple(dict.fromkeys(getattr(self, field_name)))
            object.__setattr__(self, field_name, values)
        object.__setattr__(
            self.approvals,
            "role_ids",
            tuple(dict.fromkeys(self.approvals.role_ids)),
        )
        return self


class AuthorityRoleDefinition(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    inherits: tuple[str, ...] = ()
    grants: tuple[AuthorityGrant, ...] = ()


class AuthorityRoleBinding(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    id: str = Field(
        default_factory=lambda: f"authority-binding-{uuid.uuid4().hex}",
        min_length=1,
    )
    role_id: str = Field(min_length=1)
    subject_kind: Literal["identity", "team"]
    subject_id: str = Field(min_length=1)
    organization_id: str | None = None
    workspace_id: str | None = None
    project_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def normalize(self) -> "AuthorityRoleBinding":
        object.__setattr__(
            self,
            "project_ids",
            tuple(dict.fromkeys(self.project_ids)),
        )
        return self


class AuthorityDelegation(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    id: str = Field(min_length=1)
    role_id: str = Field(min_length=1)
    delegate_identity_id: str = Field(min_length=1)
    delegated_by_identity_id: str = Field(min_length=1)
    organization_id: str | None = None
    workspace_id: str | None = None
    project_ids: tuple[str, ...] = ()
    expires_at: float = Field(gt=0.0)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def normalize(self) -> "AuthorityDelegation":
        object.__setattr__(
            self,
            "project_ids",
            tuple(dict.fromkeys(self.project_ids)),
        )
        return self


class AuthorityRoleCatalogDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    roles: tuple[AuthorityRoleDefinition, ...]
    bindings: tuple[AuthorityRoleBinding, ...] = ()
    delegations: tuple[AuthorityDelegation, ...] = ()

    @model_validator(mode="after")
    def validate_catalog(self) -> "AuthorityRoleCatalogDefinition":
        role_ids = [item.id for item in self.roles]
        if len(role_ids) != len(set(role_ids)):
            raise ValueError("authority role ids must be unique")
        known = set(role_ids)

        grant_ids: list[str] = []
        for role in self.roles:
            unknown = set(role.inherits) - known
            if unknown:
                raise ValueError(
                    f"authority role {role.id} inherits unknown roles: "
                    + ", ".join(sorted(unknown))
                )
            grant_ids.extend(item.id for item in role.grants)
        if len(grant_ids) != len(set(grant_ids)):
            raise ValueError("authority grant ids must be globally unique")

        for binding in self.bindings:
            if binding.role_id not in known:
                raise ValueError(
                    f"authority binding {binding.id} references unknown role "
                    f"{binding.role_id}"
                )
        binding_keys = [
            (
                item.role_id,
                item.subject_kind,
                item.subject_id,
                item.project_ids,
            )
            for item in self.bindings
        ]
        if len(binding_keys) != len(set(binding_keys)):
            raise ValueError("duplicate authority role binding is ambiguous")

        delegation_ids = [item.id for item in self.delegations]
        if len(delegation_ids) != len(set(delegation_ids)):
            raise ValueError("authority delegation ids must be unique")
        for delegation in self.delegations:
            if delegation.role_id not in known:
                raise ValueError(
                    f"authority delegation {delegation.id} references unknown role "
                    f"{delegation.role_id}"
                )

        visiting: set[str] = set()
        visited: set[str] = set()
        roles = {item.id: item for item in self.roles}

        def visit(role_id: str) -> None:
            if role_id in visited:
                return
            if role_id in visiting:
                raise ValueError("authority role inheritance contains a cycle")
            visiting.add(role_id)
            for inherited in roles[role_id].inherits:
                visit(inherited)
            visiting.remove(role_id)
            visited.add(role_id)

        for role_id in sorted(roles):
            visit(role_id)
        return self


class AuthorityEvaluationRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    capability: str = Field(min_length=1)
    level: AuthorityLevel
    project_id: str | None = None
    resource_ids: tuple[str, ...] = ()
    environment: AuthorityEnvironment | None = None
    amount_usd: float | None = Field(default=None, ge=0.0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    model_calls: int | None = Field(default=None, ge=0)
    autonomous_risk: AuthorityAutonomyRisk = AuthorityAutonomyRisk.LOW
    approval_role_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def normalize(self) -> "AuthorityEvaluationRequest":
        object.__setattr__(
            self,
            "resource_ids",
            tuple(dict.fromkeys(self.resource_ids)),
        )
        object.__setattr__(
            self,
            "approval_role_ids",
            tuple(dict.fromkeys(self.approval_role_ids)),
        )
        return self


class AuthorityDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"authority-decision-{uuid.uuid4().hex}")
    outcome: AuthorityDecisionOutcome
    source: str = "canonical:role-authority"
    actor_identity_id: str
    organization_id: str
    workspace_id: str
    request: AuthorityEvaluationRequest
    definition_ref: DefinitionReference | None = None
    matched_role_ids: tuple[str, ...] = ()
    matched_grant_ids: tuple[str, ...] = ()
    delegation_ids: tuple[str, ...] = ()
    expires_at: float | None = None
    reasons: tuple[str, ...] = ()
    evaluated_at: float = Field(default_factory=time.time)


def validate_authority_role_catalog(payload: dict[str, Any]) -> dict[str, Any]:
    return AuthorityRoleCatalogDefinition.model_validate(payload).model_dump(
        mode="json"
    )
