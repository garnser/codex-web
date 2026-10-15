"""Human semantic observations; deterministic replay/technical gates stay separate."""
import hashlib
import json
import time

from codex_web.artifact_evidence import (
    EvidenceCreate, EvidenceResult, EvidenceType,
)
from codex_web.evaluation_quality import EvaluationQualityCreate, EvaluationQualityPin
from codex_web.definitions import DefinitionScope, reference_for
from codex_web.evaluation_rubrics import RUBRIC_KIND, EvaluationRubric, RubricScoreInput
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.artifact_evidence import ArtifactEvidenceConflictError
from codex_web.services.evaluation_rubrics import compare_rubric_scores, score_rubric
from codex_web.services.identity import AuthorizationError, IdentityService


SOURCE = "human-evaluation-quality/1.0"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class EvaluationQualityService:
    def __init__(self, *, evaluations, definitions, evidence, identity, projects, clock=time.time):
        self.evaluations, self.definitions = evaluations, definitions
        self.evidence, self.identity, self.projects = evidence, identity, projects
        self.clock = clock

    def _valid(self, item):
        return (item.lifecycle.value in {"active", "valid"}
                and (item.retention_expires_at is None or self.clock() < item.retention_expires_at))

    @staticmethod
    def access(actor, *, mutation=False):
        if actor.principal_kind == PrincipalKind.SERVICE:
            if mutation:
                raise AuthorizationError("semantic annotations require an authenticated human")
            if not {"evaluation:admin", "artifact-evidence:admin"}.issubset(actor.service_scopes):
                raise AuthorizationError("quality inspection requires Evaluation and Evidence authority")
        else:
            IdentityService.require_admin(actor)
            if mutation:
                IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)

    def _run(self, run_id, actor):
        run = self.evaluations.get_run(run_id, actor=actor)
        scenario = self.evaluations.get_scenario(run.scenario_id, run.scenario_version, actor=actor)
        if not scenario.project_id or run.fixture_checksum_sha256 != scenario.fixture_checksum_sha256:
            raise ValueError("quality review requires an exact Project-scoped scenario fixture")
        self.projects.get(scenario.project_id, scope=actor.tenant)
        return run, scenario

    def _artifact(self, artifact_id, *, run, scenario, actor, state=None):
        state = state if state is not None else self.evidence.store.load()
        artifact = self.evidence.get_artifact(artifact_id, actor=actor, state=state)
        if (artifact.project_id != scenario.project_id or not self._valid(artifact)
                or not artifact.revision or artifact.digest is None):
            raise ValueError("review output requires an active, revision-pinned Project Artifact with SHA256")
        parents = []
        if len(run.trace.evidence_ids) > 100:
            raise ValueError("quality source Evidence scan exceeds the 100-record bound")
        for evidence_id in run.trace.evidence_ids:
            item = self.evidence.get_evidence(evidence_id, actor=actor, state=state)
            if (item.project_id == scenario.project_id and self._valid(item)
                    and artifact.id in item.artifact_ids):
                parents.append(item.id)
        if not parents:
            raise ValueError("output Artifact must be linked by valid Evidence in the exact replay trace")
        return artifact, tuple(parents)

    @staticmethod
    def _pin(run, artifact):
        return EvaluationQualityPin(
            run_id=run.id, run_sha256=hashlib.sha256(_json(run.model_dump(mode="json")).encode()).hexdigest(),
            artifact_id=artifact.id, artifact_revision=artifact.revision,
            artifact_sha256=artifact.digest.value,
        )

    def annotate(self, run_id, payload: EvaluationQualityCreate, *, actor):
        self.access(actor, mutation=True)
        run, scenario = self._run(run_id, actor)
        request_digest = hashlib.sha256(_json({"run_id": run_id, "payload": payload.model_dump(mode="json")}).encode()).hexdigest()
        previous = self.evidence.find_idempotent_evidence(actor=actor, source=SOURCE,
                                                        idempotency_key=payload.idempotency_key)
        if previous is not None:
            if previous.metadata.get("quality_request_sha256") != request_digest:
                raise ArtifactEvidenceConflictError("annotation retry key has different content")
            return previous
        artifact, parents = self._artifact(payload.artifact_id, run=run, scenario=scenario, actor=actor)
        if artifact.revision != payload.artifact_revision:
            raise ArtifactEvidenceConflictError("output Artifact revision changed")
        rubric = self.definitions.get_record(payload.rubric.record_id)
        observation = RubricScoreInput(
            rubric=payload.rubric, organization_id=actor.organization_id, workspace_id=actor.workspace_id,
            project_id=scenario.project_id, task_ref=f"scenario:{scenario.scenario_id}@{scenario.version}",
            artifact_ref=artifact.id, artifact_revision_sha256=artifact.digest.value,
            annotation_ref=payload.idempotency_key, rater_ref=actor.identity_id, ratings=payload.ratings,
        )
        score_rubric(rubric, observation)
        pin = self._pin(run, artifact)
        return self.evidence.create_evidence(EvidenceCreate(
            project_id=scenario.project_id, artifact_ids=(artifact.id,), execution_id=run.id,
            evidence_type=EvidenceType.USER_OUTCOME, result=EvidenceResult.INFO, source=SOURCE,
            external_id=payload.idempotency_key, deny_model_context=True,
            summary="Human semantic annotation; separate from deterministic safety and technical verification.",
            metadata={"quality_pin": pin.model_dump_json(), "quality_observation": observation.model_dump_json(),
                      "quality_request_sha256": request_digest, "reviewer_principal_kind": "human"},
        ), actor=actor, idempotency_key=payload.idempotency_key, source_evidence_ids=parents)

    def _report(self, item, *, actor, state=None, context=None):
        if item.source != SOURCE or item.evidence_type != EvidenceType.USER_OUTCOME or item.result != EvidenceResult.INFO:
            raise ValueError("Evidence is not a human semantic annotation")
        if not self._valid(item):
            raise ValueError("annotation Evidence is no longer valid")
        producer = self.identity.actor_for_identity(item.producer_identity_id, scope=actor.tenant)
        if producer.principal_kind != PrincipalKind.HUMAN:
            raise ValueError("annotation producer is not a canonical human identity")
        observation = RubricScoreInput.model_validate_json(item.metadata.get("quality_observation", ""))
        pin = EvaluationQualityPin.model_validate_json(item.metadata.get("quality_pin", ""))
        run, scenario = context if context and context[0].id == pin.run_id else self._run(pin.run_id, actor)
        artifact, _ = self._artifact(pin.artifact_id, run=run, scenario=scenario, actor=actor, state=state)
        if (pin != self._pin(run, artifact) or observation.rater_ref != item.producer_identity_id
                or observation.annotation_ref != item.external_id or item.execution_id != run.id
                or item.project_id != scenario.project_id or item.artifact_ids != (artifact.id,)
                or observation.organization_id != actor.organization_id or observation.workspace_id != actor.workspace_id
                or observation.project_id != scenario.project_id or observation.artifact_ref != artifact.id
                or observation.artifact_revision_sha256 != artifact.digest.value
                or observation.task_ref != f"scenario:{scenario.scenario_id}@{scenario.version}"):
            raise ValueError("annotation provenance no longer matches canonical records")
        result = score_rubric(self.definitions.get_record(observation.rubric.record_id), observation)
        return {"evidence_id": item.id, "reviewed_by": item.producer_identity_id,
                "observed_at": item.observed_at, "pin": pin.model_dump(mode="json"),
                "result": result.model_dump(mode="json"),
                "status": "inconclusive" if not result.complete else "semantic_pass" if result.passed else "semantic_fail"}

    def inspect(self, run_id, *, actor, baseline_evidence_id=None, candidate_evidence_id=None):
        self.access(actor)
        run, scenario = self._run(run_id, actor)
        state = self.evidence.store.load()
        reports, invalid = [], []
        annotations = [item for item in self.evidence.list_evidence(actor, state=state)
                       if item.source == SOURCE and item.execution_id == run.id]
        for item in annotations[:100]:
            try:
                reports.append(self._report(item, actor=actor, state=state, context=(run, scenario)))
            except (ValueError, RuntimeError, LookupError):
                invalid.append({"evidence_id": item.id, "status": "inconclusive", "reason": "Pinned annotation or source is unavailable, invalid or superseded."})
        comparison = None
        if baseline_evidence_id:
            if candidate_evidence_id:
                candidate = self._report(self.evidence.get_evidence(candidate_evidence_id, actor=actor, state=state), actor=actor,
                                         state=state, context=(run, scenario))
                if candidate["pin"]["run_id"] != run.id:
                    raise ArtifactEvidenceConflictError("candidate annotation belongs to another replay run")
            else:
                if len(reports) != 1 or len(annotations) > 100:
                    raise ArtifactEvidenceConflictError("select an exact candidate annotation for baseline comparison")
                candidate = reports[0]
            baseline = self._report(self.evidence.get_evidence(baseline_evidence_id, actor=actor, state=state), actor=actor,
                                    state=state, context=(run, scenario))
            before = RubricScoreInput.model_validate(baseline["result"]["provenance"])
            after = RubricScoreInput.model_validate(candidate["result"]["provenance"])
            comparison = {**compare_rubric_scores(self.definitions.get_record(after.rubric.record_id), before, after).model_dump(mode="json"),
                          "baseline_evidence_id": baseline_evidence_id, "candidate_evidence_id": candidate["evidence_id"],
                          "baseline_pin": baseline["pin"], "candidate_pin": candidate["pin"]}
        return {"run_id": run.id, "project_id": scenario.project_id, "deterministic_passed": run.passed,
                "reports": reports, "invalidated": invalid, "comparison": comparison,
                "truncated": len(annotations) > 100,
                "status": "unassessed" if not reports and not invalid else "reviewed" if reports else "inconclusive",
                "limitations": "Manual human annotations; no model judge, calibration claim, automatic routing or release policy change."}

    def options(self, run_id, *, actor):
        self.access(actor)
        run, scenario = self._run(run_id, actor)
        state = self.evidence.store.load()
        if len(run.trace.evidence_ids) > 100:
            raise ValueError("quality source Evidence scan exceeds the 100-record bound")
        artifacts = {}
        artifact_attempts = 0
        for evidence_id in run.trace.evidence_ids:
            try:
                item = self.evidence.get_evidence(evidence_id, actor=actor, state=state)
                if item.project_id != scenario.project_id or not self._valid(item):
                    continue
                for artifact_id in item.artifact_ids:
                    if artifact_attempts >= 100:
                        raise ValueError("quality output Artifact scan exceeds the 100-record bound")
                    artifact_attempts += 1
                    artifact = self.evidence.get_artifact(artifact_id, actor=actor, state=state)
                    if artifact.project_id == scenario.project_id and self._valid(artifact) and artifact.revision and artifact.digest:
                        artifacts[artifact.id] = {"id": artifact.id, "name": artifact.name, "revision": artifact.revision, "sha256": artifact.digest.value}
            except RuntimeError:
                continue
        scopes = {DefinitionScope.GLOBAL: None, DefinitionScope.ORGANIZATION: actor.organization_id,
                  DefinitionScope.WORKSPACE: actor.workspace_id, DefinitionScope.PROJECT: scenario.project_id}
        rubrics = []
        for record in self.definitions.list_records(kind=RUBRIC_KIND):
            if scopes.get(record.scope_type) != record.scope_id:
                continue
            payload = EvaluationRubric.model_validate(record.payload)
            rubrics.append({"reference": reference_for(record).model_dump(mode="json"),
                            "lifecycle": record.lifecycle.value, "payload": payload.model_dump(mode="json")})
            if len(rubrics) >= 100:
                break
        return {"run_id": run.id, "project_id": scenario.project_id,
                "artifacts": list(artifacts.values()), "rubrics": rubrics}
