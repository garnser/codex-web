from __future__ import annotations

from codex_web.automation_definitions import (
    AUTOMATION_KIND,
    AUTOMATION_SCHEMA_VERSION,
    AutomationDefinition,
    validate_automation_definition,
)
from codex_web.definitions import DefinitionContext, reference_for
from codex_web.services.definitions import DefinitionKindSchema, DefinitionRegistryService


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
