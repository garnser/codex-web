from __future__ import annotations

from codex_web.definitions import (
    DefinitionContext,
    DefinitionDraftCreate,
    reference_for,
)
from codex_web.executive_role_seed import executive_role_catalog_seed_payload
from codex_web.executive_roles import (
    EXECUTIVE_ROLE_CATALOG_ID,
    EXECUTIVE_ROLE_CATALOG_KIND,
    EXECUTIVE_ROLE_CATALOG_SCHEMA_VERSION,
    ExecutiveRoleCatalogDefinition,
    validate_executive_role_catalog,
)
from codex_web.services.definitions import (
    DefinitionKindSchema,
    DefinitionRegistryService,
)


class ExecutiveRoleDefinitionService:
    """Resolve mutable Executive role policy exclusively from Definition Registry."""

    def __init__(self, registry: DefinitionRegistryService) -> None:
        self.registry = registry

    def bootstrap(self) -> None:
        self.registry.bootstrap(
            [
                DefinitionDraftCreate(
                    definition_id=EXECUTIVE_ROLE_CATALOG_ID,
                    kind=EXECUTIVE_ROLE_CATALOG_KIND,
                    definition_schema_version=EXECUTIVE_ROLE_CATALOG_SCHEMA_VERSION,
                    payload=executive_role_catalog_seed_payload(),
                    actor="bootstrap",
                    reason="bootstrap canonical Executive role policy",
                )
            ]
        )

    def record(
        self,
        *,
        organization_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
    ):
        return self.registry.resolve(
            definition_id=EXECUTIVE_ROLE_CATALOG_ID,
            kind=EXECUTIVE_ROLE_CATALOG_KIND,
            context=DefinitionContext(
                organization_id=organization_id,
                workspace_id=workspace_id,
                project_id=project_id,
            ),
        )

    def catalog(
        self,
        *,
        organization_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
    ) -> ExecutiveRoleCatalogDefinition:
        return ExecutiveRoleCatalogDefinition.model_validate(
            self.record(
                organization_id=organization_id,
                workspace_id=workspace_id,
                project_id=project_id,
            ).payload
        )

    def resolve(
        self,
        *,
        organization_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
    ):
        record = self.record(
            organization_id=organization_id,
            workspace_id=workspace_id,
            project_id=project_id,
        )
        return (
            ExecutiveRoleCatalogDefinition.model_validate(record.payload),
            reference_for(record),
        )

    def reference(
        self,
        *,
        organization_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
    ):
        return self.resolve(
            organization_id=organization_id,
            workspace_id=workspace_id,
            project_id=project_id,
        )[1]


def install_executive_role_definitions(
    registry: DefinitionRegistryService,
) -> ExecutiveRoleDefinitionService:
    if not any(
        item["kind"] == EXECUTIVE_ROLE_CATALOG_KIND
        and item["schema_version"] == EXECUTIVE_ROLE_CATALOG_SCHEMA_VERSION
        for item in registry.schemas.metadata()
    ):
        registry.register_schema(
            DefinitionKindSchema(
                kind=EXECUTIVE_ROLE_CATALOG_KIND,
                schema_version=EXECUTIVE_ROLE_CATALOG_SCHEMA_VERSION,
                validate=validate_executive_role_catalog,
            )
        )
    service = ExecutiveRoleDefinitionService(registry)
    service.bootstrap()
    return service
