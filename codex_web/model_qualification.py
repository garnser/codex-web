from __future__ import annotations

import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.agent_runtime_usage import UsageResourceSource


MODEL_WORKLOAD_CLASSES = (
    "lightweight",
    "primary-coding",
    "coding-deep",
    "high-reasoning",
    "architecture",
    "strategic",
    "operator",
    "research",
    "synthesis",
    "critic",
)


class ModelQualificationStatus(StrEnum):
    DISCOVERED = "discovered"
    METADATA_VALID = "metadata-valid"
    CANDIDATE = "candidate"
    QUALIFIED = "qualified"
    CANARY = "canary"
    ACTIVE = "active"
    DEGRADED = "degraded"
    RESTRICTED = "restricted"
    RETIRED = "retired"


class ModelRoutingRole(StrEnum):
    PRIMARY = "primary"
    ESCALATION = "escalation"
    CRITIC = "critic"


class WorkloadEvaluationProfileUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    workload_class: str = Field(min_length=1)
    metrics: tuple[str, ...] = ()
    minimum_quality_score: float = Field(default=0.0, ge=0.0, le=1.0)
    maximum_cost_per_successful_outcome_usd: float | None = Field(
        default=None,
        gt=0.0,
    )
    required_suite_ids: tuple[str, ...] = ()
    require_provider_reported_cost: bool = False
    require_canary: bool = False

    @model_validator(mode="after")
    def normalize(self) -> "WorkloadEvaluationProfileUpsert":
        if self.workload_class not in MODEL_WORKLOAD_CLASSES:
            raise ValueError(f"unsupported workload class: {self.workload_class}")
        self.metrics = tuple(dict.fromkeys(item for item in self.metrics if item))
        self.required_suite_ids = tuple(
            dict.fromkeys(item for item in self.required_suite_ids if item)
        )
        return self


class WorkloadEvaluationProfile(WorkloadEvaluationProfileUpsert):
    model_config = ConfigDict(extra="forbid")

    organization_id: str
    workspace_id: str
    revision: int = Field(ge=1)
    created_by: str
    created_at: float = Field(default_factory=time.time)


class ModelQualificationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    model_id: str = Field(min_length=1)
    workload_class: str = Field(min_length=1)
    status: ModelQualificationStatus
    evaluation_profile_revision: int = Field(ge=1)
    evaluation_run_ids: tuple[str, ...] = ()
    quality_score: float | None = Field(default=None, ge=0.0, le=1.0)
    cost_per_successful_outcome_usd: float | None = Field(default=None, ge=0.0)
    cost_source: UsageResourceSource | None = None
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_evidence(self) -> "ModelQualificationUpdate":
        if self.workload_class not in MODEL_WORKLOAD_CLASSES:
            raise ValueError(f"unsupported workload class: {self.workload_class}")
        self.evaluation_run_ids = tuple(
            dict.fromkeys(item for item in self.evaluation_run_ids if item)
        )
        if self.status in {
            ModelQualificationStatus.QUALIFIED,
            ModelQualificationStatus.CANARY,
            ModelQualificationStatus.ACTIVE,
        } and not self.evaluation_run_ids:
            raise ValueError("qualified/canary/active status requires evaluation runs")
        return self


class ModelQualificationRevision(ModelQualificationUpdate):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"model-qualification-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    revision: int = Field(ge=1)
    provider_id: str
    model_version: str | None = None
    previous_revision_id: str | None = None
    created_by: str
    created_at: float = Field(default_factory=time.time)


class ModelRoutingDefinitionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    mapping_id: str = Field(min_length=1)
    function_id: str = Field(min_length=1)
    workload_class: str = Field(min_length=1)
    primary_model_ids: tuple[str, ...] = ()
    escalation_model_ids: tuple[str, ...] = ()
    critic_model_ids: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    reasoning_effort: str | None = None
    max_cost_per_invocation_usd: float | None = Field(default=None, gt=0.0)
    latency_preferences: tuple[str, ...] = ()
    allow_fallback: bool = True
    qualification_revision: str = Field(min_length=1)
    evaluated_at: float
    effective_at: float = Field(default_factory=time.time)
    source: str = Field(default="operator", min_length=1)

    @model_validator(mode="after")
    def normalize(self) -> "ModelRoutingDefinitionCreate":
        if self.workload_class not in MODEL_WORKLOAD_CLASSES:
            raise ValueError(f"unsupported workload class: {self.workload_class}")
        for field in (
            "primary_model_ids",
            "escalation_model_ids",
            "critic_model_ids",
            "required_capabilities",
            "latency_preferences",
        ):
            setattr(self, field, tuple(dict.fromkeys(item for item in getattr(self, field) if item)))
        if not self.primary_model_ids:
            raise ValueError("routing definition requires a primary model")
        return self


class ModelRoutingDefinitionRevision(ModelRoutingDefinitionCreate):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"model-routing-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    revision: int = Field(ge=1)
    previous_revision_id: str | None = None
    previous_known_good_revision_id: str | None = None
    active: bool = True
    created_by: str
    created_at: float = Field(default_factory=time.time)


class ModelRoutingRollback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)
