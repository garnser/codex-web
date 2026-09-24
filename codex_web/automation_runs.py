from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec
from codex_web.definitions import DefinitionReference


AUTOMATION_RUN_STATE_CONTRACT = ContractSpec(
    "automation-run-state",
    "1.3",
    ("1.0", "1.1", "1.2", "1.3"),
)


class AutomationRunTriggerKind(StrEnum):
    MANUAL = "manual"
    SCHEDULE = "schedule"
    CANONICAL_EVENT = "canonical_event"
    PROVIDER_EVENT = "provider_event"


class AutomationRunStatus(StrEnum):
    ADMITTED = "admitted"
    RUNNING = "running"
    WAITING_FOR_WORK_ITEM = "waiting_for_work_item"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


ACTIVE_AUTOMATION_RUN_STATUSES = frozenset(
    {
        AutomationRunStatus.ADMITTED,
        AutomationRunStatus.RUNNING,
        AutomationRunStatus.WAITING_FOR_WORK_ITEM,
        AutomationRunStatus.WAITING_FOR_APPROVAL,
    }
)


class AutomationRunTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: AutomationRunTriggerKind
    source_id: str = Field(min_length=1, max_length=500)
    occurred_at: float = Field(default_factory=time.time)
    event_id: str | None = None
    schedule_id: str | None = None
    scheduled_for: float | None = None
    correlation_id: str | None = None
    causation_id: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "AutomationRunTrigger":
        if self.kind == AutomationRunTriggerKind.SCHEDULE and not self.schedule_id:
            raise ValueError("schedule Automation trigger requires schedule_id")
        if self.kind in {
            AutomationRunTriggerKind.CANONICAL_EVENT,
            AutomationRunTriggerKind.PROVIDER_EVENT,
        } and not self.event_id:
            raise ValueError("event Automation trigger requires event_id")
        return self


class AutomationRun(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"automation-run-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    project_id: str | None = None
    automation_id: str = Field(min_length=1)
    definition_ref: DefinitionReference
    trigger: AutomationRunTrigger
    dedupe_key: str = Field(min_length=1, max_length=1000)
    status: AutomationRunStatus
    target_kind: str
    target_id: str
    block_code: str | None = None
    block_reason: str | None = None
    result_code: str | None = None
    result_reason: str | None = None
    work_item_ref: str | None = None
    work_item_action_intent_id: str | None = None
    approval_request_id: str | None = None
    execution_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    attempt: int = Field(default=1, ge=1)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None

    @model_validator(mode="after")
    def normalize(self) -> "AutomationRun":
        self.execution_ids = tuple(dict.fromkeys(self.execution_ids))
        self.evidence_ids = tuple(dict.fromkeys(self.evidence_ids))
        return self


class AutomationRunState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = AUTOMATION_RUN_STATE_CONTRACT.current
    runs: list[AutomationRun] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        AUTOMATION_RUN_STATE_CONTRACT.require(self.schema_version)
