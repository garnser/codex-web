from __future__ import annotations

from enum import StrEnum
from string import Formatter

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.definitions import DefinitionReference


AUTOMATION_KIND = "automation"
AUTOMATION_SCHEMA_VERSION = "1.0"


class AutomationTriggerType(StrEnum):
    RECURRING_SCHEDULE = "recurring_schedule"
    ONE_SHOT_SCHEDULE = "one_shot_schedule"
    CANONICAL_EVENT = "canonical_event"
    PROVIDER_EVENT = "provider_event"
    MANUAL = "manual"


class AutomationTargetKind(StrEnum):
    AGENT_PROFILE = "agent_profile"
    TEAM = "team"


class AutomationLifecycle(StrEnum):
    ENABLED = "enabled"
    PAUSED = "paused"


class AutomationTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: AutomationTriggerType
    cron: str | None = None
    timezone: str | None = None
    due_at: float | None = None
    event_type: str | None = None
    event_filter: dict[str, str] = Field(default_factory=dict)
    provider_id: str | None = None

    @model_validator(mode="after")
    def validate_trigger(self) -> "AutomationTrigger":
        if self.type == AutomationTriggerType.RECURRING_SCHEDULE:
            if not self.cron or not self.timezone:
                raise ValueError("recurring schedule requires cron and timezone")
        elif self.type == AutomationTriggerType.ONE_SHOT_SCHEDULE:
            if self.due_at is None:
                raise ValueError("one-shot schedule requires due_at")
        elif self.type in {
            AutomationTriggerType.CANONICAL_EVENT,
            AutomationTriggerType.PROVIDER_EVENT,
        }:
            if not self.event_type:
                raise ValueError("event trigger requires event_type")
            if self.type == AutomationTriggerType.PROVIDER_EVENT and not self.provider_id:
                raise ValueError("provider event trigger requires provider_id")
        return self


class AutomationTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: AutomationTargetKind
    id: str = Field(min_length=1)


class AutomationBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_input_tokens: int | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, ge=0)
    max_cost_usd: float | None = Field(default=None, ge=0)
    max_duration_seconds: int | None = Field(default=None, ge=1)
    max_concurrency: int = Field(default=1, ge=1, le=100)


class AutomationRetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=1, ge=1, le=100)
    backoff_seconds: float = Field(default=0, ge=0)


class AutomationDefinition(BaseModel):
    """Versioned Automation facade over existing scheduler/event/execution primitives."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    lifecycle: AutomationLifecycle = AutomationLifecycle.PAUSED
    trigger: AutomationTrigger
    target: AutomationTarget
    instructions: str = Field(min_length=1, max_length=12000)
    skill_refs: tuple[DefinitionReference, ...] = ()
    execution_profile_ref: DefinitionReference | None = None
    authority_ref: DefinitionReference | None = None
    approval_required: bool = False
    budget: AutomationBudget = Field(default_factory=AutomationBudget)
    retry: AutomationRetryPolicy = Field(default_factory=AutomationRetryPolicy)
    dedupe_key_template: str | None = Field(default=None, max_length=1000)
    work_item_policy: str = Field(default="reuse_or_create", pattern=r"^(reuse_or_create|always_create|reuse_only)$")
    failure_attention: bool = True
    owner_identity_id: str | None = None

    @model_validator(mode="after")
    def validate_automation(self) -> "AutomationDefinition":
        refs = [
            *self.skill_refs,
            *(
                (self.execution_profile_ref,)
                if self.execution_profile_ref is not None
                else ()
            ),
            *((self.authority_ref,) if self.authority_ref is not None else ()),
        ]
        seen: set[tuple[str, str, int]] = set()
        for ref in refs:
            key = (ref.kind, ref.definition_id, ref.revision)
            if key in seen:
                raise ValueError("automation Definition references must be unique")
            seen.add(key)
        if self.dedupe_key_template:
            allowed_fields = {
                "automation",
                "project",
                "occurrence",
                "event",
                "schedule",
                "source",
            }
            for _literal, field_name, format_spec, conversion in Formatter().parse(
                self.dedupe_key_template
            ):
                if not field_name:
                    continue
                if field_name not in allowed_fields:
                    raise ValueError(
                        "automation dedupe_key_template contains unsupported "
                        f"placeholder: {field_name}"
                    )
                if format_spec or conversion:
                    raise ValueError(
                        "automation dedupe_key_template placeholders do not "
                        "support formatting or conversion"
                    )
        return self


def validate_automation_definition(payload: dict[str, object]) -> dict[str, object]:
    return AutomationDefinition.model_validate(payload).model_dump(mode="json")
