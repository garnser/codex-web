from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec


PROVIDER_CAPACITY_CONTRACT = ContractSpec(
    "provider-capacity-state",
    "1.0",
    ("1.0",),
)


class ProviderCapacityStatus(StrEnum):
    AVAILABLE = "available"
    THROTTLED = "throttled"
    DEPLETED = "depleted"
    UNAVAILABLE = "unavailable"


class ProviderCapacityWaitStatus(StrEnum):
    WAITING = "waiting"
    RESUMED = "resumed"
    CANCELLED = "cancelled"


class ProviderCapacityReport(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider_id: str = Field(min_length=1)
    runtime_id: str | None = None
    status: ProviderCapacityStatus
    reason: str | None = None
    retry_at: float | None = None
    source: str = Field(default="runtime", min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    observed_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def validate_retry(self) -> "ProviderCapacityReport":
        if self.runtime_id is not None and not self.runtime_id.strip():
            self.runtime_id = None
        if self.status == ProviderCapacityStatus.AVAILABLE:
            self.retry_at = None
        return self


class ProviderCapacityRecord(ProviderCapacityReport):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    organization_id: str
    workspace_id: str
    consecutive_failures: int = Field(default=0, ge=0)
    last_success_at: float | None = None
    updated_at: float = Field(default_factory=time.time)

    @property
    def key(self) -> str:
        return f"{self.provider_id}/{self.runtime_id or '*'}"

    def blocks(self, now: float) -> bool:
        if self.status == ProviderCapacityStatus.AVAILABLE:
            return False
        if self.retry_at is not None and self.retry_at <= now:
            return False
        return True


class ProviderCapacityWaitCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    thread_id: str | None = None
    work_item_ref: str | None = None
    execution_id: str | None = None
    provider_keys: tuple[str, ...] = ()
    retry_at: float
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_target(self) -> "ProviderCapacityWaitCreate":
        if not any((self.thread_id, self.work_item_ref, self.execution_id)):
            raise ValueError(
                "capacity wait requires thread_id, work_item_ref, or execution_id"
            )
        self.provider_keys = tuple(
            dict.fromkeys(item.strip() for item in self.provider_keys if item.strip())
        )
        return self


class ProviderCapacityWait(ProviderCapacityWaitCreate):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"capacity-wait-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    status: ProviderCapacityWaitStatus = ProviderCapacityWaitStatus.WAITING
    schedule_id: str | None = None
    created_at: float = Field(default_factory=time.time)
    resumed_at: float | None = None
    updated_at: float = Field(default_factory=time.time)


class ProviderCapacityState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = PROVIDER_CAPACITY_CONTRACT.current
    records: list[ProviderCapacityRecord] = Field(default_factory=list)
    waits: list[ProviderCapacityWait] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        PROVIDER_CAPACITY_CONTRACT.require(self.schema_version)
