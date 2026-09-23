from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Awaitable, Callable

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
from codex_web.services.canonical_events import CanonicalEventBus
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
