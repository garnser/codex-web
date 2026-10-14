from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from pydantic import ValidationError

from codex_web.definitions import (
    DefinitionDraftCreate, DefinitionRecord, DefinitionScope, definition_checksum, reference_for,
)
from codex_web.evaluation_rubrics import (
    RUBRIC_KIND, RUBRIC_SCHEMA_VERSION, EvaluationRubric, RubricScoreInput,
)
from codex_web.services.definitions import DefinitionRegistryService, DefinitionSchemaRegistry
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.services.evaluation_rubrics import (
    compare_rubric_scores, install_evaluation_rubric_schema, score_rubric,
)


def record(**changes):
    # Synthetic arithmetic fixture; no claim of human-labelled calibration.
    payload = dict(title="Fixture rubric", workload="fixture-review", minimum_score=.6,
                   criteria=[dict(id="correctness", description="Fixture correctness",
                                  minimum=-2, maximum=2, weight=3,
                                  minimum_normalized_score=.5),
                             dict(id="clarity", description="Fixture clarity",
                                  minimum=0, maximum=10, weight=1)])
    values = dict(record_id="rubric-record-1", definition_id="fixture.rubric",
                  kind=RUBRIC_KIND, definition_schema_version=RUBRIC_SCHEMA_VERSION,
                  revision=1, scope_type=DefinitionScope.PROJECT, scope_id="project-a",
                  payload=payload, created_by="fixture-author")
    values.update(changes)
    values["checksum"] = definition_checksum(**{
        key: values[key] for key in ("definition_id", "kind", "definition_schema_version", "payload")
    })
    return DefinitionRecord(**values)


def observation(rubric, **changes):
    values = dict(rubric=reference_for(rubric), organization_id="org-a",
                  workspace_id="workspace-a", project_id="project-a", task_ref="task-a",
                  artifact_ref="artifact-a", artifact_revision_sha256="a" * 64,
                  annotation_ref="fixture-annotation", rater_ref="unverified-fixture-rater",
                  ratings=[dict(criterion_id="correctness", value=1, finding="Fixture finding"),
                           dict(criterion_id="clarity", value=10, finding="Fixture finding")])
    values.update(changes)
    return RubricScoreInput(**values)


class RubricScoringTests(unittest.TestCase):
    def test_weighted_scales_threshold_and_provenance(self):
        rubric = record()
        source = observation(rubric)
        result = score_rubric(rubric, source)
        self.assertEqual(result.score, .8125)
        self.assertTrue(result.complete)
        self.assertTrue(result.passed)
        self.assertEqual(result.criteria[0].normalized_score, .75)
        self.assertEqual(result.criteria[0].finding, "Fixture finding")
        self.assertEqual(result.provenance, source)
        self.assertEqual(result.provenance.rubric, reference_for(rubric))

    def test_criterion_floor_cannot_be_hidden_by_weighted_average(self):
        payload = record().payload
        payload["criteria"][0]["weight"] = 1
        payload["criteria"][1]["weight"] = 100
        rubric = record(payload=payload)
        source = observation(rubric)
        source = observation(rubric, ratings=[dict(criterion_id="correctness", value=-1,
                                                 finding="Below criterion floor"),
                                             source.ratings[1]])
        result = score_rubric(rubric, source)
        self.assertGreater(result.score, .9)
        self.assertFalse(result.passed)
        self.assertFalse(result.criteria[0].passed)

    def test_missing_all_or_some_ratings_never_passes(self):
        rubric = record()
        for ratings in ([], [observation(rubric).ratings[0]]):
            with self.subTest(ratings=ratings):
                result = score_rubric(rubric, observation(rubric, ratings=ratings))
                self.assertFalse(result.complete)
                self.assertIsNone(result.score)
                self.assertIsNone(result.passed)
                self.assertIsNone(result.criteria[1].normalized_score)

    def test_unknown_duplicate_and_out_of_range_ratings_rejected(self):
        rubric = record()
        for ratings in ([dict(criterion_id="foreign", value=0, finding="Fixture")],
                        [dict(criterion_id="correctness", value=3, finding="Fixture")],
                        [dict(criterion_id="correctness", value=-3, finding="Fixture")],
                        [observation(rubric).ratings[0]] * 2):
            with self.subTest(ratings=ratings), self.assertRaises(ValueError):
                score_rubric(rubric, observation(rubric, ratings=ratings))

    def test_nonfinite_values_and_invalid_scales_rejected(self):
        rubric = record()
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                observation(rubric, ratings=[dict(criterion_id="clarity", value=value, finding="Fixture")])
            for field in ("minimum", "maximum", "weight", "minimum_normalized_score"):
                payload = record().payload
                payload["criteria"][0][field] = value
                with self.subTest(value=value, field=field), self.assertRaises(ValidationError):
                    EvaluationRubric.model_validate(payload)
        for field, value in (("weight", 0), ("maximum", -2), ("minimum_normalized_score", 2)):
            payload = record().payload
            payload["criteria"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                EvaluationRubric.model_validate(payload)

    def test_schema_bounds_duplicates_and_extra_fields(self):
        for criteria in ([], record().payload["criteria"] * 17,
                         [record().payload["criteria"][0]] * 2):
            payload = record().payload
            payload["criteria"] = criteria
            with self.subTest(criteria=len(criteria)), self.assertRaises(ValidationError):
                EvaluationRubric.model_validate(payload)
        with self.assertRaises(ValidationError):
            observation(record(), human_verified=True)
        with self.assertRaises(ValidationError):
            observation(record(), artifact_revision_sha256="latest")
        with self.assertRaises(ValidationError):
            observation(record(), ratings=[dict(criterion_id="clarity", value=1, finding=" ")])

    def test_pin_kind_schema_and_mutated_payload_rejected(self):
        rubric = record()
        source = observation(rubric)
        for modified in (record(revision=2), record(kind="foreign-kind"),
                         record(definition_schema_version="2.0")):
            with self.subTest(modified=modified.kind), self.assertRaises(ValueError):
                score_rubric(modified, source)
        rubric.payload["minimum_score"] = 0
        with self.assertRaises(ValidationError):
            score_rubric(rubric, source)

    def test_bypassed_input_validation_rejected(self):
        rubric = record()
        invalid = observation(rubric).model_copy(update={"ratings": (
            observation(rubric).ratings[0].model_copy(update={"value": float("nan")}),
        )})
        with self.assertRaises(ValidationError):
            score_rubric(rubric, invalid)

    def test_definition_scopes(self):
        for scope, scope_id in ((DefinitionScope.ORGANIZATION, "org-a"),
                                (DefinitionScope.WORKSPACE, "workspace-a"),
                                (DefinitionScope.PROJECT, "project-a"),
                                (DefinitionScope.GLOBAL, None)):
            rubric = record(scope_type=scope, scope_id=scope_id)
            self.assertTrue(score_rubric(rubric, observation(rubric)).complete)
            if scope != DefinitionScope.GLOBAL:
                foreign = record(scope_type=scope, scope_id="foreign")
                with self.assertRaisesRegex(ValueError, "outside observation scope"):
                    score_rubric(foreign, observation(foreign))

    def test_comparison_recomputes_deltas_and_preserves_artifact_revisions(self):
        rubric = record()
        before = observation(rubric)
        after = observation(rubric, artifact_ref="artifact-b", artifact_revision_sha256="b" * 64,
                            ratings=[dict(criterion_id="correctness", value=2, finding="Fixture"),
                                     before.ratings[1]])
        result = compare_rubric_scores(rubric, before, after)
        self.assertEqual(result.score_delta, .1875)
        self.assertEqual(result.criterion_deltas, dict(correctness=.25, clarity=0))
        self.assertEqual(result.candidate.provenance.artifact_revision_sha256, "b" * 64)
        incomplete = compare_rubric_scores(rubric, before, observation(rubric, ratings=[]))
        self.assertIsNone(incomplete.score_delta)
        self.assertEqual(incomplete.criterion_deltas, dict(correctness=None, clarity=None))

    def test_comparison_refuses_changed_scope_or_rubric(self):
        rubric = record()
        before = observation(rubric)
        for field in ("task_ref", "organization_id", "workspace_id", "project_id"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                compare_rubric_scores(rubric, before, observation(rubric, **{field: "foreign"}))
        with self.assertRaisesRegex(ValueError, "pin does not match"):
            compare_rubric_scores(rubric, before, observation(record(revision=2)))

    def test_registry_schema_is_idempotent_and_validates_data(self):
        class Registry:
            schemas = DefinitionSchemaRegistry()

            def register_schema(self, schema):
                self.schemas.register(schema)

        registry = Registry()
        install_evaluation_rubric_schema(registry)
        install_evaluation_rubric_schema(registry)
        self.assertEqual(len(registry.schemas.metadata()), 1)
        schema = registry.schemas.get(RUBRIC_KIND, RUBRIC_SCHEMA_VERSION)
        normalized = schema.validate(record().payload)
        self.assertEqual(normalized["criteria"][0]["minimum_normalized_score"], .5)
        with self.assertRaises(ValidationError):
            schema.validate(dict(title="Invalid", workload="fixture", criteria=[], minimum_score=.5))

    def test_canonical_registry_reopen_preserves_exact_historical_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SQLiteStateStore(Path(directory) / "state.sqlite3")
            registry = DefinitionRegistryService(DefinitionRegistryStore(state))
            install_evaluation_rubric_schema(registry)
            draft = DefinitionDraftCreate(
                definition_id="fixture.rubric", kind=RUBRIC_KIND,
                definition_schema_version=RUBRIC_SCHEMA_VERSION,
                scope_type=DefinitionScope.PROJECT, scope_id="project-a",
                payload=record().payload, actor="fixture-author",
            )
            original = registry.create_draft(draft)
            revised_payload = {**draft.payload, "minimum_score": 1}
            revised = registry.create_draft(draft.model_copy(update={"payload": revised_payload}))
            self.assertNotEqual(reference_for(original), reference_for(revised))
            reopened = DefinitionRegistryService(DefinitionRegistryStore(state))
            install_evaluation_rubric_schema(reopened)
            old = reopened.get_record(original.record_id)
            new = reopened.get_record(revised.record_id)
            self.assertTrue(score_rubric(old, observation(old)).passed)
            self.assertFalse(score_rubric(new, observation(new)).passed)
            with self.assertRaisesRegex(ValueError, "pin does not match"):
                compare_rubric_scores(old, observation(old), observation(new))
