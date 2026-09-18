from __future__ import annotations

import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec


ENTITLEMENT_CONTRACT = ContractSpec("entitlement-state", "1.0", ("1.0",))


class EntitlementMode(StrEnum):
    SELF_HOSTED_UNLIMITED = "self_hosted_unlimited"
    ENFORCED = "enforced"


class QuotaBehavior(StrEnum):
    HARD_STOP = "hard_stop"
    DEGRADED = "degraded"
    GRACE = "grace"
    NOTIFY = "notify"


class QuotaWindow(StrEnum):
    LIFETIME = "lifetime"
    HOUR = "hour"
    DAY = "day"
    MONTH = "month"


class TenantEntitlementSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    mode: EntitlementMode = EntitlementMode.SELF_HOSTED_UNLIMITED
    updated_by: str = Field(min_length=1)
    updated_at: float = Field(default_factory=time.time)


class CapabilityEntitlement(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"entitlement-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    enabled: bool = True
    source: str = Field(default="manual", min_length=1)
    starts_at: float | None = None
    expires_at: float | None = None
    updated_by: str = Field(min_length=1)
    updated_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def validate_window(self) -> "CapabilityEntitlement":
        if (
            self.starts_at is not None
            and self.expires_at is not None
            and self.expires_at <= self.starts_at
        ):
            raise ValueError("entitlement expires_at must be after starts_at")
        return self


class CapabilityEntitlementUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool = True
    source: str = Field(default="manual", min_length=1)
    starts_at: float | None = None
    expires_at: float | None = None


class QuotaPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"quota-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    limit: float = Field(ge=0.0)
    window: QuotaWindow = QuotaWindow.MONTH
    behavior: QuotaBehavior = QuotaBehavior.HARD_STOP
    warning_fraction: float = Field(default=0.8, ge=0.0, le=1.0)
    source: str = Field(default="manual", min_length=1)
    updated_by: str = Field(min_length=1)
    updated_at: float = Field(default_factory=time.time)


class QuotaPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    limit: float = Field(ge=0.0)
    window: QuotaWindow = QuotaWindow.MONTH
    behavior: QuotaBehavior = QuotaBehavior.HARD_STOP
    warning_fraction: float = Field(default=0.8, ge=0.0, le=1.0)
    source: str = Field(default="manual", min_length=1)


class UsageEventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    amount: float = Field(gt=0.0)
    occurred_at: float = Field(default_factory=time.time)
    source: str = Field(default="runtime", min_length=1)
    project_id: str | None = None
    resource_id: str | None = None
    work_item_ref: str | None = None
    action_intent_id: str | None = None


class UsageEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"usage-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    amount: float = Field(gt=0.0)
    occurred_at: float
    received_at: float = Field(default_factory=time.time)
    source: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    project_id: str | None = None
    resource_id: str | None = None
    work_item_ref: str | None = None
    action_intent_id: str | None = None


class QuotaStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metric: str
    limit: float
    usage: float
    projected_usage: float
    remaining: float
    window: QuotaWindow
    window_start: float | None = None
    window_end: float | None = None
    behavior: QuotaBehavior
    warning: bool
    exceeded: bool


class EntitlementDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    mode: EntitlementMode
    capability: str
    reason: str
    quota: QuotaStatus | None = None


class UsageRecordResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event: UsageEvent
    duplicate: bool
    decision: EntitlementDecision | None = None


class TenantModeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: EntitlementMode


class EntitlementState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = ENTITLEMENT_CONTRACT.current
    settings: list[TenantEntitlementSettings] = Field(default_factory=list)
    capabilities: list[CapabilityEntitlement] = Field(default_factory=list)
    quotas: list[QuotaPolicy] = Field(default_factory=list)
    usage: list[UsageEvent] = Field(default_factory=list)
