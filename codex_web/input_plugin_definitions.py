from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.input_plugins import InputFailurePolicy, InputPhase


INPUT_PIPELINE_DEFINITION_ID = "input-pipeline.default"
INPUT_PIPELINE_DEFINITION_KIND = "input_pipeline"
INPUT_PIPELINE_SCHEMA_VERSION = "1.0"
MAX_INPUT_PIPELINE_PLUGINS = 32
MAX_INPUT_PLUGIN_SETTINGS = 32


class InputPluginConditionDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    purposes: tuple[str, ...] = ()
    model_classes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def normalize(self) -> "InputPluginConditionDefinition":
        self.purposes = tuple(sorted({value for value in self.purposes if value}))
        self.model_classes = tuple(
            sorted({value for value in self.model_classes if value})
        )
        return self

    def matches(self, *, purpose: str, model_class: str) -> bool:
        if self.purposes and purpose not in self.purposes:
            return False
        if self.model_classes and model_class not in self.model_classes:
            return False
        return True


class InputPluginRegistrationDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    plugin_id: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    plugin_version: str = Field(min_length=1, max_length=128)
    phase: InputPhase
    order: int = Field(default=100, ge=0, le=1_000_000)
    enabled: bool = True
    failure_policy: InputFailurePolicy = InputFailurePolicy.FAIL_CLOSED
    max_patch_bytes: int = Field(default=32 * 1024, ge=256, le=1024 * 1024)
    max_added_characters: int = Field(default=16_000, ge=0, le=1_000_000)
    conditions: InputPluginConditionDefinition = Field(
        default_factory=InputPluginConditionDefinition
    )
    settings: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def validate_settings(self) -> "InputPluginRegistrationDefinition":
        if len(self.settings) > MAX_INPUT_PLUGIN_SETTINGS:
            raise ValueError(
                f"input plugin settings cannot exceed {MAX_INPUT_PLUGIN_SETTINGS} keys"
            )
        return self


class InputPipelineDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registrations: tuple[InputPluginRegistrationDefinition, ...] = ()

    @model_validator(mode="after")
    def normalize(self) -> "InputPipelineDefinition":
        if len(self.registrations) > MAX_INPUT_PIPELINE_PLUGINS:
            raise ValueError(
                f"input pipeline cannot exceed {MAX_INPUT_PIPELINE_PLUGINS} registrations"
            )
        seen: set[tuple[str, str, InputPhase]] = set()
        for item in self.registrations:
            key = (item.plugin_id, item.plugin_version, item.phase)
            if key in seen:
                raise ValueError(
                    "duplicate input plugin registration for "
                    f"{item.plugin_id}@{item.plugin_version}:{item.phase.value}"
                )
            seen.add(key)
        self.registrations = tuple(
            sorted(
                self.registrations,
                key=lambda item: (
                    tuple(InputPhase).index(item.phase),
                    item.order,
                    item.plugin_id,
                    item.plugin_version,
                ),
            )
        )
        return self


def validate_input_pipeline_definition(payload: dict) -> dict:
    return InputPipelineDefinition.model_validate(payload).model_dump(mode="json")


def input_pipeline_seed_payload() -> dict:
    # Preserve existing model behavior until an operator intentionally publishes
    # a scoped registration.
    return InputPipelineDefinition().model_dump(mode="json")
