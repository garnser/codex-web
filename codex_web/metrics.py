from __future__ import annotations

import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec


METRIC_CONTRACT = ContractSpec("metric-state", "1.0", ("1.0",))


class MetricValueType(StrEnum):
    NUMBER = "number"
    INTEGER = "integer"
    BOOLEAN = "boolean"


class MetricAggregation(StrEnum):
    LAST = "last"
    SUM = "sum"
    AVERAGE = "average"
    MINIMUM = "minimum"
    MAXIMUM = "maximum"
    COUNT = "count"


class MetricDirection(StrEnum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"
    NEUTRAL = "neutral"


class MetricFreshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    MISSING = "missing"
    PARTIAL = "partial"


class MetricThresholdOperator(StrEnum):
    EQ = "eq"
    GTE = "gte"
    LTE = "lte"


class MetricThreshold(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    label: str = Field(min_length=1)
    operator: MetricThresholdOperator
    value: float | int | bool


class MetricDefinitionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    owner_identity_id: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    value_type: MetricValueType = MetricValueType.NUMBER
    aggregation: MetricAggregation = MetricAggregation.LAST
    window_seconds: int | None = Field(default=None, ge=1)
    freshness_seconds: int = Field(default=3600, ge=1)
    direction: MetricDirection = MetricDirection.NEUTRAL
    source_requirements: tuple[str, ...] = ()
    thresholds: tuple[MetricThreshold, ...] = ()
    project_id: str | None = None
    resource_id: str | None = None


class MetricDefinitionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1)
    description: str | None = Field(default=None, min_length=1)
    owner_identity_id: str | None = Field(default=None, min_length=1)
    unit: str | None = Field(default=None, min_length=1)
    value_type: MetricValueType | None = None
    aggregation: MetricAggregation | None = None
    window_seconds: int | None = Field(default=None, ge=1)
    freshness_seconds: int | None = Field(default=None, ge=1)
    direction: MetricDirection | None = None
    source_requirements: tuple[str, ...] | None = None
    thresholds: tuple[MetricThreshold, ...] | None = None
    project_id: str | None = None
    resource_id: str | None = None
    reason: str = Field(min_length=1)


class MetricDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"metric-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    key: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    owner_identity_id: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    value_type: MetricValueType
    aggregation: MetricAggregation
    window_seconds: int | None = Field(default=None, ge=1)
    freshness_seconds: int = Field(ge=1)
    direction: MetricDirection
    source_requirements: tuple[str, ...] = ()
    thresholds: tuple[MetricThreshold, ...] = ()
    project_id: str | None = None
    resource_id: str | None = None
    revision: int = Field(default=1, ge=1)
    created_by: str = Field(min_length=1)
    updated_by: str = Field(min_length=1)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def normalize(self) -> "MetricDefinition":
        self.source_requirements = tuple(dict.fromkeys(self.source_requirements))
        if self.value_type == MetricValueType.BOOLEAN and self.aggregation not in {
            MetricAggregation.LAST,
            MetricAggregation.COUNT,
        }:
            raise ValueError("boolean metrics only support last or count aggregation")
        return self


class MetricDefinitionRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_id: str
    revision: int = Field(ge=1)
    snapshot: MetricDefinition
    reason: str
    revised_by: str
    revised_at: float = Field(default_factory=time.time)


class MetricObservationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    value: float | int | bool
    unit: str | None = None
    observed_at: float | None = None
    window_start: float | None = None
    window_end: float | None = None
    source: str = Field(min_length=1)
    provider: str | None = None
    external_record_ref: str | None = None
    evidence_ids: tuple[str, ...] = ()
    partial: bool = False
    idempotency_key: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_window(self) -> "MetricObservationCreate":
        if self.window_start is not None and self.window_end is not None:
            if self.window_end < self.window_start:
                raise ValueError("metric observation window_end must be >= window_start")
        return self


class MetricObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"metric-observation-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    metric_id: str
    metric_revision: int = Field(ge=1)
    value: float | int | bool
    unit: str
    observed_at: float
    window_start: float | None = None
    window_end: float | None = None
    source: str
    provider: str | None = None
    external_record_ref: str | None = None
    evidence_ids: tuple[str, ...] = ()
    partial: bool = False
    idempotency_key: str
    ingested_by: str
    ingested_at: float = Field(default_factory=time.time)


class MetricEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_id: str
    metric_revision: int = Field(ge=1)
    observation_ids: tuple[str, ...] = ()
    window_start: float | None = None
    window_end: float | None = None
    aggregation: MetricAggregation
    value: float | int | bool | None = None
    unit: str
    freshness: MetricFreshness
    freshness_reason: str
    newest_observation_at: float | None = None
    evaluated_at: float = Field(default_factory=time.time)


class MetricSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"metric-snapshot-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    metric_id: str
    metric_revision: int = Field(ge=1)
    observation_ids: tuple[str, ...] = ()
    window_start: float | None = None
    window_end: float | None = None
    aggregation: MetricAggregation
    value: float | int | bool | None = None
    unit: str
    freshness: MetricFreshness
    freshness_reason: str
    newest_observation_at: float | None = None
    captured_by: str
    captured_at: float = Field(default_factory=time.time)


class MetricSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_start: float | None = None
    window_end: float | None = None

    @model_validator(mode="after")
    def validate_window(self) -> "MetricSnapshotRequest":
        if self.window_start is not None and self.window_end is not None:
            if self.window_end < self.window_start:
                raise ValueError("metric snapshot window_end must be >= window_start")
        return self


class MetricState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = METRIC_CONTRACT.current
    definitions: list[MetricDefinition] = Field(default_factory=list)
    revisions: list[MetricDefinitionRevision] = Field(default_factory=list)
    observations: list[MetricObservation] = Field(default_factory=list)
    snapshots: list[MetricSnapshot] = Field(default_factory=list)
