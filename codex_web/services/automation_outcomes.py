from __future__ import annotations

import time
from typing import Any

from codex_web.attention import AttentionItemCreate, AttentionSeverity, AttentionSource
from codex_web.automation_definitions import AutomationTargetKind
from codex_web.automation_runs import AutomationRun, AutomationRunStatus
from codex_web.execution_workers import AssignmentStatus
from codex_web.identity import AuthenticationActor
from codex_web.scheduler import MisfirePolicy, ScheduleCreate
from codex_web.services.attention import AttentionService
from codex_web.services.automation_runs import AutomationRunService
from codex_web.services.execution_workers import (
    AssignmentNotFoundError,
    ExecutionWorkerService,
)
from codex_web.services.scheduler import SchedulerService


class AutomationOutcomeReconciliationService:
    """Project canonical execution-assignment outcomes onto Automation runs."""

    _TERMINAL = {
        AssignmentStatus.SUCCEEDED,
        AssignmentStatus.FAILED,
        AssignmentStatus.CANCELLED,
        AssignmentStatus.LOST,
    }

    def __init__(
        self,
        runs: AutomationRunService,
        workers: ExecutionWorkerService,
        *,
        attention: AttentionService | None = None,
        runtime_usage: Any | None = None,
        scheduler: SchedulerService | None = None,
        clock=time.time,
    ) -> None:
        self.runs = runs
        self.workers = workers
        self.attention = attention
        self.runtime_usage = runtime_usage
        self.scheduler = scheduler
        self.clock = clock

    def reconcile_for_execution(
        self,
        execution_id: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[AutomationRun, ...]:
        """Reconcile running Automation runs that own one execution id."""
        matches = [
            run
            for run in self.runs.store.list(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
            )
            if run.status == AutomationRunStatus.RUNNING
            and execution_id in run.execution_ids
        ]
        return tuple(
            self.reconcile(run.id, actor=actor)
            for run in matches
        )

    @staticmethod
    def _failure_attention_key(run: AutomationRun) -> str:
        return f"automation-run:{run.id}:failure"

    async def sync_attention(
        self,
        run: AutomationRun,
        *,
        actor: AuthenticationActor,
    ) -> None:
        """Project terminal Automation failure into canonical Attention."""
        if self.attention is None:
            return
        dedupe_key = self._failure_attention_key(run)
        if run.status == AutomationRunStatus.SUCCEEDED:
            await self.attention.resolve_by_source(
                dedupe_key,
                organization_id=run.organization_id,
                workspace_id=run.workspace_id,
                actor_id=actor.identity_id,
                reason="Automation run recovered successfully",
            )
            return
        if run.status != AutomationRunStatus.FAILED:
            return

        automation, _reference = self.runs.definitions.resolve_reference(
            run.definition_ref,
            organization_id=run.organization_id,
            workspace_id=run.workspace_id,
            project_id=run.project_id,
        )
        if not automation.failure_attention:
            return

        requesting_profile = (
            automation.target.id
            if automation.target.kind == AutomationTargetKind.AGENT_PROFILE
            else None
        )
        requesting_team = (
            automation.target.id
            if automation.target.kind == AutomationTargetKind.TEAM
            else None
        )
        await self.attention.upsert(
            AttentionItemCreate(
                organization_id=run.organization_id,
                workspace_id=run.workspace_id,
                project_id=run.project_id,
                type="automation.failed",
                severity=AttentionSeverity.HIGH,
                source=AttentionSource(
                    object_type="automation_run",
                    object_id=run.id,
                ),
                reason=(
                    f"Automation {run.automation_id} failed after canonical "
                    "execution completed."
                ),
                dedupe_key=dedupe_key,
                owner_identity_id=automation.owner_identity_id,
                requesting_agent_profile_id=requesting_profile,
                requesting_agent_team_id=requesting_team,
                evidence_ids=run.evidence_ids,
                diagnostic_refs=tuple(
                    f"execution:{execution_id}"
                    for execution_id in run.execution_ids
                ),
            ),
            actor_id=actor.identity_id,
        )

    def _usage_records(self, run: AutomationRun):
        if self.runtime_usage is None:
            return []
        execution_ids = set(run.execution_ids)
        return [
            record
            for record in self.runtime_usage.list()
            if record.organization_id == run.organization_id
            and record.workspace_id == run.workspace_id
            and record.execution_id in execution_ids
        ]

    @staticmethod
    def _execution_failure(assignments) -> tuple[str, str]:
        failed = next(
            (item for item in assignments if item.status != AssignmentStatus.SUCCEEDED),
            assignments[0],
        )
        failure = getattr(failed, "failure", None)
        reason = getattr(getattr(failure, "reason", None), "value", None)
        code = (
            reason
            or getattr(failed, "failure_code", None)
            or f"assignment_{failed.status.value}"
        )
        summary = (
            getattr(failure, "summary", None)
            or getattr(failed, "failure_message", None)
            or f"canonical execution ended as {failed.status.value}"
        )
        return f"automation_execution_{code}", str(summary)[:1000]

    @staticmethod
    def _automatic_retry_safe(assignments) -> bool:
        if not assignments:
            return False
        for assignment in assignments:
            if assignment.status not in {
                AssignmentStatus.FAILED,
                AssignmentStatus.LOST,
            }:
                return False
            failure = getattr(assignment, "failure", None)
            if failure is None or not failure.automatic_retry_allowed:
                return False
        return True

    def _existing_retry_schedule(
        self,
        run: AutomationRun,
        *,
        next_attempt: int,
    ):
        if self.scheduler is None:
            return None
        return next(
            (
                schedule
                for schedule in self.scheduler.list()
                if schedule.trigger_type == "automation.retry"
                and schedule.tenant_id == run.organization_id
                and schedule.workspace_id == run.workspace_id
                and schedule.payload.get("retry_of_run_id") == run.id
                and schedule.payload.get("retry_attempt") == next_attempt
            ),
            None,
        )

    def _schedule_retry(
        self,
        run: AutomationRun,
        assignments,
        automation,
        *,
        actor: AuthenticationActor,
    ):
        if self.scheduler is None:
            return None
        if run.attempt >= automation.retry.max_attempts:
            return None
        if not self._automatic_retry_safe(assignments):
            return None

        next_attempt = run.attempt + 1
        existing = self._existing_retry_schedule(
            run,
            next_attempt=next_attempt,
        )
        if existing is not None:
            return existing

        due_at = float(self.clock()) + float(automation.retry.backoff_seconds)
        reference = run.definition_ref
        return self.scheduler.create(
            ScheduleCreate(
                name=f"Automation retry: {automation.name}",
                tenant_id=run.organization_id,
                workspace_id=run.workspace_id,
                trigger_type="automation.retry",
                payload={
                    "automation_id": run.automation_id,
                    "definition_record_id": reference.record_id,
                    "definition_revision": reference.revision,
                    "definition_checksum": reference.checksum,
                    "project_id": run.project_id,
                    "retry_of_run_id": run.id,
                    "retry_attempt": next_attempt,
                    "work_item_ref": run.work_item_ref,
                },
                due_at=due_at,
                misfire_policy=MisfirePolicy.FIRE_ONCE,
            ),
            actor_id=actor.identity_id,
        )

    def _budget_evaluation(
        self,
        run: AutomationRun,
        assignments,
        automation,
    ) -> tuple[str, str | None, str | None, tuple[str, ...]]:
        budget = automation.budget
        metric_limits = (
            ("input_tokens", budget.max_input_tokens, "input token"),
            ("output_tokens", budget.max_output_tokens, "output token"),
            ("cost_usd", budget.max_cost_usd, "cost"),
        )
        configured = [
            (field, limit, label)
            for field, limit, label in metric_limits
            if limit is not None
        ]
        records = self._usage_records(run)
        usage_evidence = tuple(
            dict.fromkeys(
                evidence_id
                for record in records
                for evidence_id in getattr(record, "evidence_ids", ())
            )
        )

        if configured:
            if self.runtime_usage is None:
                return (
                    "failed",
                    "automation_budget_telemetry_unavailable",
                    "Automation budget requires runtime usage telemetry, but the canonical usage store is unavailable.",
                    usage_evidence,
                )
            by_execution = {
                execution_id: [
                    record
                    for record in records
                    if record.execution_id == execution_id
                ]
                for execution_id in run.execution_ids
            }
            if any(not values for values in by_execution.values()):
                # Assignment completion can race terminal runtime telemetry.
                # Keep the run pending until the telemetry observer retries
                # reconciliation rather than treating missing data as zero.
                return "pending", None, None, usage_evidence

            for field, limit, label in configured:
                total = 0.0
                for execution_id, values in by_execution.items():
                    measured = [
                        getattr(record, field)
                        for record in values
                        if getattr(record, field) is not None
                    ]
                    if not measured:
                        return (
                            "failed",
                            "automation_budget_unverifiable",
                            (
                                f"Configured {label} budget cannot be verified for "
                                f"execution {execution_id}; canonical runtime telemetry "
                                f"did not expose {field}."
                            ),
                            usage_evidence,
                        )
                    total += sum(float(value) for value in measured)
                if total > float(limit):
                    unit = " USD" if field == "cost_usd" else ""
                    return (
                        "failed",
                        f"automation_{field}_budget_exhausted",
                        (
                            f"Automation {label} budget exceeded: "
                            f"{total:g}{unit} > {float(limit):g}{unit}."
                        ),
                        usage_evidence,
                    )

        if budget.max_duration_seconds is not None:
            if run.started_at is None:
                return (
                    "failed",
                    "automation_budget_unverifiable",
                    "Configured duration budget cannot be verified because the Automation run has no canonical start time.",
                    usage_evidence,
                )
            completed_at = [
                getattr(item, "completed_at", None)
                for item in assignments
            ]
            if any(value is None for value in completed_at):
                return (
                    "failed",
                    "automation_budget_unverifiable",
                    "Configured duration budget cannot be verified because a terminal assignment has no completion time.",
                    usage_evidence,
                )
            elapsed = max(float(value) for value in completed_at) - float(run.started_at)
            if elapsed > float(budget.max_duration_seconds):
                return (
                    "failed",
                    "automation_duration_budget_exhausted",
                    (
                        f"Automation duration budget exceeded: "
                        f"{elapsed:g}s > {budget.max_duration_seconds}s."
                    ),
                    usage_evidence,
                )

        return "ok", None, None, usage_evidence

    def reconcile(
        self,
        run_id: str,
        *,
        actor: AuthenticationActor,
    ) -> AutomationRun:
        run = self.runs.store.get(
            run_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        if run.status != AutomationRunStatus.RUNNING or not run.execution_ids:
            return run

        assignments = []
        for execution_id in run.execution_ids:
            try:
                assignment = self.workers.assignment_for_execution(
                    execution_id,
                    actor=actor,
                )
            except AssignmentNotFoundError:
                # A turn can be launched before the canonical worker assignment
                # is materialized. Missing assignment state is therefore pending,
                # not evidence of failure.
                return run
            assignments.append(assignment)

        if any(item.status not in self._TERMINAL for item in assignments):
            return run

        assignment_evidence_ids = tuple(
            dict.fromkeys(
                evidence_id
                for item in assignments
                for evidence_id in item.evidence_ids
            )
        )
        succeeded = all(
            item.status == AssignmentStatus.SUCCEEDED
            for item in assignments
        )
        if not succeeded:
            automation, _reference = self.runs.definitions.resolve_reference(
                run.definition_ref,
                organization_id=run.organization_id,
                workspace_id=run.workspace_id,
                project_id=run.project_id,
            )
            retry_schedule = self._schedule_retry(
                run,
                assignments,
                automation,
                actor=actor,
            )
            result_code, result_reason = self._execution_failure(assignments)
            if retry_schedule is not None:
                result_reason = (
                    f"{result_reason} Retry attempt {run.attempt + 1} is "
                    f"scheduled for {retry_schedule.due_at:g}."
                )[:1000]
            return self.runs.complete(
                run.id,
                organization_id=run.organization_id,
                workspace_id=run.workspace_id,
                succeeded=False,
                evidence_ids=assignment_evidence_ids,
                result_code=result_code,
                result_reason=result_reason,
            )

        automation, _reference = self.runs.definitions.resolve_reference(
            run.definition_ref,
            organization_id=run.organization_id,
            workspace_id=run.workspace_id,
            project_id=run.project_id,
        )
        budget_state, result_code, result_reason, usage_evidence_ids = (
            self._budget_evaluation(run, assignments, automation)
        )
        if budget_state == "pending":
            return run
        evidence_ids = tuple(
            dict.fromkeys(
                (*assignment_evidence_ids, *usage_evidence_ids)
            )
        )
        if budget_state == "failed":
            return self.runs.complete(
                run.id,
                organization_id=run.organization_id,
                workspace_id=run.workspace_id,
                succeeded=False,
                evidence_ids=evidence_ids,
                result_code=result_code,
                result_reason=result_reason,
            )
        return self.runs.complete(
            run.id,
            organization_id=run.organization_id,
            workspace_id=run.workspace_id,
            succeeded=True,
            evidence_ids=evidence_ids,
            result_code="automation_succeeded",
            result_reason="Canonical execution completed within configured Automation budgets.",
        )
