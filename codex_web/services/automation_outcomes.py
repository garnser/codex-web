from __future__ import annotations

from codex_web.attention import AttentionItemCreate, AttentionSeverity, AttentionSource
from codex_web.automation_definitions import AutomationTargetKind
from codex_web.automation_runs import AutomationRun, AutomationRunStatus
from codex_web.execution_workers import AssignmentStatus
from codex_web.identity import AuthenticationActor
from codex_web.services.attention import AttentionService
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
        *,
        attention: AttentionService | None = None,
    ) -> None:
        self.runs = runs
        self.workers = workers
        self.attention = attention

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
