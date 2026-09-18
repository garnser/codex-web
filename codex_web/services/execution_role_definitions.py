from __future__ import annotations

from codex_web.definitions import DefinitionContext, DefinitionDraftCreate, DefinitionReference, reference_for
from codex_web.execution_contract_seed import execution_role_catalog_seed_payload
from codex_web.execution_contracts import install_execution_role_catalog_provider
from codex_web.execution_role_models import (
    EXECUTION_ROLE_CATALOG_ID,
    EXECUTION_ROLE_CATALOG_KIND,
    EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
    ExecutionRoleCatalogDefinition,
    validate_execution_role_catalog,
)
from codex_web.services.definitions import (
    DefinitionKindSchema,
    DefinitionRegistryService,
)


class ExecutionRoleDefinitionService:
    """Resolve the execution-role catalog exclusively from Definition Registry."""

    def __init__(self, registry: DefinitionRegistryService) -> None:
        self.registry = registry

    def bootstrap(self) -> None:
        self.registry.bootstrap(
            [
                DefinitionDraftCreate(
                    definition_id=EXECUTION_ROLE_CATALOG_ID,
                    kind=EXECUTION_ROLE_CATALOG_KIND,
                    definition_schema_version=EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
                    payload=execution_role_catalog_seed_payload(),
                    actor="bootstrap",
                    reason="migrate hard-coded execution role catalog into Definition Registry",
                )
            ]
        )

    def catalog(
        self,
        *,
        organization_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
    ) -> ExecutionRoleCatalogDefinition:
        record = self.registry.resolve(
            definition_id=EXECUTION_ROLE_CATALOG_ID,
            kind=EXECUTION_ROLE_CATALOG_KIND,
            context=DefinitionContext(
                organization_id=organization_id,
                workspace_id=workspace_id,
                project_id=project_id,
            ),
        )
        return ExecutionRoleCatalogDefinition.model_validate(record.payload)

    def reference(
        self,
        *,
        organization_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
    ) -> DefinitionReference:
        record = self.registry.resolve(
            definition_id=EXECUTION_ROLE_CATALOG_ID,
            kind=EXECUTION_ROLE_CATALOG_KIND,
            context=DefinitionContext(
                organization_id=organization_id,
                workspace_id=workspace_id,
                project_id=project_id,
            ),
        )
        return reference_for(record)


def install_execution_role_definitions(
    registry: DefinitionRegistryService,
) -> ExecutionRoleDefinitionService:
    if not any(
        item["kind"] == EXECUTION_ROLE_CATALOG_KIND
        and item["schema_version"] == EXECUTION_ROLE_CATALOG_SCHEMA_VERSION
        for item in registry.schemas.metadata()
    ):
        registry.register_schema(
            DefinitionKindSchema(
                kind=EXECUTION_ROLE_CATALOG_KIND,
                schema_version=EXECUTION_ROLE_CATALOG_SCHEMA_VERSION,
                validate=validate_execution_role_catalog,
            )
        )
    service = ExecutionRoleDefinitionService(registry)
    service.bootstrap()
    install_execution_role_catalog_provider(service.catalog)
    return service
