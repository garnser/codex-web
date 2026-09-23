from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from codex_web.automation_definitions import (
    AUTOMATION_KIND,
    AUTOMATION_SCHEMA_VERSION,
    AutomationDefinition,
    AutomationLifecycle,
    AutomationTriggerType,
    validate_automation_definition,
)
from codex_web.compatibility import CanonicalEventEnvelope
from codex_web.definitions import DefinitionContext, DefinitionReference, reference_for
from codex_web.scheduler import (
    MisfirePolicy,
    RecurrenceKind,
    ScheduleCreate,
    ScheduleRecurrence,
    ScheduleRecord,
    ScheduleStatus,
)
from codex_web.services.canonical_events import CanonicalEventBus
from codex_web.services.scheduler import SchedulerService
from codex_web.services.definitions import (
    DefinitionConflictError,
    DefinitionKindSchema,
    DefinitionNotFoundError,
    DefinitionRegistryService,
)


class AutomationDefinitionService:
    def __init__(self, registry: DefinitionRegistryService) -> None:
        self.registry = registry

    def resolve(
        self,
        automation_id: str,
        *,
        organization_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
    ) -> tuple[AutomationDefinition, object]:
        record = self.registry.resolve(
            definition_id=automation_id,
            kind=AUTOMATION_KIND,
            context=DefinitionContext(
                organization_id=organization_id,
                workspace_id=workspace_id,
                project_id=project_id,
            ),
        )
        return AutomationDefinition.model_validate(record.payload), reference_for(record)


@dataclass(frozen=True, slots=True)
class AutomationEventMatch:
    automation_id: str
    automation: AutomationDefinition
    definition_ref: DefinitionReference
    event_id: str
    dedupe_key: str


AutomationEventHandler = Callable[
    [AutomationEventMatch],
    Awaitable[None] | None,
]


class AutomationEventTriggerService:
    """Deterministically project canonical events onto enabled Automation definitions."""

    def __init__(
        self,
        registry: DefinitionRegistryService,
        bus: CanonicalEventBus,
    ) -> None:
        self.registry = registry
        self.bus = bus

    @staticmethod
    def _project_id(event: CanonicalEventEnvelope) -> str | None:
        value = event.payload.get("project_id")
        normalized = str(value or "").strip()
        return normalized or None

    @staticmethod
    def _provider_id(event: CanonicalEventEnvelope) -> str | None:
        direct = str(event.payload.get("provider_id") or "").strip()
        if direct:
            return direct
        identity = event.payload.get("identity")
        if isinstance(identity, dict):
            source_type = str(identity.get("source_type") or "").strip()
            source_instance = str(identity.get("source_instance") or "").strip()
            if source_type and source_instance:
                return f"{source_type}:{source_instance}"
        return None

    @staticmethod
    def _filter_matches(
        payload: dict[str, object],
        expected: dict[str, str],
    ) -> bool:
        return all(
            str(payload.get(key, "")) == str(value)
            for key, value in expected.items()
        )

    def matches(
        self,
        event: CanonicalEventEnvelope,
    ) -> tuple[AutomationEventMatch, ...]:
        context = DefinitionContext(
            organization_id=event.tenant_id,
            workspace_id=event.workspace_id,
            project_id=self._project_id(event),
        )
        definition_ids = sorted(
            {
                record.definition_id
                for record in self.registry.list_records(kind=AUTOMATION_KIND)
            }
        )
        matches: list[AutomationEventMatch] = []
        for automation_id in definition_ids:
            try:
                record = self.registry.resolve(
                    definition_id=automation_id,
                    kind=AUTOMATION_KIND,
                    context=context,
                )
            except (DefinitionNotFoundError, DefinitionConflictError):
                continue

            automation = AutomationDefinition.model_validate(record.payload)
            if automation.lifecycle != AutomationLifecycle.ENABLED:
                continue

            trigger = automation.trigger
            if trigger.type == AutomationTriggerType.CANONICAL_EVENT:
                if trigger.event_type != event.event_type:
                    continue
            elif trigger.type == AutomationTriggerType.PROVIDER_EVENT:
                if trigger.event_type != str(
                    event.payload.get("provider_event_type") or ""
                ):
                    continue
                if trigger.provider_id != self._provider_id(event):
                    continue
            else:
                continue

            if not self._filter_matches(event.payload, trigger.event_filter):
                continue

            reference = reference_for(record)
            matches.append(
                AutomationEventMatch(
                    automation_id=automation_id,
                    automation=automation,
                    definition_ref=reference,
                    event_id=event.event_id,
                    dedupe_key=f"{reference.record_id}:{event.event_id}",
                )
            )
        return tuple(matches)

    def install(self, handler: AutomationEventHandler) -> Callable[[], None]:
        async def on_event(event: CanonicalEventEnvelope) -> None:
            for match in self.matches(event):
                outcome = handler(match)
                if inspect.isawaitable(outcome):
                    await outcome

        return self.bus.subscribe(on_event)



class AutomationScheduleMaterializationError(RuntimeError):
    pass


class AutomationScheduleMaterializer:
    """Reconcile schedule-backed Automations onto the canonical durable scheduler."""

    TRIGGER_TYPE = "automation.definition"

    def __init__(
        self,
        automations: AutomationDefinitionService,
        scheduler: SchedulerService,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.automations = automations
        self.scheduler = scheduler
        self.clock = clock

    @staticmethod
    def _automation_schedules(
        schedules: list[ScheduleRecord],
        automation_id: str,
    ) -> list[ScheduleRecord]:
        return [
            schedule
            for schedule in schedules
            if schedule.trigger_type == AutomationScheduleMaterializer.TRIGGER_TYPE
            and schedule.payload.get("automation_id") == automation_id
        ]

    @staticmethod
    def _daily_cron(
        cron: str,
        timezone: str,
        *,
        now: float,
    ) -> tuple[float, ScheduleRecurrence]:
        parts = str(cron or "").split()
        if len(parts) != 5 or parts[2:] != ["*", "*", "*"]:
            raise AutomationScheduleMaterializationError(
                "canonical scheduler currently supports recurring Automation cron "
                "only in fixed daily 'minute hour * * *' form"
            )
        try:
            minute = int(parts[0])
            hour = int(parts[1])
        except ValueError as exc:
            raise AutomationScheduleMaterializationError(
                "daily Automation cron minute/hour must be integers"
            ) from exc
        if not 0 <= minute <= 59 or not 0 <= hour <= 23:
            raise AutomationScheduleMaterializationError(
                "daily Automation cron minute/hour is out of range"
            )
        try:
            zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise AutomationScheduleMaterializationError(
                "Automation timezone must be a valid IANA timezone"
            ) from exc

        local_now = datetime.fromtimestamp(now, zone)
        candidate = local_now.replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
            fold=0,
        )
        if candidate.timestamp() <= now:
            next_date = local_now.date() + timedelta(days=1)
            candidate = datetime(
                next_date.year,
                next_date.month,
                next_date.day,
                hour,
                minute,
                tzinfo=zone,
                fold=0,
            )
        return (
            candidate.timestamp(),
            ScheduleRecurrence(
                kind=RecurrenceKind.DAILY,
                local_time=f"{hour:02d}:{minute:02d}",
                timezone=timezone,
            ),
        )

    def reconcile(
        self,
        automation_id: str,
        *,
        actor_id: str,
        organization_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
    ) -> ScheduleRecord | None:
        automation, reference = self.automations.resolve(
            automation_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            project_id=project_id,
        )
        schedules = self._automation_schedules(
            self.scheduler.list(),
            automation_id,
        )
        current = next(
            (
                schedule
                for schedule in schedules
                if schedule.payload.get("definition_record_id") == reference.record_id
            ),
            None,
        )

        schedule_backed = automation.trigger.type in {
            AutomationTriggerType.ONE_SHOT_SCHEDULE,
            AutomationTriggerType.RECURRING_SCHEDULE,
        }
        if automation.lifecycle != AutomationLifecycle.ENABLED or not schedule_backed:
            for schedule in schedules:
                if schedule.status == ScheduleStatus.ACTIVE:
                    self.scheduler.pause(schedule.id, actor_id=actor_id)
            return None

        for schedule in schedules:
            if (
                schedule.payload.get("definition_record_id") != reference.record_id
                and schedule.status == ScheduleStatus.ACTIVE
            ):
                self.scheduler.pause(schedule.id, actor_id=actor_id)

        if current is not None:
            if current.status == ScheduleStatus.PAUSED:
                current = self.scheduler.resume(current.id, actor_id=actor_id)
            return current

        trigger = automation.trigger
        recurrence = None
        if trigger.type == AutomationTriggerType.ONE_SHOT_SCHEDULE:
            due_at = float(trigger.due_at)
        else:
            due_at, recurrence = self._daily_cron(
                str(trigger.cron),
                str(trigger.timezone),
                now=float(self.clock()),
            )

        return self.scheduler.create(
            ScheduleCreate(
                name=f"Automation: {automation.name}",
                tenant_id=organization_id or "local",
                workspace_id=workspace_id,
                trigger_type=self.TRIGGER_TYPE,
                payload={
                    "automation_id": automation_id,
                    "definition_record_id": reference.record_id,
                    "definition_revision": reference.revision,
                    "definition_checksum": reference.checksum,
                    "project_id": project_id,
                },
                due_at=due_at,
                recurrence=recurrence,
                misfire_policy=MisfirePolicy.FIRE_ONCE,
            ),
            actor_id=actor_id,
        )



def install_automation_definitions(
    registry: DefinitionRegistryService,
) -> AutomationDefinitionService:
    if not any(
        item["kind"] == AUTOMATION_KIND
        and item["schema_version"] == AUTOMATION_SCHEMA_VERSION
        for item in registry.schemas.metadata()
    ):
        registry.register_schema(
            DefinitionKindSchema(
                kind=AUTOMATION_KIND,
                schema_version=AUTOMATION_SCHEMA_VERSION,
                validate=validate_automation_definition,
            )
        )
    return AutomationDefinitionService(registry)
