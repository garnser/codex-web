from __future__ import annotations

import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec


UX_TELEMETRY_CONTRACT = ContractSpec("ux-telemetry-state", "1.0", ("1.0",))
UX_TELEMETRY_TAXONOMY_VERSION = "1.0"


class UxEventName(StrEnum):
    ONBOARDING_STARTED = "onboarding_started"
    ONBOARDING_STEP_COMPLETED = "onboarding_step_completed"
    ONBOARDING_COMPLETED = "onboarding_completed"
    ONBOARDING_SKIPPED = "onboarding_skipped"
    WORKFLOW_STARTED = "workflow_started"
    WORKFLOW_COMPLETED = "workflow_completed"
    WORKFLOW_ABANDONED = "workflow_abandoned"
    VALIDATION_ERROR_SHOWN = "validation_error_shown"
    ACTION_STARTED = "action_started"
    ACTION_SUCCEEDED = "action_succeeded"
    ACTION_FAILED = "action_failed"
    RECOVERY_ACTION_USED = "recovery_action_used"
    ROUTE_TRANSITION = "route_transition"
    CONTEXTUAL_HELP_USED = "contextual_help_used"
    EMPTY_STATE_CTA_USED = "empty_state_cta_used"
    FEATURE_DISCOVERY_USED = "feature_discovery_used"


class UxWorkflow(StrEnum):
    ONBOARDING = "onboarding"
    PROJECT_SETUP = "project_setup"
    AUTOMATION = "automation"
    AGENT_MANAGEMENT = "agent_management"
    THREAD_EXECUTION = "thread_execution"
    NAVIGATION = "navigation"


class UxRouteGroup(StrEnum):
    PROJECT_OVERVIEW = "project_overview"
    PROJECT_WORK = "project_work"
    PROJECT_THREADS = "project_threads"
    PROJECT_AGENTS = "project_agents"
    PROJECT_AUTOMATION = "project_automation"
    PROJECT_OPERATIONS = "project_operations"
    PROJECT_SETUP = "project_setup"
    ADMINISTRATION = "administration"
    OTHER = "other"


WORKFLOW_STEPS: dict[UxWorkflow, frozenset[str]] = {
    UxWorkflow.ONBOARDING: frozenset({
        "execution_ready",
        "first_workflow_started",
        "first_useful_outcome",
    }),
    UxWorkflow.PROJECT_SETUP: frozenset({
        "preflight",
        "build_plan",
        "apply_plan",
        "retry_setup",
    }),
    UxWorkflow.AUTOMATION: frozenset({
        "save_definition",
        "run_now",
    }),
    UxWorkflow.AGENT_MANAGEMENT: frozenset({
        "create_profile",
        "edit_profile",
        "create_team",
        "edit_team",
        "profile_lifecycle",
        "team_lifecycle",
    }),
    UxWorkflow.THREAD_EXECUTION: frozenset({
        "send_turn",
        "retry_turn",
    }),
    UxWorkflow.NAVIGATION: frozenset({
        "route",
        "contextual_help",
        "empty_state_cta",
        "feature_discovery",
    }),
}


class UxTelemetryEventCreate(BaseModel):
    """Closed product-interaction event.

    There is intentionally no arbitrary metadata/payload field. Prompt text,
    repository content, generated content, form values, credentials and secret
    references have no representable field in this contract.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    event_name: UxEventName
    workflow: UxWorkflow
    step: str | None = Field(default=None, min_length=1, max_length=64)
    route_group: UxRouteGroup | None = None
    duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    retry_count: int | None = Field(default=None, ge=0, le=100)
    journey_id: str | None = Field(
        default=None,
        min_length=8,
        max_length=80,
        pattern=r"^[A-Za-z0-9_-]+$",
    )

    @model_validator(mode="after")
    def validate_taxonomy(self) -> "UxTelemetryEventCreate":
        if self.step is not None and self.step not in WORKFLOW_STEPS[self.workflow]:
            raise ValueError(
                f"unsupported UX telemetry step {self.step!r} for workflow {self.workflow.value!r}"
            )
        if self.event_name == UxEventName.ROUTE_TRANSITION:
            if self.workflow != UxWorkflow.NAVIGATION or self.route_group is None:
                raise ValueError("route_transition requires navigation workflow and route_group")
        return self


class UxTelemetryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"ux-{uuid.uuid4().hex}")
    schema_version: str = UX_TELEMETRY_TAXONOMY_VERSION
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    event_name: UxEventName
    workflow: UxWorkflow
    step: str | None = None
    route_group: UxRouteGroup | None = None
    duration_ms: int | None = None
    retry_count: int | None = None
    journey_id: str | None = None
    recorded_at: float = Field(default_factory=time.time)


class UxTelemetryState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = UX_TELEMETRY_CONTRACT.current
    events: list[UxTelemetryEvent] = Field(default_factory=list)
