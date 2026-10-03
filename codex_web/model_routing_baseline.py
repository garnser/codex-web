from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.model_qualification import MODEL_WORKLOAD_CLASSES


MODEL_ROUTING_BASELINE_ID = "model-routing.initial-functional-matrix"
MODEL_ROUTING_BASELINE_KIND = "model-routing-baseline"
MODEL_ROUTING_BASELINE_SCHEMA_VERSION = "1.0"


class ModelRoutingBaselineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    function_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    workload_class: str | None = None
    deterministic: bool = False
    primary_models: tuple[str, ...] = ()
    escalation_models: tuple[str, ...] = ()
    critic_models: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_assignment(self) -> "ModelRoutingBaselineEntry":
        if self.deterministic:
            if self.primary_models:
                raise ValueError("deterministic functions cannot assign a primary model")
        elif self.workload_class not in MODEL_WORKLOAD_CLASSES:
            raise ValueError("modeled functions require a stable workload class")
        return self


class ModelRoutingBaselineDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    evaluation_revision: str = Field(min_length=1)
    evaluated_at: float
    replaceable: bool = True
    entries: tuple[ModelRoutingBaselineEntry, ...]


def validate_model_routing_baseline(payload: dict) -> dict:
    return ModelRoutingBaselineDefinition.model_validate(payload).model_dump(mode="json")
