"""Deterministic scoring with no persistence, identity grants or model calls."""
from __future__ import annotations

import math

from codex_web.definitions import DefinitionRecord, DefinitionScope, reference_for
from codex_web.evaluation_rubrics import (
    RUBRIC_KIND, RUBRIC_SCHEMA_VERSION, CriterionScore, EvaluationRubric,
    RubricScoreComparison, RubricScoreInput, RubricScoreResult,
)
from codex_web.services.definitions import DefinitionKindSchema, DefinitionRegistryService


def validate_evaluation_rubric(payload: dict) -> dict:
    return EvaluationRubric.model_validate(payload).model_dump(mode="json")


def install_evaluation_rubric_schema(registry: DefinitionRegistryService) -> None:
    """Register the interpreter only; never seed or publish a rubric."""
    if not any(item["kind"] == RUBRIC_KIND and item["schema_version"] == RUBRIC_SCHEMA_VERSION
               for item in registry.schemas.metadata()):
        registry.register_schema(DefinitionKindSchema(
            kind=RUBRIC_KIND, schema_version=RUBRIC_SCHEMA_VERSION,
            validate=validate_evaluation_rubric,
        ))


def score_rubric(record: DefinitionRecord, observation: RubricScoreInput) -> RubricScoreResult:
    # Revalidate even model_copy/model_construct inputs and mutable payloads.
    record = DefinitionRecord.model_validate(record.model_dump(mode="python"))
    observation = RubricScoreInput.model_validate(observation.model_dump(mode="python"))
    if record.kind != RUBRIC_KIND or record.definition_schema_version != RUBRIC_SCHEMA_VERSION:
        raise ValueError("unsupported evaluation rubric kind/schema")
    if reference_for(record) != observation.rubric:
        raise ValueError("rubric pin does not match immutable record")
    scope_ids = {
        DefinitionScope.ORGANIZATION: observation.organization_id,
        DefinitionScope.WORKSPACE: observation.workspace_id,
        DefinitionScope.PROJECT: observation.project_id,
    }
    if record.scope_type != DefinitionScope.GLOBAL and scope_ids.get(record.scope_type) != record.scope_id:
        raise ValueError("rubric definition is outside observation scope")
    rubric = EvaluationRubric.model_validate(record.payload)
    ratings = {item.criterion_id: item for item in observation.ratings}
    if set(ratings) - {item.id for item in rubric.criteria}:
        raise ValueError("rating names an unknown rubric criterion")
    findings = []
    weighted_scores = []
    for criterion in rubric.criteria:
        rating = ratings.get(criterion.id)
        normalized = None
        passed = None
        if rating is not None:
            if not criterion.minimum <= rating.value <= criterion.maximum:
                raise ValueError(f"rating outside criterion scale: {criterion.id}")
            normalized = (rating.value - criterion.minimum) / (criterion.maximum - criterion.minimum)
            weighted_scores.append(normalized * criterion.weight)
            if criterion.minimum_normalized_score is not None:
                passed = normalized >= criterion.minimum_normalized_score
        findings.append(CriterionScore(
            criterion_id=criterion.id, normalized_score=normalized,
            minimum_normalized_score=criterion.minimum_normalized_score,
            passed=passed, finding=rating.finding if rating else None,
        ))
    complete = len(ratings) == len(rubric.criteria)
    score = (math.fsum(weighted_scores) / math.fsum(item.weight for item in rubric.criteria)
             if complete else None)
    return RubricScoreResult(
        provenance=observation, workload=rubric.workload, criteria=tuple(findings),
        complete=complete, score=score, minimum_score=rubric.minimum_score,
        passed=(score >= rubric.minimum_score and all(item.passed is not False for item in findings)
                if score is not None else None),
    )


def compare_rubric_scores(
    record: DefinitionRecord, baseline: RubricScoreInput, candidate: RubricScoreInput,
) -> RubricScoreComparison:
    # Take inputs, not purported results: recompute findings and arithmetic.
    before = score_rubric(record, baseline)
    after = score_rubric(record, candidate)
    for field in ("organization_id", "workspace_id", "project_id", "task_ref"):
        if getattr(before.provenance, field) != getattr(after.provenance, field):
            raise ValueError("rubric comparison requires the same tenant/project/task scope")
    return RubricScoreComparison(
        baseline=before, candidate=after,
        score_delta=(after.score - before.score
                     if after.score is not None and before.score is not None else None),
        criterion_deltas={left.criterion_id: (
            right.normalized_score - left.normalized_score
            if left.normalized_score is not None and right.normalized_score is not None else None
        ) for left, right in zip(before.criteria, after.criteria, strict=True)},
    )
