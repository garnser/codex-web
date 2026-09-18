from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.identity import TenantScope


class SecretStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class SecretReference(BaseModel):
    """Metadata-only canonical reference. Secret material never serializes here."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"secret-{uuid.uuid4().hex}")
    backend: str = Field(default="local", min_length=1)
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    provider: str | None = None
    purpose: str | None = None
    owner_identity_id: str | None = None
    allowed_identity_ids: list[str] = Field(default_factory=list)
    reveal_identity_ids: list[str] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)
    expires_at: float | None = None
    rotated_at: float | None = None
    rotation: int = Field(default=0, ge=0)
    revoked_at: float | None = None
    revoke_reason: str | None = None

    @model_validator(mode="after")
    def normalize_acl(self) -> "SecretReference":
        self.allowed_identity_ids = sorted(set(self.allowed_identity_ids))
        self.reveal_identity_ids = sorted(set(self.reveal_identity_ids))
        return self

    @property
    def tenant(self) -> TenantScope:
        return TenantScope(
            organization_id=self.organization_id,
            workspace_id=self.workspace_id,
        )

    def status(self, now: float | None = None) -> SecretStatus:
        now = time.time() if now is None else now
        if self.revoked_at is not None:
            return SecretStatus.REVOKED
        if self.expires_at is not None and now >= self.expires_at:
            return SecretStatus.EXPIRED
        return SecretStatus.ACTIVE


class SecretCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1)
    value: str = Field(min_length=1)
    provider: str | None = None
    purpose: str | None = None
    allowed_identity_ids: list[str] = Field(default_factory=list)
    reveal_identity_ids: list[str] = Field(default_factory=list)
    expires_at: float | None = None


class SecretRotate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1)


class SecretAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"secret-audit-{uuid.uuid4().hex}")
    secret_id: str
    organization_id: str
    workspace_id: str
    actor_identity_id: str
    actor_kind: str
    action: str
    outcome: str
    created_at: float = Field(default_factory=time.time)
    reason: str | None = None
    context: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class SecretState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    references: list[SecretReference] = Field(default_factory=list)
    audit: list[SecretAuditEvent] = Field(default_factory=list)
