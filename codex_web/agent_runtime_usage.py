from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec


AGENT_RUNTIME_USAGE_STATE_CONTRACT = ContractSpec(
    "agent-runtime-usage-state",
    "1.1",
    ("1.0", "1.1"),
)


class RuntimeTelemetryCompleteness(StrEnum):
    EXACT = "exact"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class RuntimeTerminalOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"


class UsageResourceKind(StrEnum):
    TOKEN_USAGE = "token_usage"
    TOKEN_QUOTA = "token_quota"
    MONEY = "money"
    REQUEST_QUOTA = "request_quota"
    RATE_LIMIT = "rate_limit"
    COMPUTE = "compute"
    OTHER = "other"


class UsageResourceSource(StrEnum):
    PROVIDER_REPORTED = "provider_reported"
    CODEX_CALCULATED = "codex_calculated"


class UsageMeasurementMode(StrEnum):
    """Whether observations add usage or replace a point-in-time snapshot."""

    INCREMENT = "increment"
    SNAPSHOT = "snapshot"


class UsageResource(BaseModel):
    """Provider-neutral usage, balance, budget, or quota observation.

    A missing limit is meaningful: callers must present the absolute value and
    must not invent a percentage. Scope and provenance are copied into every
    observation so historical records keep their original accounting meaning.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    resource_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    kind: UsageResourceKind
    consumed: float | None = Field(default=None, ge=0.0)
    limit: float | None = Field(default=None, ge=0.0)
    remaining: float | None = Field(default=None, ge=0.0)
    unit: str = Field(min_length=1)
    currency: str | None = None
    period: str | None = None
    period_started_at: float | None = None
    reset_at: float | None = None
    source: UsageResourceSource
    authoritative: bool = False
    measurement_mode: UsageMeasurementMode = UsageMeasurementMode.SNAPSHOT
    provider_id: str = Field(min_length=1)
    runtime_id: str = Field(min_length=1)
    account_id: str | None = None
    model_id: str | None = None
    model_version: str | None = None
    pricing_revision: str | None = None
    observed_at: float = Field(default_factory=time.time)
    freshness_seconds: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def validate_semantics(self) -> "UsageResource":
        self.unit = self.unit.casefold()
        if self.kind == UsageResourceKind.MONEY:
            if not self.currency:
                raise ValueError("money usage requires a currency")
            self.currency = self.currency.upper()
        elif self.currency is not None:
            raise ValueError("currency is only valid for money usage")
        if self.consumed is None and self.limit is None and self.remaining is None:
            raise ValueError("usage resource requires a measured value")
        if self.source == UsageResourceSource.CODEX_CALCULATED:
            if self.kind == UsageResourceKind.MONEY and not (
                self.model_id and self.pricing_revision
            ):
                raise ValueError(
                    "calculated money usage requires model and pricing revisions"
                )
            self.authoritative = False
        return self

    @property
    def utilization_percent(self) -> float | None:
        if (
            not self.authoritative
            or self.limit is None
            or self.limit <= 0
            or self.consumed is None
        ):
            return None
        return min(100.0, max(0.0, self.consumed / self.limit * 100.0))

    def accounting_key(self) -> tuple[str | None, ...]:
        return (
            self.provider_id,
            self.account_id,
            self.runtime_id,
            self.model_id,
            self.model_version,
            self.resource_id,
            self.kind.value,
            self.unit,
            self.currency,
            self.period,
            str(self.period_started_at),
            str(self.reset_at),
        )


class AgentRuntimeUsage(BaseModel):
    """Compact canonical usage/evidence projection for one runtime turn/session.

    Provider transcripts are deliberately excluded. Unknown measurements remain
    null and completeness states how much the provider actually exposed.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"runtime-usage-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    project_id: str | None = None
    goal_id: str | None = None
    work_item_ref: str | None = None
    decision_id: str | None = None
    execution_id: str | None = None
    assignment_id: str | None = None
    execution_workspace_id: str | None = None
    agent_session_id: str | None = None

    provider_id: str = Field(min_length=1)
    runtime_id: str = Field(min_length=1)
    runtime_type: str = Field(min_length=1)
    capability_revision: int = Field(default=1, ge=1)
    provider_native_session_id: str | None = None
    provider_native_turn_id: str | None = None
    provider_request_ids: tuple[str, ...] = ()
    observed_model_ids: tuple[str, ...] = ()
    runtime_version: str | None = None

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    cache_write_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0.0)
    runtime_duration_seconds: float | None = Field(default=None, ge=0.0)
    resources: tuple[UsageResource, ...] = ()

    tool_call_count: int = Field(default=0, ge=0)
    shell_command_count: int = Field(default=0, ge=0)
    file_edit_count: int = Field(default=0, ge=0)
    git_operation_count: int = Field(default=0, ge=0)
    subagent_count: int | None = Field(default=None, ge=0)
    model_call_count: int | None = Field(default=None, ge=0)
    context_compaction_count: int = Field(default=0, ge=0)

    telemetry_completeness: RuntimeTelemetryCompleteness = (
        RuntimeTelemetryCompleteness.UNAVAILABLE
    )
    terminal_outcome: RuntimeTerminalOutcome = RuntimeTerminalOutcome.UNKNOWN
    started_at: float | None = None
    completed_at: float | None = None
    observed_at: float = Field(default_factory=time.time)
    evidence_ids: tuple[str, ...] = ()
    event_fingerprints: tuple[str, ...] = ()

    @model_validator(mode="after")
    def normalize(self) -> "AgentRuntimeUsage":
        self.provider_request_ids = tuple(
            dict.fromkeys(item for item in self.provider_request_ids if item)
        )
        self.observed_model_ids = tuple(
            dict.fromkeys(item for item in self.observed_model_ids if item)
        )
        self.evidence_ids = tuple(
            dict.fromkeys(item for item in self.evidence_ids if item)
        )
        self.event_fingerprints = tuple(
            dict.fromkeys(item for item in self.event_fingerprints if item)
        )[-256:]
        return self


class AgentRuntimeUsageState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = AGENT_RUNTIME_USAGE_STATE_CONTRACT.current
    records: list[AgentRuntimeUsage] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        AGENT_RUNTIME_USAGE_STATE_CONTRACT.require(self.schema_version)
