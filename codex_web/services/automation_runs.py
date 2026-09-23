from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from codex_web.automation_definitions import AutomationLifecycle, AutomationTriggerType
from codex_web.automation_runs import (
    ACTIVE_AUTOMATION_RUN_STATUSES,
    AutomationRun,
    AutomationRunStatus,
    AutomationRunTrigger,
    AutomationRunTriggerKind,
)
from codex_web.canonical_events import CanonicalEventType
from codex_web.compatibility import CanonicalEventEnvelope
from codex_web.definitions import DefinitionReference, reference_for
from codex_web.services.automation_definitions import (
    AutomationDefinitionService,
    AutomationEventTriggerService,
)
from codex_web.services.canonical_events import CanonicalEventBus
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
        definition_ref: DefinitionReference | None = None,
    ) -> AutomationAdmissionResult:
        if definition_ref is None:
            automation, reference = self.definitions.resolve(
                automation_id,
                organization_id=organization_id,
                workspace_id=workspace_id,
                project_id=project_id,
            )
        else:
            if definition_ref.definition_id != automation_id:
                raise ValueError("Automation Definition reference id does not match trigger")
            automation, reference = self.definitions.resolve_reference(
                definition_ref,
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
                launch_allowed=False,
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
                    "execution_ids": tuple(dict.fromkeys(execution_ids)),
                    "started_at": now,
                    "updated_at": now,
                }
            )
        )

    def block(
        self,
        run_id: str,
        *,
        organization_id: str,
        workspace_id: str,
        code: str,
        reason: str,
    ) -> AutomationRun:
        current = self.store.get(
            run_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
        )
        if current.status != AutomationRunStatus.ADMITTED:
            raise ValueError("only admitted Automation runs can be blocked")
        now = float(self.clock())
        return self.store.replace(
            current.model_copy(
                update={
                    "status": AutomationRunStatus.BLOCKED,
                    "block_code": str(code or "automation_launch_blocked")[:200],
                    "block_reason": str(reason or "Automation launch blocked")[:2000],
                    "updated_at": now,
                    "completed_at": now,
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
                    "evidence_ids": tuple(dict.fromkeys(evidence_ids)),
                    "updated_at": now,
                    "completed_at": now,
                }
            )
        )



class AutomationTriggerAdmissionBridge:
    """Feed manual, event and schedule triggers through one canonical admission ledger."""

    def __init__(
        self,
        runs: AutomationRunService,
        event_triggers: AutomationEventTriggerService,
        bus: CanonicalEventBus,
        *,
        launch_handler: Callable[[AutomationRun], Awaitable[Any]] | None = None,
    ) -> None:
        self.runs = runs
        self.event_triggers = event_triggers
        self.bus = bus
        self.launch_handler = launch_handler

    def set_launch_handler(
        self,
        handler: Callable[[AutomationRun], Awaitable[Any]] | None,
    ) -> None:
        self.launch_handler = handler

    async def _launch(self, result: AutomationAdmissionResult | None) -> None:
        if (
            result is None
            or not result.launch_allowed
            or self.launch_handler is None
        ):
            return
        await self.launch_handler(result.run)

    @staticmethod
    def _scope(event: CanonicalEventEnvelope) -> tuple[str, str, str | None]:
        organization_id = str(event.tenant_id or "").strip()
        workspace_id = str(event.workspace_id or "").strip()
        project_id = str(event.payload.get("project_id") or "").strip() or None
        if not organization_id or not workspace_id:
            raise ValueError(
                "Automation trigger requires canonical tenant/workspace scope"
            )
        return organization_id, workspace_id, project_id

    def manual(
        self,
        automation_id: str,
        *,
        organization_id: str,
        workspace_id: str,
        idempotency_key: str,
        project_id: str | None = None,
        actor_id: str,
    ) -> AutomationAdmissionResult:
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("manual Automation trigger requires idempotency_key")
        return self.runs.admit(
            automation_id,
            AutomationRunTrigger(
                kind=AutomationRunTriggerKind.MANUAL,
                source_id=f"manual:{actor_id}:{key}",
            ),
            organization_id=organization_id,
            workspace_id=workspace_id,
            project_id=project_id,
            idempotency_key=key,
        )

    def _admit_event_match(
        self,
        event: CanonicalEventEnvelope,
        match,
    ) -> AutomationAdmissionResult:
        organization_id, workspace_id, project_id = self._scope(event)
        kind = (
            AutomationRunTriggerKind.PROVIDER_EVENT
            if match.automation.trigger.type == AutomationTriggerType.PROVIDER_EVENT
            else AutomationRunTriggerKind.CANONICAL_EVENT
        )
        return self.runs.admit(
            match.automation_id,
            AutomationRunTrigger(
                kind=kind,
                source_id=event.event_id,
                event_id=event.event_id,
                occurred_at=event.occurred_at,
                correlation_id=event.correlation_id,
                causation_id=event.causation_id,
            ),
            organization_id=organization_id,
            workspace_id=workspace_id,
            project_id=project_id,
            definition_ref=match.definition_ref,
        )

    def _admit_schedule(
        self,
        event: CanonicalEventEnvelope,
    ) -> AutomationAdmissionResult | None:
        if event.event_type != CanonicalEventType.SCHEDULE.value:
            return None
        if event.payload.get("trigger_type") != "automation.definition":
            return None
        materialized = event.payload.get("payload")
        if not isinstance(materialized, dict):
            return None
        automation_id = str(materialized.get("automation_id") or "").strip()
        record_id = str(materialized.get("definition_record_id") or "").strip()
        schedule_id = str(event.payload.get("schedule_id") or "").strip()
        scheduled_for = event.payload.get("scheduled_for")
        if not automation_id or not record_id or not schedule_id or scheduled_for is None:
            raise ValueError("Automation schedule event lacks canonical provenance")

        record = self.runs.definitions.registry.get_record(record_id)
        reference = reference_for(record)
        if (
            reference.definition_id != automation_id
            or reference.revision != materialized.get("definition_revision")
            or reference.checksum != materialized.get("definition_checksum")
        ):
            raise ValueError("Automation schedule Definition provenance mismatch")

        organization_id = str(event.tenant_id or "").strip()
        workspace_id = str(event.workspace_id or "").strip()
        project_id = str(materialized.get("project_id") or "").strip() or None
        if not organization_id or not workspace_id:
            raise ValueError(
                "Automation schedule trigger requires tenant/workspace scope"
            )
        return self.runs.admit(
            automation_id,
            AutomationRunTrigger(
                kind=AutomationRunTriggerKind.SCHEDULE,
                source_id=f"{schedule_id}:{float(scheduled_for)!r}",
                schedule_id=schedule_id,
                scheduled_for=float(scheduled_for),
                occurred_at=event.occurred_at,
                correlation_id=event.correlation_id,
                causation_id=event.causation_id,
            ),
            organization_id=organization_id,
            workspace_id=workspace_id,
            project_id=project_id,
            definition_ref=reference,
        )

    def install(self) -> Callable[[], None]:
        async def on_event(event: CanonicalEventEnvelope) -> None:
            for match in self.event_triggers.matches(event):
                await self._launch(self._admit_event_match(event, match))
            await self._launch(self._admit_schedule(event))

        return self.bus.subscribe(on_event)
