from __future__ import annotations

from codex_web.definitions import (
    DefinitionContext,
    DefinitionDraftCreate,
    DefinitionReference,
    reference_for,
)
from codex_web.execution_profile_models import (
    EXECUTION_PROFILE_CATALOG_ID,
    EXECUTION_PROFILE_CATALOG_KIND,
    EXECUTION_PROFILE_CATALOG_SCHEMA_VERSION,
    ExecutionProfileCatalogDefinition,
    ExecutionProfileContract,
    validate_execution_profile_catalog,
)
from codex_web.execution_profile_seed import execution_profile_catalog_seed_payload
from codex_web.execution_workers import ExecutionProfileBinding
from codex_web.services.definitions import (
    DefinitionKindSchema,
    DefinitionRegistryService,
)


class ExecutionProfileDefinitionService:
    """Resolve execution authority profiles exclusively from Definition Registry."""

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
                    reason="install canonical execution profile catalog",
                )
            ]
        )

    def _record(
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
            self._record(
                organization_id=organization_id,
                workspace_id=workspace_id,
                project_id=project_id,
            ).payload
        )

    def reference(
        self,
        *,
        organization_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
    ) -> DefinitionReference:
        return reference_for(
            self._record(
                organization_id=organization_id,
                workspace_id=workspace_id,
                project_id=project_id,
            )
        )

    def profile(
        self,
        profile_id: str | None = None,
        *,
        execution_role_id: str | None = None,
        organization_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
    ) -> ExecutionProfileContract:
        catalog = self.catalog(
            organization_id=organization_id,
            workspace_id=workspace_id,
            project_id=project_id,
        )
        selected = profile_id
        if selected is None and execution_role_id:
            selected = catalog.role_to_profile.get(execution_role_id)
        selected = selected or catalog.default_profile_id
        profile = catalog.profile_map.get(selected)
        if profile is None or profile.lifecycle == "disabled":
            raise ValueError(f"execution profile unavailable: {selected}")
        return profile

    def binding(
        self,
        profile_id: str | None = None,
        *,
        execution_role_id: str | None = None,
        organization_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
    ) -> ExecutionProfileBinding:
        record = self._record(
            organization_id=organization_id,
            workspace_id=workspace_id,
            project_id=project_id,
        )
        catalog = ExecutionProfileCatalogDefinition.model_validate(record.payload)
        selected = profile_id
        if selected is None and execution_role_id:
            selected = catalog.role_to_profile.get(execution_role_id)
        selected = selected or catalog.default_profile_id
        profile = catalog.profile_map.get(selected)
        if profile is None or profile.lifecycle == "disabled":
            raise ValueError(f"execution profile unavailable: {selected}")
        return ExecutionProfileBinding(
            profile_id=profile.id,
            definition=reference_for(record),
            workspace_mode=profile.workspace_mode,
            repository_access=profile.repository_access,
            required_worker_capabilities=profile.required_worker_capabilities,
            allowed_sandboxes=profile.allowed_sandboxes,
            allowed_control_plane_operations=profile.allowed_control_plane_operations,
            authority_explanation=profile.authority_explanation,
        )


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
