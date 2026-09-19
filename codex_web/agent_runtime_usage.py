from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec


AGENT_RUNTIME_USAGE_STATE_CONTRACT = ContractSpec(
    "agent-runtime-usage-state",
    "1.0",
    ("1.0",),
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
