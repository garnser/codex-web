from __future__ import annotations

from codex_web.definitions import (
    DefinitionContext,
    DefinitionDraftCreate,
    DefinitionReference,
    reference_for,
)
from codex_web.execution_profile_seed import execution_profile_catalog_seed_payload
from codex_web.execution_profiles import (
    EXECUTION_PROFILE_CATALOG_ID,
    EXECUTION_PROFILE_CATALOG_KIND,
    EXECUTION_PROFILE_CATALOG_SCHEMA_VERSION,
    ExecutionProfileCatalogDefinition,
    ExecutionProfileContract,
    validate_execution_profile_catalog,
)
from codex_web.services.definitions import DefinitionKindSchema, DefinitionRegistryService


class ExecutionProfileDefinitionService:
    """Resolve canonical execution profiles exclusively from Definition Registry."""

    def __init__(self, registry: DefinitionRegistryService) -> None:
        self.registry = registry

    def bootstrap(self) -> None:
        self.registry.bootstrap(
            [
                DefinitionDraftCreate(
                    definition_id=EXECUTION_PROFILE_CATALOG_ID,
                    kind=EXECUTION_PROFILE_CATALOG_KIND,
                    definition_schema_version=EXECUTION_PROFILE_CATALOG_SCHEMA_VERSION,
                    payload=execution_profile_catalog_seed_payload(),
                    actor="bootstrap",
                    reason="bootstrap canonical execution profiles",
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
            definition_id=EXECUTION_PROFILE_CATALOG_ID,
            kind=EXECUTION_PROFILE_CATALOG_KIND,
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
    ) -> ExecutionProfileCatalogDefinition:
        return ExecutionProfileCatalogDefinition.model_validate(
            self.record(
                organization_id=organization_id,
                workspace_id=workspace_id,
                project_id=project_id,
            ).payload
        )

    def resolve(
        self,
        profile_id: str | None,
        *,
        organization_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
    ) -> tuple[ExecutionProfileContract, DefinitionReference]:
        record = self.record(
            organization_id=organization_id,
            workspace_id=workspace_id,
            project_id=project_id,
        )
        catalog = ExecutionProfileCatalogDefinition.model_validate(record.payload)
        effective_id = str(profile_id or catalog.default_profile_id).strip()
        profile = catalog.profile_map.get(effective_id)
        if profile is None:
            raise ValueError(f"unknown execution profile: {effective_id}")
        return profile, reference_for(record)

    def public(
        self,
        *,
        organization_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, object]:
        record = self.record(
            organization_id=organization_id,
            workspace_id=workspace_id,
            project_id=project_id,
        )
        catalog = ExecutionProfileCatalogDefinition.model_validate(record.payload)
        return {
            "definition": reference_for(record).model_dump(mode="json"),
            "default_profile_id": catalog.default_profile_id,
            "items": [profile.public() for profile in catalog.profiles],
        }


def install_execution_profile_definitions(
    registry: DefinitionRegistryService,
) -> ExecutionProfileDefinitionService:
    if not any(
        item["kind"] == EXECUTION_PROFILE_CATALOG_KIND
        and item["schema_version"] == EXECUTION_PROFILE_CATALOG_SCHEMA_VERSION
        for item in registry.schemas.metadata()
    ):
        registry.register_schema(
            DefinitionKindSchema(
                kind=EXECUTION_PROFILE_CATALOG_KIND,
                schema_version=EXECUTION_PROFILE_CATALOG_SCHEMA_VERSION,
                validate=validate_execution_profile_catalog,
            )
        )
    service = ExecutionProfileDefinitionService(registry)
    service.bootstrap()
    return service
