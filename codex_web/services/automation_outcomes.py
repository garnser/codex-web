from __future__ import annotations

from codex_web.automation_runs import AutomationRun, AutomationRunStatus
from codex_web.execution_workers import AssignmentStatus
from codex_web.identity import AuthenticationActor
from codex_web.services.automation_runs import AutomationRunService
from codex_web.services.execution_workers import (
    AssignmentNotFoundError,
    ExecutionWorkerService,
)


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
    ) -> None:
        self.runs = runs
        self.workers = workers

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

        evidence_ids = tuple(
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
        return self.runs.complete(
            run.id,
            organization_id=run.organization_id,
            workspace_id=run.workspace_id,
            succeeded=succeeded,
            evidence_ids=evidence_ids,
        )
