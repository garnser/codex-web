"""Offline rubric arithmetic. Attribution inputs are not human authentication."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.definitions import DefinitionReference


RUBRIC_KIND = "evaluation-rubric"
RUBRIC_SCHEMA_VERSION = "1.0"


class RubricModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True,
                              allow_inf_nan=False)


class RubricCriterion(RubricModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    description: str = Field(min_length=1, max_length=2000)
    minimum: float = Field(default=0, ge=-1000000, le=1000000)
    maximum: float = Field(default=4, ge=-1000000, le=1000000)
    weight: float = Field(default=1, gt=0, le=1000000)
    minimum_normalized_score: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def ordered_scale(self) -> RubricCriterion:
        if self.maximum <= self.minimum:
            raise ValueError("criterion maximum must exceed minimum")
        return self


class EvaluationRubric(RubricModel):
    title: str = Field(min_length=1, max_length=200)
    workload: str = Field(min_length=1, max_length=100)
    criteria: tuple[RubricCriterion, ...] = Field(min_length=1, max_length=32)
    minimum_score: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def unique_criteria(self) -> EvaluationRubric:
        if len({item.id for item in self.criteria}) != len(self.criteria):
            raise ValueError("rubric criterion ids must be unique")
        return self


class CriterionRating(RubricModel):
    criterion_id: str = Field(min_length=1, max_length=80)
    value: float
    finding: str = Field(min_length=1, max_length=2000)


class RubricScoreInput(RubricModel):
    rubric: DefinitionReference
    organization_id: str = Field(min_length=1, max_length=200)
    workspace_id: str = Field(min_length=1, max_length=200)
    project_id: str | None = Field(default=None, min_length=1, max_length=200)
    task_ref: str = Field(min_length=1, max_length=200)
    artifact_ref: str = Field(min_length=1, max_length=200)
    artifact_revision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    annotation_ref: str = Field(min_length=1, max_length=200)
    rater_ref: str = Field(min_length=1, max_length=200)
    ratings: tuple[CriterionRating, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def unique_ratings(self) -> RubricScoreInput:
        if len({item.criterion_id for item in self.ratings}) != len(self.ratings):
            raise ValueError("criterion ratings must be unique")
        return self


class CriterionScore(RubricModel):
    criterion_id: str
    normalized_score: float | None
    minimum_normalized_score: float | None
    passed: bool | None
    finding: str | None


class RubricScoreResult(RubricModel):
    provenance: RubricScoreInput
    workload: str
    criteria: tuple[CriterionScore, ...]
    complete: bool
    score: float | None
    minimum_score: float
    passed: bool | None


class RubricScoreComparison(RubricModel):
    baseline: RubricScoreResult
    candidate: RubricScoreResult
    score_delta: float | None
    criterion_deltas: dict[str, float | None]
