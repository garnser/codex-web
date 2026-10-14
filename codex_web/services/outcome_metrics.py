"""Deterministic quality projections over canonical runtime usage and Evidence.

Human observations are review Evidence, never policy approval or completion proof.
No prompt, transcript or free-form feedback is copied into this projection.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import time

from codex_web.agent_runtime_usage import RuntimeTerminalOutcome
from codex_web.artifact_evidence import (ArtifactLifecycle, EvidenceCreate,
    EvidenceLifecycle, EvidenceResult, EvidenceType)
from codex_web.identity import PrincipalKind
from codex_web.metrics import MetricAggregation, MetricDefinitionCreate, MetricObservationCreate, MetricValueType
from codex_web.outcome_metrics import OutcomeFeedbackCreate, UserOutcome
from codex_web.services.identity import AuthorizationError


SOURCE = "human-outcome/1.0"
FORMULA_VERSION = "accepted-outcome/1.0"


class OutcomeMetricService:
    def __init__(self, *, metrics, evidence, telemetry, identity, projects, clock=time.time):
        self.metrics, self.evidence, self.telemetry = metrics, evidence, telemetry
        self.identity, self.projects, self.clock = identity, projects, clock

    def _project(self, project_id, actor):
        self.projects.get(project_id, scope=actor.tenant)

    @staticmethod
    def _task(record):
        return ("work:" + record.work_item_ref if record.work_item_ref
                else "execution:" + (record.execution_id or record.id))

    def feedback(self, payload: OutcomeFeedbackCreate, *, project_id, actor):
        self._project(project_id, actor)
        if actor.principal_kind != PrincipalKind.HUMAN:
            raise AuthorizationError("outcome feedback requires a human reviewer")
        request_digest = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
        previous_request = next((row for row in self.evidence.list_evidence(actor) if
            row.project_id == project_id and row.source == SOURCE
            and row.external_id == payload.idempotency_key and row.producer_identity_id == actor.identity_id), None)
        if previous_request is not None:
            if previous_request.metadata.get("outcome_request_sha256") != request_digest:
                from codex_web.services.artifact_evidence import ArtifactEvidenceConflictError
                raise ArtifactEvidenceConflictError("outcome idempotency key has different content")
            return previous_request
        usage = next((row for row in self.telemetry.list(actor, project_id=project_id)
                      if row.id == payload.runtime_usage_id), None)
        if usage is None:
            raise ValueError("runtime usage not found in this Project")
        if payload.outcome in {UserOutcome.ACCEPTED, UserOutcome.REJECTED} and (
            usage.completed_at is None or usage.terminal_outcome == RuntimeTerminalOutcome.UNKNOWN
        ):
            raise ValueError("unresolved native execution cannot receive a settled outcome")
        artifacts = ()
        template_revision = None
        if payload.template_artifact_id:
            artifact = self.evidence.get_artifact(payload.template_artifact_id, actor=actor)
            if artifact.project_id != project_id or artifact.lifecycle != ArtifactLifecycle.ACTIVE or not artifact.revision:
                raise ValueError("template requires an active exact-revision Project Artifact")
            artifacts, template_revision = (artifact.id,), artifact.revision
        parents = set(payload.supersedes_evidence_ids)
        if payload.supersedes_evidence_id:
            parents.add(payload.supersedes_evidence_id)
        for parent_id in parents:
            previous = self.evidence.get_evidence(parent_id, actor=actor)
            if (previous.project_id != project_id or previous.source != SOURCE
                or previous.metadata.get("task_key") != self._task(usage)):
                raise ValueError("superseded feedback belongs to another task")
        metadata = payload.model_dump(mode="json", exclude={"idempotency_key"})
        metadata["supersedes_evidence_ids"] = json.dumps(sorted(parents))
        metadata.update(task_key=self._task(usage), template_revision=template_revision,
                        reviewer_principal_kind="human", formula_version=FORMULA_VERSION,
                        outcome_request_sha256=request_digest)
        return self.evidence.create_evidence(EvidenceCreate(
            project_id=project_id, execution_id=usage.execution_id, work_item_ref=usage.work_item_ref,
            artifact_ids=artifacts, evidence_type=EvidenceType.REVIEW, source=SOURCE,
            external_id=payload.idempotency_key,
            result=EvidenceResult.INFO,
            summary="Optional human outcome observation; not a completion or approval gate.",
            deny_model_context=True, metadata=metadata), actor=actor,
            idempotency_key=payload.idempotency_key)

    def _human(self, row, actor):
        try:
            producer = self.identity.actor_for_identity(row.producer_identity_id, scope=actor.tenant)
            return producer.principal_kind == PrincipalKind.HUMAN
        except (RuntimeError, ValueError, KeyError):
            return False

    @staticmethod
    def _parents(row):
        try:
            parents = json.loads(row.metadata.get("supersedes_evidence_ids", "[]"))
            if not isinstance(parents, list) or len(parents) > 17 or any(not isinstance(value, str) for value in parents):
                return None
            return set(parents)
        except (TypeError, ValueError):
            return None

    def evaluate(self, *, project_id, actor, start_at, end_at, as_of=None):
        self._project(project_id, actor)
        now = self.clock()
        as_of = now if as_of is None else as_of
        if not (0 <= start_at < end_at <= as_of <= now):
            raise ValueError("window requires 0 <= start < end <= as_of <= now")
        if as_of < now - 5:
            raise ValueError("historical as-of reads require the immutable projection Evidence; runtime counters are mutable")
        records = self.telemetry.list(actor, project_id=project_id)
        records = list({row.id:row for row in records}.values())
        evidence_rows = self.evidence.list_evidence(actor)
        evidence_by_id = {row.id:row for row in evidence_rows if row.project_id == project_id}
        reviews = [row for row in evidence_rows if
            row.project_id == project_id and row.source == SOURCE and row.observed_at <= as_of
            and row.created_at <= as_of]
        if len(records) > 5000 or len(reviews) > 10000:
            raise ValueError("outcome history exceeds bounded evaluation capacity")
        reviews_by_task = defaultdict(list)
        for review in reviews:
            reviews_by_task[review.metadata.get("task_key")].append(review)
        humans = {}
        tasks = defaultdict(list)
        # Cohort by first observed start. Retry costs and late outcomes are kept
        # with their original task, including attempts outside the enrollment window.
        for record in records:
            timestamp = record.started_at if record.started_at is not None else record.observed_at
            if timestamp <= as_of:
                tasks[self._task(record)].append(record)
        cohorts = defaultdict(list)
        for task, attempts in tasks.items():
            attempts.sort(key=lambda row: (row.started_at if row.started_at is not None else row.observed_at, row.id))
            enrolled = attempts[0].started_at if attempts[0].started_at is not None else attempts[0].observed_at
            if not start_at <= enrolled < end_at:
                continue
            latest = attempts[-1]
            history = reviews_by_task[task]
            ids = {row.id for row in history}
            superseded = {parent for row in history for parent in (self._parents(row) or ())}
            heads = [row for row in history if row.id not in superseded]
            feedback = heads[0] if len(heads) == 1 else None
            reason = None
            if len(heads) > 1:
                reason = "conflicting_feedback_requires_explicit_supersession"
            if feedback and feedback.producer_identity_id not in humans:
                humans[feedback.producer_identity_id] = self._human(feedback, actor)
            if feedback and (feedback.lifecycle != EvidenceLifecycle.VALID
                or (feedback.retention_expires_at is not None and feedback.retention_expires_at <= as_of)
                or not humans[feedback.producer_identity_id]
                or feedback.metadata.get("runtime_usage_id") != latest.id
                or feedback.execution_id != latest.execution_id
                or feedback.work_item_ref != latest.work_item_ref
                or any(self._parents(row) is None or not self._parents(row).issubset(ids) for row in history)):
                feedback, reason = None, "feedback_invalid_or_previous_attempt"
            metadata = feedback.metadata if feedback else {}
            if feedback:
                try:
                    parsed = OutcomeFeedbackCreate.model_validate({**{key: metadata[key]
                        for key in OutcomeFeedbackCreate.model_fields if key in metadata
                        and key not in {"supersedes_evidence_ids", "idempotency_key"}},
                        "supersedes_evidence_ids": tuple(self._parents(feedback) or ()),
                        "idempotency_key": feedback.external_id})
                    metadata = {**metadata, **parsed.model_dump(mode="json",
                        exclude={"idempotency_key", "supersedes_evidence_ids"})}
                except ValueError:
                    feedback, metadata, reason = None, {}, "malformed_feedback"
            outcome = metadata.get("outcome", "unknown")
            if latest.completed_at is None or latest.completed_at > as_of or latest.terminal_outcome == RuntimeTerminalOutcome.UNKNOWN:
                outcome = "pending" if latest.completed_at is None else "unknown"
            models = sorted({model for row in attempts for model in row.observed_model_ids})
            cohort = (metadata.get("workload", "unclassified"), tuple(models),
                      metadata.get("template_artifact_id"), metadata.get("template_revision"))
            def measured(field):
                values = [getattr(row, field) for row in attempts]
                return sum(values) if all(value is not None for value in values) else None
            source_ids = {eid for row in attempts for eid in row.evidence_ids}
            source_problem = any(eid not in evidence_by_id or evidence_by_id[eid].lifecycle != EvidenceLifecycle.VALID
                or (evidence_by_id[eid].retention_expires_at is not None
                    and evidence_by_id[eid].retention_expires_at <= as_of) for eid in source_ids)
            cohorts[cohort].append({"task_key": task, "outcome": outcome,
                "runtime_usage_ids": [row.id for row in attempts],
                "execution_ids": [row.execution_id for row in attempts],
                "feedback_evidence_id": feedback.id if feedback else None,
                "source_evidence_ids": sorted(eid for eid in source_ids if eid in evidence_by_id),
                "feedback_history_ids": sorted(ids), "limitation": reason or
                    ("source_evidence_missing_expired_or_outside_project" if source_problem else None),
                "feedback_head_ids": sorted(row.id for row in heads),
                "correction_required": metadata.get("correction_required"),
                "review_count": metadata.get("review_count"),
                "intervention_count": metadata.get("intervention_count"),
                "review_seconds": metadata.get("review_seconds"),
                "tokens": measured("total_tokens"), "cost_usd": measured("cost_usd"),
                "runtime_seconds": measured("runtime_duration_seconds"),
                "telemetry_partial": any(row.telemetry_completeness.value != "exact" for row in attempts)})
        result = []
        for (workload, models, template, revision), rows in sorted(cohorts.items(), key=lambda pair:str(pair[0])):
            accepted = sum(row["outcome"] == "accepted" for row in rows)
            known_reviews = [row for row in rows if row["feedback_evidence_id"]]
            def total(field):
                return sum(row[field] for row in rows) if all(row[field] is not None for row in rows) else None
            def ratio(field):
                value = total(field)
                return value / accepted if accepted and value is not None else None
            corrections = [row["correction_required"] for row in known_reviews]
            values = {"accepted_outcome_rate": accepted / len(rows),
                "human_correction_rate": (sum(corrections) / len(corrections)
                    if corrections and all(value is not None for value in corrections) else None),
                "review_count": total("review_count"), "intervention_count": total("intervention_count"),
                "review_seconds": total("review_seconds"), "tokens_per_accepted_outcome": ratio("tokens"),
                "cost_usd_per_accepted_outcome": ratio("cost_usd"),
                "runtime_seconds_per_accepted_outcome": ratio("runtime_seconds")}
            partial = (len(known_reviews) != len(rows) or any(value is None for value in values.values())
                       or any(row["telemetry_partial"] or row["limitation"] for row in rows))
            result.append({"workload": workload, "model_ids": list(models),
                "template_artifact_id": template, "template_revision": revision,
                "eligible_count": len(rows), "accepted_count": accepted,
                "outcome_counts": {state.value: sum(row["outcome"] == state.value for row in rows) for state in UserOutcome},
                "correction_observation_count": sum(value is not None for value in corrections),
                "values": values, "status": "partial" if partial else "fresh", "tasks": rows})
        eligible_count = sum(cohort["eligible_count"] for cohort in result)
        accepted_count = sum(cohort["accepted_count"] for cohort in result)
        return {"formula_version": FORMULA_VERSION, "project_id": project_id,
            "eligible_count":eligible_count, "accepted_count":accepted_count,
            "accepted_outcome_rate":accepted_count / eligible_count if eligible_count else None,
            "window": {"start_at": start_at, "end_at": end_at, "as_of": as_of},
            "cohorts": result, "status": "missing" if not result else
                "stale" if as_of - end_at > 86400 else "partial" if any(row["status"] == "partial" for row in result) else "fresh",
            "limitations": ["Explicit human observations; not independent verification or policy approval.",
                "Unreviewed, reopened and uncertain tasks are eligible but never accepted.",
                "Runtime duration is not human review effort; missing measurements remain null.",
                "Cohorts are observational; matching workload/model/template does not establish causal comparability."]}

    def publish(self, report, *, actor):
        """Explicitly materialize an exact projection in existing Metric state."""
        self._project(report["project_id"], actor)
        if report["formula_version"] != FORMULA_VERSION:
            raise ValueError("outcome projection version is incompatible with this engine")
        observations = []
        fingerprint = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
        snapshot = self.evidence.create_evidence(EvidenceCreate(
            project_id=report["project_id"], evidence_type=EvidenceType.RUNTIME_USAGE,
            source=FORMULA_VERSION, result=EvidenceResult.INFO,
            summary="Deterministic outcome metric projection; no completion or approval authority.",
            deny_model_context=True, metadata={"formula_version":FORMULA_VERSION,
                "projection_sha256":fingerprint, "projection_json":json.dumps(report, sort_keys=True)}),
            actor=actor, idempotency_key=fingerprint, source_evidence_ids=tuple(sorted({eid
                for cohort in report["cohorts"] for task in cohort["tasks"]
                for eid in [*task["feedback_history_ids"], *task["source_evidence_ids"]]})))
        for cohort in report["cohorts"]:
            cohort_key = hashlib.sha256(json.dumps({"project_id":report["project_id"],
                **{key:cohort[key] for key in ("workload", "model_ids", "template_artifact_id", "template_revision")}},
                sort_keys=True).encode()).hexdigest()
            definitions = {row.key:row for row in self.metrics.list_definitions(scope=actor.tenant, project_id=report["project_id"])}
            for name, value in cohort["values"].items():
                if value is None:
                    continue
                key = f"outcomes.v1.{cohort_key}.{name}"
                definition = definitions.get(key)
                unit = ("ratio" if name.endswith("rate") else "USD" if name.startswith("cost")
                    else "seconds" if "seconds" in name else "tokens" if name.startswith("tokens") else "count")
                if definition is None:
                    definition = self.metrics.create_definition(MetricDefinitionCreate(
                        key=key, name=name.replace("_", " ").capitalize(),
                        description=f"{FORMULA_VERSION}; workload={cohort['workload']}; {name}. See exact observation Evidence and outcome window.",
                        owner_identity_id=actor.identity_id, unit=unit,
                        source_requirements=(FORMULA_VERSION,), freshness_seconds=86400, project_id=report["project_id"]),
                        scope=actor.tenant, actor_id=actor.identity_id)
                elif (definition.unit != unit or definition.aggregation != MetricAggregation.LAST
                    or definition.value_type != MetricValueType.NUMBER
                    or definition.source_requirements != (FORMULA_VERSION,)):
                    raise ValueError("outcome Metric definition is incompatible with its calculation contract")
                evidence_ids = tuple(sorted({snapshot.id} | {eid for task in cohort["tasks"] for eid in
                    [*task["source_evidence_ids"], *task["feedback_history_ids"]]}))
                observation = self.metrics.ingest(definition.id, MetricObservationCreate(
                    value=float(value), source=FORMULA_VERSION, evidence_ids=evidence_ids,
                    observed_at=report["window"]["end_at"], window_start=report["window"]["start_at"],
                    window_end=report["window"]["end_at"], partial=cohort["status"] == "partial",
                    external_record_ref=f"{FORMULA_VERSION}:{fingerprint}", idempotency_key=snapshot.id + ":" + name),
                    scope=actor.tenant, actor_id=actor.identity_id)
                observations.append({"metric_id": definition.id, "metric_revision": definition.revision,
                                     "observation_id": observation.id, "projection_evidence_id":snapshot.id})
        return observations
