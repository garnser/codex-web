from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from codex_web.artifact_evidence import EvidenceRequirement
from codex_web.execution_workspaces import ExecutionWorkspaceReference


class WorkItemRetryPolicy(BaseModel):
    """Deterministic retry bounds for one canonical work item."""

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=3, ge=1, le=100)
    backoff_seconds: float = Field(default=0.0, ge=0.0)


class WorkItemRetryState(BaseModel):
    """Current retry position plus the policy that bounds it."""

    model_config = ConfigDict(extra="forbid")

    attempt: int = Field(default=0, ge=0)
    policy: WorkItemRetryPolicy = Field(default_factory=WorkItemRetryPolicy)
    last_retry_at: float | None = None


class WorkItemFailureReason(BaseModel):
    """Structured failure classification; category/code remain extensible data."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    category: str = Field(min_length=1)
    code: str | None = None
    message: str = Field(min_length=1)
    retryable: bool | None = None
    recorded_at: float


class WorkItemExecutionCheckpoint(BaseModel):
    """Compact resumable state for long-running work."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    created_at: float
    actor: str | None = None
    source: str | None = None
    reason: str | None = None
    summary: str = Field(min_length=1)
    objective: str | None = None
    current_state: str | None = None
    important_decisions: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


class WorkItemUsageAttribution(BaseModel):
    """Cumulative model/accounting totals attributed to canonical work."""

    model_config = ConfigDict(extra="forbid")

    calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float = Field(default=0.0, ge=0.0)
    goal_id: str | None = None
    decision_id: str | None = None


class WorkItemExecutionLifecycle(BaseModel):
    """Execution metadata embedded in the canonical WorkItemState."""

    model_config = ConfigDict(extra="forbid")

    retry: WorkItemRetryState = Field(default_factory=WorkItemRetryState)
    timeout_seconds: float | None = Field(default=None, gt=0.0)
    deadline_at: float | None = None
    failure_reason: WorkItemFailureReason | None = None
    latest_checkpoint: WorkItemExecutionCheckpoint | None = None
    checkpoint_history: list[WorkItemExecutionCheckpoint] = Field(default_factory=list)
    usage: WorkItemUsageAttribution = Field(default_factory=WorkItemUsageAttribution)
    workspace: ExecutionWorkspaceReference | None = None
    evidence_requirements: list[EvidenceRequirement] = Field(default_factory=list)


class WorkItemExecutionUpdate(BaseModel):
    """Partial deterministic update of retry/deadline/failure metadata."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    actor: str | None = None
    source: str | None = None
    reason: str | None = None
    retry_attempt: int | None = Field(default=None, ge=0)
    retry_max_attempts: int | None = Field(default=None, ge=1, le=100)
    retry_backoff_seconds: float | None = Field(default=None, ge=0.0)
    timeout_seconds: float | None = Field(default=None, gt=0.0)
    deadline_at: float | None = None
    failure_category: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    failure_retryable: bool | None = None
    clear_failure: bool = False


class WorkItemCheckpointCreate(BaseModel):
    """Checkpoint payload intentionally contains only minimum resumable context."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    actor: str | None = None
    source: str | None = None
    reason: str | None = None
    summary: str = Field(min_length=1)
    objective: str | None = None
    current_state: str | None = None
    important_decisions: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


class WorkItemUsageRecord(BaseModel):
    """One additive model/accounting record for a work item."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    actor: str | None = None
    source: str | None = None
    reason: str | None = None
    provider: str | None = None
    model: str | None = None
    role: str | None = None
    calls: int = Field(default=1, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float = Field(default=0.0, ge=0.0)
    goal_id: str | None = None
    decision_id: str | None = None
