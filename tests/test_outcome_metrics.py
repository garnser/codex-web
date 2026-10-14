from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from codex_web.agent_runtime_usage import AgentRuntimeUsage
from codex_web.artifact_evidence import EvidenceRequirement, EvidenceType, EvidenceResult
from codex_web.identity import AuthenticationActor
from codex_web.outcome_metrics import OutcomeFeedbackCreate
from codex_web.services.artifact_evidence import ArtifactEvidenceService, ArtifactEvidenceConflictError
from codex_web.services.metrics import MetricService
from codex_web.services.outcome_metrics import OutcomeMetricService
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.metrics import MetricStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class OutcomeMetricTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.sqlite = sqlite
        self.actor = AuthenticationActor(identity_id="reviewer", principal_kind="human",
            organization_id="org", workspace_id="ws", assurance="mfa", roles=("admin",))
        self.rows = []
        self.evidence = ArtifactEvidenceService(ArtifactEvidenceStore(sqlite))
        self.metrics = MetricService(MetricStore(sqlite))
        self.now = time.time() + 100
        def project(project_id, scope):
            if project_id != "p" or scope != self.actor.tenant:
                from codex_web.services.projects import ProjectNotFoundError
                raise ProjectNotFoundError("Project not found")
            return SimpleNamespace(id="p")
        self.service = OutcomeMetricService(metrics=self.metrics, evidence=self.evidence,
            telemetry=SimpleNamespace(list=lambda actor, project_id: [row for row in self.rows
                if row.project_id == project_id and row.organization_id == actor.organization_id
                and row.workspace_id == actor.workspace_id]),
            identity=SimpleNamespace(actor_for_identity=lambda identity_id, scope: self.actor),
            projects=SimpleNamespace(get=project), clock=lambda:self.now)

    def usage(self, key="one", **changes):
        data = dict(id=key, organization_id="org", workspace_id="ws", project_id="p",
            provider_id="fixture", runtime_id="fixture", runtime_type="fixture",
            execution_id="execution-" + key, work_item_ref="fixture#" + key,
            observed_model_ids=("model-a",), total_tokens=100, cost_usd=0.01,
            runtime_duration_seconds=5, telemetry_completeness="exact",
            terminal_outcome="succeeded", started_at=self.now - 200, completed_at=self.now - 150)
        self.rows.append(AgentRuntimeUsage(**(data | changes)))
        return self.rows[-1]

    def feedback(self, usage, outcome="accepted", **changes):
        data = dict(runtime_usage_id=usage.id, outcome=outcome, workload="documentation",
            correction_required=False, review_count=1, intervention_count=0,
            review_seconds=30, idempotency_key="feedback-" + usage.id)
        return self.service.feedback(OutcomeFeedbackCreate(**(data | changes)), project_id="p", actor=self.actor)

    def evaluate(self, **changes):
        args = dict(project_id="p", actor=self.actor, start_at=self.now - 300,
            end_at=self.now - 100, as_of=self.now)
        return self.service.evaluate(**(args | changes))

    def test_agent_success_and_human_acceptance_are_distinct(self):
        accepted = self.feedback(self.usage())
        rejected = self.feedback(self.usage("two"), "rejected", correction_required=True)
        report = self.evaluate()
        cohort = report["cohorts"][0]
        self.assertEqual(cohort["eligible_count"], 2)
        self.assertEqual(cohort["values"]["accepted_outcome_rate"], 0.5)
        self.assertEqual(cohort["values"]["human_correction_rate"], 0.5)
        self.assertEqual(cohort["values"]["tokens_per_accepted_outcome"], 200)
        self.assertEqual(cohort["values"]["cost_usd_per_accepted_outcome"], 0.02)
        self.assertEqual(cohort["values"]["review_seconds"], 60)
        self.assertEqual({row["feedback_evidence_id"] for row in cohort["tasks"]}, {accepted.id, rejected.id})

    def test_feedback_is_not_verification_even_with_permissive_review_gate(self):
        usage = self.usage()
        self.feedback(usage)
        gate = self.evidence.evaluate(usage.work_item_ref, (EvidenceRequirement(
            id="review", evidence_type=EvidenceType.REVIEW, accepted_results=(EvidenceResult.INFO,)),), actor=self.actor)
        self.assertFalse(gate.satisfied)

    def test_feedback_retry_returns_same_evidence_and_conflicting_key_fails(self):
        usage = self.usage()
        self.assertEqual(self.feedback(usage).id, self.feedback(usage).id)
        self.assertEqual(len(self.evidence.list_evidence(self.actor)), 1)
        with self.assertRaises(ArtifactEvidenceConflictError):
            self.feedback(usage, "rejected")

    def test_unknown_and_pending_are_not_accepted(self):
        pending = self.usage(completed_at=None, terminal_outcome="unknown")
        with self.assertRaises(ValueError):
            self.feedback(pending)
        self.feedback(pending, "pending")
        cohort = self.evaluate()["cohorts"][0]
        self.assertEqual(cohort["outcome_counts"]["pending"], 1)
        self.assertEqual(cohort["accepted_count"], 0)
        self.assertIsNone(cohort["values"]["tokens_per_accepted_outcome"])

    def test_reopened_task_keeps_cost_and_replaces_prior_acceptance(self):
        usage = self.usage()
        original = self.feedback(usage)
        self.feedback(usage, "reopened", supersedes_evidence_id=original.id, idempotency_key="reopen")
        cohort = self.evaluate()["cohorts"][0]
        self.assertEqual(cohort["accepted_count"], 0)
        self.assertEqual(cohort["outcome_counts"]["reopened"], 1)
        self.assertEqual(len(cohort["tasks"][0]["feedback_history_ids"]), 2)

    def test_retry_cannot_inherit_acceptance_of_previous_execution(self):
        original = self.usage()
        self.feedback(original)
        retry = self.usage("retry", work_item_ref=original.work_item_ref,
            started_at=self.now - 90, completed_at=self.now - 50)
        cohort = self.evaluate()["cohorts"][0]
        self.assertEqual(cohort["eligible_count"], 1)
        self.assertEqual(cohort["accepted_count"], 0)
        self.assertEqual(cohort["tasks"][0]["tokens"], 200)
        self.assertEqual(cohort["tasks"][0]["runtime_usage_ids"], [original.id, retry.id])

    def test_missing_measurements_remain_null_and_partial(self):
        usage = self.usage(cost_usd=None)
        self.feedback(usage, review_seconds=None)
        cohort = self.evaluate()["cohorts"][0]
        self.assertIsNone(cohort["values"]["cost_usd_per_accepted_outcome"])
        self.assertIsNone(cohort["values"]["review_seconds"])
        self.assertEqual(cohort["status"], "partial")

    def test_foreign_tenant_or_project_usage_is_not_available(self):
        for changes in ({"organization_id":"foreign"}, {"workspace_id":"foreign"}, {"project_id":"foreign"}):
            with self.subTest(changes=changes):
                usage = self.usage(str(changes), **changes)
                with self.assertRaises(ValueError):
                    self.feedback(usage)
        self.assertEqual(self.evaluate()["status"], "missing")

    def test_conflicting_feedback_is_unknown_until_explicitly_resolved(self):
        usage = self.usage()
        first = self.feedback(usage)
        second = self.feedback(usage, "rejected", idempotency_key="conflicting-review")
        cohort = self.evaluate()["cohorts"][0]
        self.assertEqual(cohort["accepted_count"], 0)
        self.assertEqual(cohort["outcome_counts"]["unknown"], 1)
        self.assertIn("conflicting", cohort["tasks"][0]["limitation"])
        self.feedback(usage, idempotency_key="resolve", supersedes_evidence_ids=(first.id, second.id))
        self.assertEqual(self.evaluate()["cohorts"][0]["accepted_count"], 1)

    def test_invalidated_feedback_does_not_restore_old_acceptance(self):
        usage = self.usage()
        first = self.feedback(usage)
        second = self.feedback(usage, "rejected", supersedes_evidence_id=first.id, idempotency_key="new")
        self.evidence.invalidate_evidence(second.id, "Test invalidation", actor=self.actor)
        self.assertEqual(self.evaluate()["cohorts"][0]["outcome_counts"]["unknown"], 1)

    def test_window_enrollment_and_old_windows_are_explicit(self):
        self.usage(started_at=self.now - 500)
        self.assertEqual(self.evaluate()["status"], "missing")
        self.rows.clear()
        self.usage(started_at=self.now - 90000)
        self.assertEqual(self.evaluate(start_at=self.now-91000, end_at=self.now-89000)["status"], "stale")
        with self.assertRaises(ValueError):
            self.evaluate(end_at=self.now + 1)

    def test_publish_reuses_canonical_metric_definitions_and_observations(self):
        self.feedback(self.usage())
        report = self.evaluate()
        published = self.service.publish(report, actor=self.actor)
        again = self.service.publish(report, actor=self.actor)
        self.assertEqual(published, again)
        self.assertEqual(len(published), 8)
        observation = self.metrics.history(published[0]["metric_id"], scope=self.actor.tenant)[0]
        self.assertTrue(observation.evidence_ids)
        self.assertEqual(observation.source, "accepted-outcome/1.0")

    def test_service_principal_cannot_self_accept(self):
        from codex_web.services.identity import AuthorizationError
        usage = self.usage()
        service_actor = self.actor.model_copy(update={"principal_kind":"service"})
        with self.assertRaises(AuthorizationError):
            self.service.feedback(OutcomeFeedbackCreate(runtime_usage_id=usage.id, outcome="accepted",
                workload="documentation", idempotency_key="service"), project_id="p", actor=service_actor)

    def test_free_form_content_and_unbounded_effort_are_refused(self):
        for changes in ({"comment":"private prompt"}, {"review_seconds":99999999}, {"review_count":-1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                OutcomeFeedbackCreate(runtime_usage_id="one", outcome="accepted", workload="documentation",
                    idempotency_key="key", **changes)

    def test_api_enforces_human_mfa_project_scope_and_server_calculation(self):
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient
        from codex_web.api.outcome_metrics import build_outcome_metrics_router
        app = FastAPI()
        actor = [self.actor]
        @app.middleware("http")
        async def authenticate(request: Request, call_next):
            request.state.identity_actor = actor[0]
            return await call_next(request)
        app.include_router(build_outcome_metrics_router(self.service))
        usage = self.usage()
        payload = dict(runtime_usage_id=usage.id, outcome="accepted", workload="documentation", idempotency_key="http")
        path = "/api/metrics/outcomes/projects/p"
        with TestClient(app) as client:
            actor[0] = self.actor.model_copy(update={"assurance":"primary"})
            self.assertEqual(client.post(path+"/feedback", json=payload).status_code, 403)
            actor[0] = self.actor
            response = client.post(path+"/feedback", json=payload)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(client.post(path+"/feedback", json={**payload,"comment":"prompt"}).status_code, 422)
            self.assertEqual(client.get(path, params={"start_at":self.now-300,"end_at":self.now-100}).status_code, 200)
            self.assertEqual(client.get(path.rsplit("/",1)[0]+"/other", params={"start_at":0,"end_at":1}).status_code, 404)
            self.assertEqual(client.post(path+"/publish", json={"start_at":0,"end_at":1,"accepted_count":999}).status_code, 422)
            actor[0] = self.actor.model_copy(update={"roles":()})
            self.assertEqual(client.get(path, params={"start_at":0,"end_at":1}).status_code, 403)

    def test_baseline_candidate_worked_comparison_reports_sample_and_missing_data(self):
        for model in ("baseline", "candidate"):
            for i in range(2):
                usage = self.usage(f"{model}-{i}", observed_model_ids=(model,),
                    total_tokens=100 if model == "baseline" else 150,
                    cost_usd=0.01 if model == "baseline" else 0.02)
                self.feedback(usage, "rejected" if model == "baseline" and i == 0 else "accepted",
                    correction_required=model == "baseline", review_seconds=60 if model == "baseline" else 30)
        baseline, candidate = self.evaluate()["cohorts"]
        self.assertEqual(baseline["model_ids"], ["baseline"])
        self.assertEqual(baseline["values"]["accepted_outcome_rate"], 0.5)
        self.assertEqual(candidate["values"]["accepted_outcome_rate"], 1)
        self.assertEqual(baseline["eligible_count"], candidate["eligible_count"])
        self.assertEqual(candidate["values"]["review_seconds"], 60)
        self.assertEqual(candidate["values"]["tokens_per_accepted_outcome"], 150)
        self.assertIsNone(candidate["template_revision"])

    def test_expired_feedback_remains_unknown_and_historical_projection_is_immutable(self):
        import json
        usage = self.usage()
        feedback = self.feedback(usage)
        published = self.service.publish(self.evaluate(), actor=self.actor)
        saved = self.evidence.get_evidence(published[0]["projection_evidence_id"], actor=self.actor)
        self.evidence.invalidate_evidence(feedback.id, "Review expired", actor=self.actor)
        self.assertEqual(self.evaluate()["accepted_count"], 0)
        self.assertEqual(json.loads(saved.metadata["projection_json"])["accepted_count"], 1)
        with self.assertRaises(ValueError):
            self.evaluate(as_of=self.now-10)

    def test_template_scope_exact_revision_and_invalidation_preserve_governance(self):
        from codex_web.artifact_evidence import ArtifactCreate
        from codex_web.services.data_governance import DataGovernanceService
        from codex_web.storage.data_governance import DataGovernanceStore
        governance = DataGovernanceService(DataGovernanceStore(self.sqlite))
        self.evidence.governance = governance
        artifact = self.evidence.create_artifact(ArtifactCreate(project_id="p", artifact_type="report",
            name="Synthetic template", revision="revision-one", classification="restricted"), actor=self.actor)
        usage = self.usage()
        feedback = self.feedback(usage, template_artifact_id=artifact.id)
        published = self.service.publish(self.evaluate(), actor=self.actor)
        snapshot = self.evidence.get_evidence(published[0]["projection_evidence_id"], actor=self.actor)
        protection = governance.get_record(snapshot.governance_record_id, actor=self.actor)
        self.assertEqual(protection.classification.value, "restricted")
        self.assertTrue(protection.deny_model_context)
        self.evidence.invalidate_artifact(artifact.id, "Template changed", actor=self.actor)
        self.assertEqual(self.feedback(usage, template_artifact_id=artifact.id).id, feedback.id)
        self.assertEqual(self.evaluate()["accepted_count"], 0)
        foreign = self.evidence.create_artifact(ArtifactCreate(project_id="foreign", artifact_type="report",
            name="Foreign template", revision="one"), actor=self.actor)
        with self.assertRaises(ValueError):
            self.feedback(usage, template_artifact_id=foreign.id, idempotency_key="foreign")

    def test_outcome_metrics_are_hidden_from_generic_metric_member_reads(self):
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient
        from codex_web.api.metrics import build_metrics_router
        self.feedback(self.usage())
        published = self.service.publish(self.evaluate(), actor=self.actor)
        app = FastAPI()
        @app.middleware("http")
        async def authenticate(request: Request, call_next):
            request.state.identity_actor = self.actor.model_copy(update={"roles":()})
            return await call_next(request)
        app.include_router(build_metrics_router(self.metrics))
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/metrics").json()["count"], 0)
            metric_id = published[0]["metric_id"]
            for suffix in ("", "/observations", "/snapshots"):
                self.assertEqual(client.get("/api/metrics/"+metric_id+suffix).status_code, 404)

    def test_concurrent_duplicate_feedback_produces_one_quality_observation(self):
        from concurrent.futures import ThreadPoolExecutor
        usage = self.usage()
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _:self.feedback(usage), range(4)))
        self.assertEqual(len({row.id for row in results}), 1)
        self.assertEqual(len(self.evidence.list_evidence(self.actor)), 1)
        self.assertEqual(self.evaluate()["eligible_count"], 1)

    def test_duplicate_usage_ids_and_late_outcomes_keep_original_enrollment_denominator(self):
        usage = self.usage()
        self.rows.append(usage)
        self.feedback(usage)
        report = self.evaluate()
        self.assertEqual(report["eligible_count"], 1)
        self.assertEqual(report["cohorts"][0]["values"]["tokens_per_accepted_outcome"], 100)
