from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.evaluation_quality import build_evaluation_quality_router
from codex_web.artifact_evidence import (
    ArtifactCreate, ArtifactType, EvidenceCreate, EvidenceRequirement, EvidenceResult,
    EvidenceType, sha256_digest,
)
from codex_web.definitions import DefinitionDraftCreate, DefinitionScope, reference_for
from codex_web.evaluation_quality import EvaluationQualityCreate
from codex_web.evaluation_rubrics import RUBRIC_KIND, RUBRIC_SCHEMA_VERSION
from codex_web.evaluations import (
    EvaluationExpectedInvariants, EvaluationReplayFixture, EvaluationRunMode, EvaluationRunRequest,
    EvaluationScenarioCreate, EvaluationStateSnapshot, EvaluationTerminalOutcome, EvaluationTrace,
)
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, PrincipalKind
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.evaluation_quality import EvaluationQualityService, SOURCE
from codex_web.services.evaluation_rubrics import install_evaluation_rubric_schema
from codex_web.services.evaluations import EvaluationService
from codex_web.services.identity import AuthorizationError
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.evaluations import EvaluationStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class EvaluationQualityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.actor = AuthenticationActor(identity_id="human-reviewer", principal_kind=PrincipalKind.HUMAN,
            organization_id="local", workspace_id="default", roles=("owner",), assurance=AuthenticationAssurance.MFA)
        self.definitions = DefinitionRegistryService(DefinitionRegistryStore(self.state))
        install_evaluation_rubric_schema(self.definitions)
        self.rubric = self.definitions.create_draft(DefinitionDraftCreate(
            definition_id="fixture.semantic", kind=RUBRIC_KIND, definition_schema_version=RUBRIC_SCHEMA_VERSION,
            scope_type=DefinitionScope.PROJECT, scope_id="project-a", actor=self.actor.identity_id,
            payload={"title": "Synthetic arithmetic fixture", "workload": "fixture-review", "minimum_score": .75,
                     "criteria": [{"id": "support", "description": "Fixture support", "minimum": 0, "maximum": 4}]}))
        self.evidence = ArtifactEvidenceService(ArtifactEvidenceStore(self.state))
        self.artifact = self.evidence.create_artifact(ArtifactCreate(project_id="project-a", work_item_ref="work:fixture",
            artifact_type=ArtifactType.REPORT, name="Synthetic output", revision="fixture-v1",
            digest=sha256_digest(b"Synthetic output"), retention_expires_at=time.time() + 86400), actor=self.actor)
        self.output = self.evidence.create_evidence(EvidenceCreate(project_id="project-a",
            artifact_ids=(self.artifact.id,), evidence_type=EvidenceType.RUNTIME_RESULT, result=EvidenceResult.INFO), actor=self.actor)
        self.evaluations = EvaluationService(EvaluationStore(self.state), self.definitions)
        self.scenario = self.evaluations.create_scenario(EvaluationScenarioCreate(
            scenario_id="semantic.fixture", version="1.0", title="Synthetic fixture", description="No human calibration claim",
            project_id="project-a", starting_state=EvaluationStateSnapshot(fingerprint_sha256="a" * 64), events=(),
            expected=EvaluationExpectedInvariants(require_no_llm=True), replay_fixtures=(EvaluationReplayFixture(
                id="recorded", mode=EvaluationRunMode.HISTORICAL, trace=EvaluationTrace(
                    terminal_outcome=EvaluationTerminalOutcome.SUCCEEDED, evidence_ids=(self.output.id,))),)), actor=self.actor)
        self.run, _ = await self.evaluations.run(EvaluationRunRequest(scenario_id=self.scenario.scenario_id,
            scenario_version="1.0", replay_fixture_id="recorded"), actor=self.actor)
        self.identity = SimpleNamespace(actor_for_identity=lambda identity_id, scope: self.actor)
        self.projects = SimpleNamespace(get=lambda project_id, scope: SimpleNamespace(id=project_id))
        self.service = EvaluationQualityService(evaluations=self.evaluations, definitions=self.definitions,
            evidence=self.evidence, identity=self.identity, projects=self.projects)

    def payload(self, **changes):
        values = dict(rubric=reference_for(self.rubric), artifact_id=self.artifact.id,
            artifact_revision=self.artifact.revision, idempotency_key="review-1",
            ratings=[{"criterion_id": "support", "value": 0, "finding": "Synthetic unsupported output"}])
        values.update(changes)
        return EvaluationQualityCreate(**values)

    def test_semantic_failure_preserves_deterministic_safety_and_exact_pins(self):
        original = self.run.model_dump(mode="json")
        item = self.service.annotate(self.run.id, self.payload(), actor=self.actor)
        view = self.service.inspect(self.run.id, actor=self.actor)
        self.assertTrue(view["deterministic_passed"])
        self.assertEqual(view["reports"][0]["status"], "semantic_fail")
        self.assertEqual(view["reports"][0]["reviewed_by"], self.actor.identity_id)
        self.assertEqual(view["reports"][0]["pin"]["artifact_sha256"], self.artifact.digest.value)
        self.assertEqual(view["reports"][0]["result"]["provenance"]["rubric"], reference_for(self.rubric).model_dump(mode="json"))
        self.assertEqual(self.evaluations.get_run(self.run.id, actor=self.actor).model_dump(mode="json"), original)
        self.assertEqual(item.evidence_type, EvidenceType.USER_OUTCOME)
        self.assertEqual(item.result, EvidenceResult.INFO)

    def test_missing_ratings_inconclusive_and_empty_unassessed(self):
        self.assertEqual(self.service.inspect(self.run.id, actor=self.actor)["status"], "unassessed")
        self.service.annotate(self.run.id, self.payload(ratings=[]), actor=self.actor)
        report = self.service.inspect(self.run.id, actor=self.actor)["reports"][0]
        self.assertEqual(report["status"], "inconclusive")
        self.assertIsNone(report["result"]["passed"])

    def test_idempotency_and_changed_content_conflict(self):
        first = self.service.annotate(self.run.id, self.payload(), actor=self.actor)
        self.assertEqual(first.id, self.service.annotate(self.run.id, self.payload(), actor=self.actor).id)
        with self.assertRaisesRegex(RuntimeError, "different content"):
            self.service.annotate(self.run.id, self.payload(ratings=[]), actor=self.actor)
        self.assertEqual(len([item for item in self.evidence.list_evidence(self.actor) if item.source == SOURCE]), 1)

    def test_services_members_and_missing_mfa_cannot_annotate(self):
        for actor in (self.actor.model_copy(update={"principal_kind": PrincipalKind.SERVICE,
                        "service_scopes": ("identity:admin", "evaluation:admin", "artifact-evidence:admin")}),
                      self.actor.model_copy(update={"roles": ("member",)}),
                      self.actor.model_copy(update={"assurance": AuthenticationAssurance.PRIMARY})):
            with self.subTest(actor=actor.principal_kind), self.assertRaises(AuthorizationError):
                self.service.annotate(self.run.id, self.payload(), actor=actor)

    def test_cross_tenant_and_foreign_rubric_or_artifact_refused(self):
        with self.assertRaises(KeyError):
            self.service.inspect(self.run.id, actor=self.actor.model_copy(update={"organization_id": "foreign"}))
        foreign = self.evidence.create_artifact(ArtifactCreate(project_id="foreign-project",
            artifact_type=ArtifactType.REPORT, name="Foreign", revision="v1", digest=sha256_digest(b"Foreign")), actor=self.actor)
        with self.assertRaises(ValueError):
            self.service.annotate(self.run.id, self.payload(artifact_id=foreign.id), actor=self.actor)
        changed = self.rubric.model_copy(update={"scope_id": "foreign-project"})
        # Scope is not part of the payload checksum; engine checks it separately.
        self.definitions.get_record = lambda record_id: changed
        with self.assertRaisesRegex(ValueError, "outside observation scope"):
            self.service.annotate(self.run.id, self.payload(), actor=self.actor)

    def test_revision_and_unlinked_output_refused(self):
        with self.assertRaisesRegex(RuntimeError, "revision changed"):
            self.service.annotate(self.run.id, self.payload(artifact_revision="stale"), actor=self.actor)
        unlinked = self.evidence.create_artifact(ArtifactCreate(project_id="project-a", artifact_type=ArtifactType.REPORT,
            name="Unlinked", revision="v1", digest=sha256_digest(b"Unlinked")), actor=self.actor)
        with self.assertRaisesRegex(ValueError, "linked by valid Evidence"):
            self.service.annotate(self.run.id, self.payload(artifact_id=unlinked.id), actor=self.actor)

    def test_supersession_invalidates_annotation_without_reclassifying_run(self):
        original = self.service.annotate(self.run.id, self.payload(), actor=self.actor)
        self.evidence.create_artifact(ArtifactCreate(project_id="project-a", artifact_type=ArtifactType.REPORT,
            name="Replacement", revision="fixture-v2", digest=sha256_digest(b"Replacement"),
            supersedes_artifact_id=self.artifact.id), actor=self.actor)
        view = self.service.inspect(self.run.id, actor=self.actor)
        self.assertEqual(view["status"], "inconclusive")
        self.assertEqual(len(view["invalidated"]), 1)
        self.assertEqual(view["reports"], [])
        self.assertTrue(view["deterministic_passed"])
        self.assertEqual(self.service.annotate(self.run.id, self.payload(), actor=self.actor).id, original.id)

    def test_expired_output_is_inconclusive_and_cannot_receive_new_annotation(self):
        self.service.annotate(self.run.id, self.payload(), actor=self.actor)
        self.service.clock = lambda: self.artifact.retention_expires_at + 1
        self.assertEqual(self.service.inspect(self.run.id, actor=self.actor)["status"], "inconclusive")
        self.assertEqual(self.service.options(self.run.id, actor=self.actor)["artifacts"], [])
        with self.assertRaises(ValueError):
            self.service.annotate(self.run.id, self.payload(idempotency_key="new"), actor=self.actor)

    def test_source_scan_budget_refuses_without_replay_or_annotation(self):
        expanded = self.run.model_copy(update={"trace": self.run.trace.model_copy(
            update={"evidence_ids": (self.output.id,) * 101})})
        self.evaluations.get_run = lambda run_id, actor: expanded
        with self.assertRaisesRegex(ValueError, "100-record bound"):
            self.service.annotate(self.run.id, self.payload(), actor=self.actor)
        with self.assertRaisesRegex(ValueError, "100-record bound"):
            self.service.options(self.run.id, actor=self.actor)
        self.assertEqual(len([item for item in self.evidence.list_evidence(self.actor) if item.source == SOURCE]), 0)

    def test_canonical_reopen_keeps_human_annotation_and_history(self):
        item = self.service.annotate(self.run.id, self.payload(), actor=self.actor)
        self.service.evidence = ArtifactEvidenceService(ArtifactEvidenceStore(self.state))
        self.service.evaluations = EvaluationService(EvaluationStore(self.state), self.definitions)
        report = self.service.inspect(self.run.id, actor=self.actor)["reports"][0]
        self.assertEqual(report["evidence_id"], item.id)
        self.assertEqual(report["status"], "semantic_fail")

    def test_inspection_reuses_one_canonical_evidence_snapshot(self):
        self.service.annotate(self.run.id, self.payload(), actor=self.actor)
        self.service.annotate(self.run.id, self.payload(idempotency_key="second-review"), actor=self.actor)
        with patch.object(self.evidence.store, "load", wraps=self.evidence.store.load) as load:
            self.assertEqual(len(self.service.inspect(self.run.id, actor=self.actor)["reports"]), 2)
            self.assertEqual(load.call_count, 1)

    async def test_baseline_comparison_is_named_and_rubric_revision_bound(self):
        baseline = self.service.annotate(self.run.id, self.payload(), actor=self.actor)
        candidate, _ = await self.evaluations.run(EvaluationRunRequest(scenario_id=self.scenario.scenario_id,
            scenario_version="1.0", replay_fixture_id="recorded"), actor=self.actor)
        self.service.annotate(candidate.id, self.payload(idempotency_key="candidate",
            ratings=[{"criterion_id": "support", "value": 4, "finding": "Synthetic better rating"}]), actor=self.actor)
        view = self.service.inspect(candidate.id, actor=self.actor, baseline_evidence_id=baseline.id)
        self.assertEqual(view["comparison"]["score_delta"], 1)
        self.assertEqual(view["comparison"]["baseline"]["provenance"]["rater_ref"], self.actor.identity_id)

    def test_quality_evidence_cannot_satisfy_permissive_technical_gate(self):
        item = self.service.annotate(self.run.id, self.payload(), actor=self.actor)
        self.assertEqual(item.work_item_ref, "work:fixture")
        gate = self.evidence.evaluate("work:fixture", requirements=(
            EvidenceRequirement(id="quality", evidence_type=EvidenceType.USER_OUTCOME,
                                accepted_results=(EvidenceResult.INFO,)),), actor=self.actor)
        self.assertFalse(gate.satisfied)

    def test_invalidated_source_and_nonhuman_producer_are_inconclusive(self):
        self.service.annotate(self.run.id, self.payload(), actor=self.actor)
        self.identity.actor_for_identity = lambda identity_id, scope: self.actor.model_copy(update={"principal_kind": PrincipalKind.SERVICE})
        self.assertEqual(self.service.inspect(self.run.id, actor=self.actor)["status"], "inconclusive")
        self.identity.actor_for_identity = lambda identity_id, scope: self.actor
        self.evidence.invalidate_evidence(self.output.id, "Fixture source invalidated", actor=self.actor)
        self.assertEqual(self.service.inspect(self.run.id, actor=self.actor)["status"], "inconclusive")

    def test_review_never_invokes_replay_and_preserves_no_llm_budget(self):
        self.evaluations.run = AsyncMock(side_effect=AssertionError("review cannot invoke replay"))
        self.service.annotate(self.run.id, self.payload(), actor=self.actor)
        self.service.inspect(self.run.id, actor=self.actor)
        self.service.options(self.run.id, actor=self.actor)
        self.evaluations.run.assert_not_called()
        self.assertEqual(self.run.trace.reasoning_calls, 0)

    async def test_multiple_annotations_require_explicit_candidate_for_baseline(self):
        baseline = self.service.annotate(self.run.id, self.payload(), actor=self.actor)
        candidate, _ = await self.evaluations.run(EvaluationRunRequest(scenario_id=self.scenario.scenario_id,
            scenario_version="1.0", replay_fixture_id="recorded"), actor=self.actor)
        first = self.service.annotate(candidate.id, self.payload(idempotency_key="candidate-1"), actor=self.actor)
        self.service.annotate(candidate.id, self.payload(idempotency_key="candidate-2"), actor=self.actor)
        with self.assertRaisesRegex(RuntimeError, "select an exact candidate"):
            self.service.inspect(candidate.id, actor=self.actor, baseline_evidence_id=baseline.id)
        report = self.service.inspect(candidate.id, actor=self.actor, baseline_evidence_id=baseline.id,
                                      candidate_evidence_id=first.id)
        self.assertEqual(report["comparison"]["candidate_evidence_id"], first.id)
        self.assertEqual(report["comparison"]["baseline_pin"]["run_id"], self.run.id)
        with self.assertRaisesRegex(RuntimeError, "another replay run"):
            self.service.inspect(candidate.id, actor=self.actor, baseline_evidence_id=baseline.id,
                                 candidate_evidence_id=baseline.id)

    def test_options_are_scoped_and_reads_require_joined_authority(self):
        options = self.service.options(self.run.id, actor=self.actor)
        self.assertEqual(options["artifacts"][0]["id"], self.artifact.id)
        self.assertEqual(options["rubrics"][0]["reference"]["record_id"], self.rubric.record_id)
        actor = self.actor.model_copy(update={"principal_kind": PrincipalKind.SERVICE,
            "service_scopes": ("evaluation:admin",)})
        with self.assertRaises(AuthorizationError):
            self.service.inspect(self.run.id, actor=actor)

    def test_routed_http_uses_authenticated_actor_and_rejects_spoofed_rater(self):
        app = FastAPI()
        @app.middleware("http")
        async def actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)
        app.include_router(build_evaluation_quality_router(self.service))
        with TestClient(app) as client:
            endpoint = f"/api/evaluations/runs/{self.run.id}/quality"
            self.assertEqual(client.get(endpoint + "/options").status_code, 200)
            self.assertEqual(client.post(endpoint, json=self.payload().model_dump(mode="json")).status_code, 200)
            result = client.get(endpoint)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["reports"][0]["status"], "semantic_fail")
            self.assertEqual(client.post(endpoint, json={**self.payload().model_dump(mode="json"),
                "rater_ref": "someone-else"}).status_code, 422)
