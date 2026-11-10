from __future__ import annotations

from enum import StrEnum
from string import Formatter

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, model_serializer

from codex_web.timeout_values import validate_timeout

from codex_web.definitions import DefinitionReference


AUTOMATION_KIND = "automation"
AUTOMATION_SCHEMA_VERSION = "1.3"


class AutomationTriggerType(StrEnum):
    RECURRING_SCHEDULE = "recurring_schedule"
    ONE_SHOT_SCHEDULE = "one_shot_schedule"
    CANONICAL_EVENT = "canonical_event"
    PROVIDER_EVENT = "provider_event"
    MANUAL = "manual"


class AutomationTargetKind(StrEnum):
    AGENT_PROFILE = "agent_profile"
    TEAM = "team"
    HOME_OPERATOR = "home_operator"
    CANONICAL_PROCEDURE = "canonical_procedure"


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
    home_thread_id: str | None = Field(default=None, min_length=1, max_length=200)
    home_binding_revision: int | None = Field(default=None, ge=1)
    agent_profile_revision: int | None = Field(default=None, ge=1)
    procedure_ref: DefinitionReference | None = None
    supervisor_role: str | None = Field(default=None, min_length=1, max_length=160)
    max_advances: int | None = Field(default=None, ge=1, le=64)

    @model_serializer(mode="wrap")
    def omit_home_pins(self, handler):
        result = handler(self)
        if self.kind != AutomationTargetKind.HOME_OPERATOR:
            for key in ('home_thread_id', 'home_binding_revision', 'agent_profile_revision'):
                result.pop(key, None)
        if self.kind != AutomationTargetKind.CANONICAL_PROCEDURE:
            for key in ("procedure_ref", "supervisor_role", "max_advances"):
                result.pop(key, None)
        return result

    @model_validator(mode="after")
    def home_selection(self):
        pins = (self.home_thread_id, self.home_binding_revision, self.agent_profile_revision)
        if self.kind == AutomationTargetKind.HOME_OPERATOR:
            if not all(value is not None for value in pins):
                raise ValueError("Home follow-up requires exact thread, selection and Agent Profile revisions")
        elif any(value is not None for value in pins):
            raise ValueError("Home selection pins require the Home operator target")
        procedure_fields = (self.procedure_ref, self.supervisor_role, self.max_advances)
        if self.kind == AutomationTargetKind.CANONICAL_PROCEDURE:
            if (any(v is None for v in procedure_fields) or self.procedure_ref.kind != 'agent.skill'
                    or self.procedure_ref.definition_id != self.id):
                raise ValueError('canonical procedure target requires exact Skill, supervisor Role and advance bound')
        elif any(v is not None for v in procedure_fields):
            raise ValueError('procedure target fields require canonical_procedure')
        return self


class AutomationBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_input_tokens: int | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, ge=0)
    max_cost_usd: float | None = Field(default=None, ge=0)
    max_duration_seconds: int | None = Field(default=None, ge=-1)

    @field_validator('max_duration_seconds')
    @classmethod
    def duration_value(cls, value):
        return validate_timeout(value) if value is not None else None
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
        if self.target.kind == AutomationTargetKind.HOME_OPERATOR:
            if (self.trigger.type not in {AutomationTriggerType.CANONICAL_EVENT, AutomationTriggerType.PROVIDER_EVENT}
                    or self.retry.max_attempts != 1 or self.budget.max_concurrency != 1
                    or self.work_item_policy != "reuse_only" or not self.owner_identity_id
                    or self.budget.max_input_tokens is None or self.budget.max_output_tokens is None
                    or self.budget.max_duration_seconds is None
                    or self.budget.max_input_tokens <= 0 or self.budget.max_output_tokens <= 0):
                raise ValueError("Home event follow-up requires an owner, event trigger, declared time budget (0 or -1 disables), finite token budgets, reuse_only and one attempt/concurrency")
        if self.target.kind == AutomationTargetKind.CANONICAL_PROCEDURE:
            if (self.trigger.type != AutomationTriggerType.CANONICAL_EVENT
                    or self.trigger.event_type != 'work.transition'
                    or self.retry.max_attempts != 1 or self.budget.max_concurrency != 1
                    or self.work_item_policy != 'reuse_only' or not self.owner_identity_id
                    or self.budget.max_duration_seconds is None
                    or (self.budget.max_input_tokens, self.budget.max_output_tokens, self.budget.max_cost_usd) != (0, 0, 0)):
                raise ValueError('canonical procedure requires Work events, owner, reuse_only, zero models and declared single-run bounds (0 or -1 disables elapsed time)')
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
