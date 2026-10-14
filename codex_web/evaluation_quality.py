from pydantic import BaseModel, ConfigDict, Field

from codex_web.definitions import DefinitionReference
from codex_web.evaluation_rubrics import CriterionRating


class EvaluationQualityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    rubric: DefinitionReference
    artifact_id: str = Field(min_length=1, max_length=200)
    artifact_revision: str = Field(min_length=1, max_length=200)
    ratings: tuple[CriterionRating, ...] = Field(default=(), max_length=32)
    idempotency_key: str = Field(min_length=1, max_length=200)


class EvaluationQualityPin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    run_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: str
    artifact_revision: str
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
