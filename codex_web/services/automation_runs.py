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

    @staticmethod
    def _dedupe_occurrence(
        trigger: AutomationRunTrigger,
        idempotency_key: str | None,
    ) -> str:
        supplied = str(idempotency_key or "").strip()
        if supplied:
            return supplied
        if trigger.event_id:
            return trigger.event_id
        if trigger.schedule_id and trigger.scheduled_for is not None:
            return f"{trigger.schedule_id}:{float(trigger.scheduled_for)!r}"
        return trigger.source_id

    @classmethod
    def policy_dedupe_key(
        cls,
        *,
        definition_record_id: str,
        automation_id: str,
        project_id: str | None,
        template: str | None,
        trigger: AutomationRunTrigger,
        idempotency_key: str | None = None,
    ) -> str:
        normalized_template = str(template or "").strip()
        if not normalized_template:
            return cls.trigger_dedupe_key(
                definition_record_id=definition_record_id,
                trigger=trigger,
                idempotency_key=idempotency_key,
            )
        rendered = normalized_template.format(
            automation=automation_id,
            project=str(project_id or ""),
            occurrence=cls._dedupe_occurrence(trigger, idempotency_key),
            event=str(trigger.event_id or ""),
            schedule=str(trigger.schedule_id or ""),
            source=trigger.source_id,
        ).strip()
        if not rendered:
            raise ValueError("Automation dedupe policy rendered an empty key")
        return f"{definition_record_id}:policy:{rendered}"

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
        dedupe_key_override: str | None = None,
        attempt: int = 1,
        work_item_ref: str | None = None,
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
        dedupe_key = str(dedupe_key_override or "").strip()
        if not dedupe_key:
            dedupe_key = self.policy_dedupe_key(
                definition_record_id=reference.record_id,
                automation_id=automation_id,
                project_id=project_id,
                template=automation.dedupe_key_template,
                trigger=trigger,
                idempotency_key=idempotency_key,
            )
        if len(dedupe_key) > 1000:
            raise ValueError("Automation dedupe key exceeds canonical limit")
        if attempt < 1:
            raise ValueError("Automation attempt must be at least one")
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
            work_item_ref=work_item_ref,
            attempt=attempt,
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



    def wait_for_work_item(
        self,
        run_id: str,
        *,
        organization_id: str,
        workspace_id: str,
        action_intent_id: str,
    ) -> AutomationRun:
        current = self.store.get(
            run_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
        )
        if current.status != AutomationRunStatus.ADMITTED:
            raise ValueError(
                "only admitted Automation runs can wait for Work Item creation"
            )
        intent_id = str(action_intent_id or "").strip()
        if not intent_id:
            raise ValueError("Work Item wait requires an ActionIntent id")
        now = float(self.clock())
        return self.store.replace(
            current.model_copy(
                update={
                    "status": AutomationRunStatus.WAITING_FOR_WORK_ITEM,
                    "work_item_action_intent_id": intent_id,
                    "updated_at": now,
                    "completed_at": None,
                }
            )
        )

    def resume_with_work_item(
        self,
        run_id: str,
        *,
        organization_id: str,
        workspace_id: str,
        work_item_ref: str,
    ) -> AutomationRun:
        current = self.store.get(
            run_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
        )
        if current.status != AutomationRunStatus.WAITING_FOR_WORK_ITEM:
            raise ValueError(
                "only Work-Item-waiting Automation runs can resume"
            )
        ref = str(work_item_ref or "").strip()
        if not ref:
            raise ValueError("Automation resume requires a canonical Work Item ref")
        now = float(self.clock())
        return self.store.replace(
            current.model_copy(
                update={
                    "status": AutomationRunStatus.ADMITTED,
                    "work_item_ref": ref,
                    "block_code": None,
                    "block_reason": None,
                    "updated_at": now,
                    "completed_at": None,
                }
            )
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
        if current.status not in {
            AutomationRunStatus.ADMITTED,
            AutomationRunStatus.WAITING_FOR_WORK_ITEM,
        }:
            raise ValueError(
                "only admitted or Work-Item-waiting Automation runs can be blocked"
            )
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
        result_code: str | None = None,
        result_reason: str | None = None,
    ) -> AutomationRun:
        current = self.store.get(
            run_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
        )
        if current.status not in {
            AutomationRunStatus.ADMITTED,
            AutomationRunStatus.RUNNING,
        }:
            raise ValueError("Automation run is not completable")
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
                    "result_code": result_code,
                    "result_reason": result_reason,
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
        trigger_type = str(event.payload.get("trigger_type") or "")
        if trigger_type not in {"automation.definition", "automation.retry"}:
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
        trigger = AutomationRunTrigger(
            kind=AutomationRunTriggerKind.SCHEDULE,
            source_id=f"{schedule_id}:{float(scheduled_for)!r}",
            schedule_id=schedule_id,
            scheduled_for=float(scheduled_for),
            occurred_at=event.occurred_at,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
        )

        if trigger_type == "automation.definition":
            return self.runs.admit(
                automation_id,
                trigger,
                organization_id=organization_id,
                workspace_id=workspace_id,
                project_id=project_id,
                definition_ref=reference,
            )

        retry_of_run_id = str(materialized.get("retry_of_run_id") or "").strip()
        try:
            retry_attempt = int(materialized.get("retry_attempt"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Automation retry schedule lacks retry attempt") from exc
        if not retry_of_run_id or retry_attempt < 2:
            raise ValueError("Automation retry schedule lacks canonical retry provenance")

        prior = self.runs.store.get(
            retry_of_run_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
        )
        if (
            prior.status != AutomationRunStatus.FAILED
            or prior.automation_id != automation_id
            or prior.definition_ref != reference
            or retry_attempt != prior.attempt + 1
            or prior.project_id != project_id
        ):
            raise ValueError("Automation retry schedule provenance mismatch")

        current_automation, current_reference = self.runs.definitions.resolve(
            automation_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            project_id=project_id,
        )
        del current_automation
        result = self.runs.admit(
            automation_id,
            trigger,
            organization_id=organization_id,
            workspace_id=workspace_id,
            project_id=project_id,
            definition_ref=current_reference,
            dedupe_key_override=(
                f"{reference.record_id}:retry:{prior.id}:{retry_attempt}"
            ),
            attempt=retry_attempt,
            work_item_ref=prior.work_item_ref,
        )
        if (
            not result.inserted
            or result.run.status != AutomationRunStatus.ADMITTED
        ):
            return result
        if current_reference != reference:
            blocked = self.runs.block(
                result.run.id,
                organization_id=organization_id,
                workspace_id=workspace_id,
                code="automation_retry_revision_changed",
                reason=(
                    "Automation Definition changed after the failed attempt; "
                    "automatic retry requires operator review."
                ),
            )
            return AutomationAdmissionResult(
                run=blocked,
                inserted=True,
                launch_allowed=False,
            )
        return result

    def install(self) -> Callable[[], None]:
        async def on_event(event: CanonicalEventEnvelope) -> None:
            for match in self.event_triggers.matches(event):
                await self._launch(self._admit_event_match(event, match))
            await self._launch(self._admit_schedule(event))

        return self.bus.subscribe(on_event)
