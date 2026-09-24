from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


DEFAULT_ORGANIZATION_ID = "local"
DEFAULT_WORKSPACE_ID = "default"
DEFAULT_HUMAN_IDENTITY_ID = "local-admin"


class PrincipalKind(StrEnum):
    HUMAN = "human"
    SERVICE = "service"


class MembershipRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    APPROVER = "approver"
    MEMBER = "member"


class AuthenticationAssurance(StrEnum):
    LOCAL_TRUSTED = "local_trusted"
    PRIMARY = "primary"
    OIDC = "oidc"
    MFA = "mfa"
    RECOVERY = "recovery"
    SERVICE_TOKEN = "service_token"


ASSURANCE_RANK: dict[AuthenticationAssurance, int] = {
    AuthenticationAssurance.RECOVERY: 0,
    AuthenticationAssurance.PRIMARY: 1,
    AuthenticationAssurance.OIDC: 1,
    AuthenticationAssurance.SERVICE_TOKEN: 1,
    AuthenticationAssurance.MFA: 2,
    AuthenticationAssurance.LOCAL_TRUSTED: 3,
}


class TenantScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    organization_id: str = Field(default=DEFAULT_ORGANIZATION_ID, min_length=1)
    workspace_id: str = Field(default=DEFAULT_WORKSPACE_ID, min_length=1)


class Organization(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    created_at: float = Field(default_factory=time.time)
    disabled_at: float | None = None


class Workspace(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1)
    organization_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    created_at: float = Field(default_factory=time.time)
    disabled_at: float | None = None


class ExternalIdentityLink(BaseModel):
    """Authentication-provider subject mapping; never an authorization grant."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: str = Field(min_length=1)
    issuer: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    email: str | None = None
    groups_snapshot: list[str] = Field(default_factory=list)
    linked_at: float = Field(default_factory=time.time)
    last_seen_at: float | None = None


class HumanIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"human-{uuid.uuid4().hex}")
    kind: Literal[PrincipalKind.HUMAN] = PrincipalKind.HUMAN
    display_name: str = Field(min_length=1)
    email: str | None = None
    external_links: list[ExternalIdentityLink] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)
    disabled_at: float | None = None


class ServiceIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"service-{uuid.uuid4().hex}")
    kind: Literal[PrincipalKind.SERVICE] = PrincipalKind.SERVICE
    name: str = Field(min_length=1)
    description: str | None = None
    created_at: float = Field(default_factory=time.time)
    disabled_at: float | None = None


class Team(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"team-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str | None = None
    name: str = Field(min_length=1)
    member_identity_ids: list[str] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)


class Membership(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"membership-{uuid.uuid4().hex}")
    identity_id: str = Field(min_length=1)
    principal_kind: PrincipalKind
    organization_id: str = Field(min_length=1)
    workspace_id: str | None = None
    roles: list[MembershipRole] = Field(default_factory=lambda: [MembershipRole.MEMBER])
    team_ids: list[str] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)
    created_by: str | None = None
    updated_at: float | None = None
    updated_by: str | None = None
    revoked_at: float | None = None
    revoked_by: str | None = None

    @model_validator(mode="after")
    def normalize_roles(self) -> "Membership":
        self.roles = list(dict.fromkeys(self.roles))
        self.team_ids = sorted(set(self.team_ids))
        if not self.roles:
            self.roles = [MembershipRole.MEMBER]
        return self


class SessionRecord(BaseModel):
    """Server-side browser/API session. Raw credentials are never persisted."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"session-{uuid.uuid4().hex}")
    identity_id: str = Field(min_length=1)
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    assurance: AuthenticationAssurance
    session_token_hash: str = Field(min_length=64, max_length=64)
    refresh_token_hash: str = Field(min_length=64, max_length=64)
    used_refresh_hashes: list[str] = Field(default_factory=list)
    csrf_token_hash: str = Field(min_length=64, max_length=64)
    created_at: float
    last_seen_at: float
    idle_expires_at: float
    absolute_expires_at: float
    step_up_until: float | None = None
    rotation: int = Field(default=0, ge=0)
    revoked_at: float | None = None
    revoke_reason: str | None = None

    @model_validator(mode="after")
    def validate_lifetimes(self) -> "SessionRecord":
        if self.idle_expires_at > self.absolute_expires_at:
            self.idle_expires_at = self.absolute_expires_at
        if self.absolute_expires_at <= self.created_at:
            raise ValueError("session absolute expiry must be after creation")
        return self


class SessionCredentials(BaseModel):
    """One-time credential material returned only on create/refresh."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    session_token: str
    refresh_token: str
    csrf_token: str
    idle_expires_at: float
    absolute_expires_at: float


class ServiceTokenRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"token-{uuid.uuid4().hex}")
    service_identity_id: str = Field(min_length=1)
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    token_hash: str = Field(min_length=64, max_length=64)
    scopes: list[str] = Field(default_factory=list)
    created_at: float
    expires_at: float | None = None
    last_used_at: float | None = None
    rotation: int = Field(default=0, ge=0)
    revoked_at: float | None = None
    revoke_reason: str | None = None

    @model_validator(mode="after")
    def normalize_scopes(self) -> "ServiceTokenRecord":
        self.scopes = sorted({scope.strip() for scope in self.scopes if scope.strip()})
        return self


class ServiceTokenCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    token_id: str
    token: str
    expires_at: float | None = None


class RecoveryFactor(BaseModel):
    """Metadata-only account recovery factor; proof material stays provider-owned."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"recovery-{uuid.uuid4().hex}")
    identity_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    label: str = Field(min_length=1)
    created_at: float = Field(default_factory=time.time)
    disabled_at: float | None = None


class AuthenticationActor(BaseModel):
    """Canonical request actor independent from agent/execution-role identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    identity_id: str
    principal_kind: PrincipalKind
    organization_id: str
    workspace_id: str
    roles: tuple[MembershipRole, ...] = ()
    team_ids: tuple[str, ...] = ()
    assurance: AuthenticationAssurance
    session_id: str | None = None
    service_token_id: str | None = None
    service_scopes: tuple[str, ...] = ()
    authenticated_at: float = Field(default_factory=time.time)

    @property
    def tenant(self) -> TenantScope:
        return TenantScope(
            organization_id=self.organization_id,
            workspace_id=self.workspace_id,
        )

    def has_role(self, *roles: MembershipRole) -> bool:
        return bool(set(self.roles) & set(roles))


class ExternalAuthenticationResult(BaseModel):
    """Provider-neutral identity assertion before canonical membership lookup."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: str = Field(min_length=1)
    issuer: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    email: str | None = None
    groups: list[str] = Field(default_factory=list)
    assurance: AuthenticationAssurance = AuthenticationAssurance.OIDC


class IdentityState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    organizations: list[Organization] = Field(default_factory=list)
    workspaces: list[Workspace] = Field(default_factory=list)
    humans: list[HumanIdentity] = Field(default_factory=list)
    services: list[ServiceIdentity] = Field(default_factory=list)
    teams: list[Team] = Field(default_factory=list)
    memberships: list[Membership] = Field(default_factory=list)
    sessions: list[SessionRecord] = Field(default_factory=list)
    service_tokens: list[ServiceTokenRecord] = Field(default_factory=list)
    recovery_factors: list[RecoveryFactor] = Field(default_factory=list)


class OrganizationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str | None = None
    name: str = Field(min_length=1)


class WorkspaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str | None = None
    organization_id: str = Field(min_length=1)
    name: str = Field(min_length=1)


class HumanIdentityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str | None = None
    display_name: str = Field(min_length=1)
    email: str | None = None


class HumanUserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str | None = None
    display_name: str = Field(min_length=1)
    email: str | None = None
    roles: list[MembershipRole] = Field(default_factory=lambda: [MembershipRole.MEMBER])
    team_ids: list[str] = Field(default_factory=list)
    organization_wide: bool = False

    @model_validator(mode="after")
    def normalize(self) -> "HumanUserCreate":
        if not self.roles:
            raise ValueError("human user requires at least one role")
        self.roles = list(dict.fromkeys(self.roles))
        self.team_ids = list(dict.fromkeys(self.team_ids))
        return self


class ServiceIdentityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str | None = None
    name: str = Field(min_length=1)
    description: str | None = None


class MembershipCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity_id: str
    principal_kind: PrincipalKind
    organization_id: str
    workspace_id: str | None = None
    roles: list[MembershipRole] = Field(default_factory=lambda: [MembershipRole.MEMBER])
    team_ids: list[str] = Field(default_factory=list)


class MembershipUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roles: list[MembershipRole] | None = None
    team_ids: list[str] | None = None

    @model_validator(mode="after")
    def require_change(self) -> "MembershipUpdate":
        if self.roles is None and self.team_ids is None:
            raise ValueError("membership update requires roles or team_ids")
        if self.roles is not None and not self.roles:
            raise ValueError("membership requires at least one role")
        return self


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity_id: str
    organization_id: str
    workspace_id: str
    assurance: AuthenticationAssurance = AuthenticationAssurance.PRIMARY
    idle_seconds: int = Field(default=3600, ge=60, le=86400)
    absolute_seconds: int = Field(default=43200, ge=300, le=604800)


class SessionRefresh(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    refresh_token: str = Field(min_length=20)


class ServiceTokenCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service_identity_id: str
    organization_id: str
    workspace_id: str
    scopes: list[str] = Field(default_factory=list)
    expires_at: float | None = None


class StepUpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    verified: bool
    duration_seconds: int = Field(default=900, ge=60, le=3600)


class ExternalIdentityLinkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity_id: str
    provider: str
    issuer: str
    subject: str
    email: str | None = None


class ExternalLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    credentials: dict[str, Any]
    organization_id: str
    workspace_id: str
