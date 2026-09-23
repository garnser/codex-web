from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from codex_web.automation_definitions import AutomationLifecycle
from codex_web.automation_runs import (
    ACTIVE_AUTOMATION_RUN_STATUSES,
    AutomationRun,
    AutomationRunStatus,
    AutomationRunTrigger,
)
from codex_web.services.automation_definitions import AutomationDefinitionService
from codex_web.storage.automation_runs import AutomationRunStore


Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class AutomationAdmissionResult:
    run: AutomationRun
    inserted: bool
    launch_allowed: bool


class AutomationRunService:
    """Canonical trigger admission/history shared by every Automation trigger path."""

    def __init__(
        self,
        store: AutomationRunStore,
        definitions: AutomationDefinitionService,
        *,
        clock: Clock = time.time,
    ) -> None:
        self.store = store
        self.definitions = definitions
        self.clock = clock

    @staticmethod
    def trigger_dedupe_key(
        *,
        definition_record_id: str,
        trigger: AutomationRunTrigger,
        idempotency_key: str | None = None,
    ) -> str:
        supplied = str(idempotency_key or "").strip()
        if supplied:
            return f"{definition_record_id}:manual:{supplied}"
        if trigger.event_id:
            return f"{definition_record_id}:event:{trigger.event_id}"
        if trigger.schedule_id and trigger.scheduled_for is not None:
            return (
                f"{definition_record_id}:schedule:{trigger.schedule_id}:"
                f"{float(trigger.scheduled_for)!r}"
            )
        return f"{definition_record_id}:source:{trigger.source_id}"

    def admit(
        self,
        automation_id: str,
        trigger: AutomationRunTrigger,
        *,
        organization_id: str,
        workspace_id: str,
        project_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> AutomationAdmissionResult:
        automation, reference = self.definitions.resolve(
            automation_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            project_id=project_id,
        )
        dedupe_key = self.trigger_dedupe_key(
            definition_record_id=reference.record_id,
            trigger=trigger,
            idempotency_key=idempotency_key,
        )
        existing = self.store.find_by_dedupe(
            dedupe_key,
            organization_id=organization_id,
            workspace_id=workspace_id,
        )
        if existing is not None:
            return AutomationAdmissionResult(
                run=existing,
                inserted=False,
                launch_allowed=existing.status == AutomationRunStatus.ADMITTED,
            )

        active = [
            run
            for run in self.store.list(
                organization_id=organization_id,
                workspace_id=workspace_id,
                automation_id=automation_id,
            )
            if run.status in ACTIVE_AUTOMATION_RUN_STATUSES
        ]
        block_code = None
        block_reason = None
        status = AutomationRunStatus.ADMITTED
        if automation.lifecycle != AutomationLifecycle.ENABLED:
            status = AutomationRunStatus.BLOCKED
            block_code = "automation_paused"
            block_reason = "Automation is paused; trigger recorded without execution"
        elif len(active) >= automation.budget.max_concurrency:
            status = AutomationRunStatus.BLOCKED
            block_code = "automation_concurrency_exhausted"
            block_reason = (
                "Automation concurrency budget is exhausted; "
                "trigger recorded without execution"
            )

        now = float(self.clock())
        run = AutomationRun(
            organization_id=organization_id,
            workspace_id=workspace_id,
            project_id=project_id,
            automation_id=automation_id,
            definition_ref=reference,
            trigger=trigger,
            dedupe_key=dedupe_key,
            status=status,
            target_kind=automation.target.kind.value,
            target_id=automation.target.id,
            block_code=block_code,
            block_reason=block_reason,
            created_at=now,
            updated_at=now,
            completed_at=now if status == AutomationRunStatus.BLOCKED else None,
        )
        saved = self.store.append(run)
        inserted = saved.id == run.id
        return AutomationAdmissionResult(
            run=saved,
            inserted=inserted,
            launch_allowed=inserted and saved.status == AutomationRunStatus.ADMITTED,
        )

    def mark_running(
        self,
        run_id: str,
        *,
        organization_id: str,
        workspace_id: str,
        work_item_ref: str | None = None,
        execution_ids: tuple[str, ...] = (),
    ) -> AutomationRun:
        current = self.store.get(
            run_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
        )
        if current.status != AutomationRunStatus.ADMITTED:
            raise ValueError("only admitted Automation runs can start")
        now = float(self.clock())
        return self.store.replace(
            current.model_copy(
                update={
                    "status": AutomationRunStatus.RUNNING,
                    "work_item_ref": work_item_ref,
                    "execution_ids": execution_ids,
                    "started_at": now,
                    "updated_at": now,
                }
            )
        )

    def complete(
        self,
        run_id: str,
        *,
        organization_id: str,
        workspace_id: str,
        succeeded: bool,
        evidence_ids: tuple[str, ...] = (),
    ) -> AutomationRun:
        current = self.store.get(
            run_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
        )
        if current.status not in ACTIVE_AUTOMATION_RUN_STATUSES:
            raise ValueError("Automation run is not active")
        now = float(self.clock())
        return self.store.replace(
            current.model_copy(
                update={
                    "status": (
                        AutomationRunStatus.SUCCEEDED
                        if succeeded
                        else AutomationRunStatus.FAILED
                    ),
                    "evidence_ids": evidence_ids,
                    "updated_at": now,
                    "completed_at": now,
                }
            )
        )
