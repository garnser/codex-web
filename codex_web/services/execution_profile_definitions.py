from __future__ import annotations

import time

from codex_web.definitions import (
    DefinitionContext,
    DefinitionDraftCreate,
    DefinitionReference,
    DefinitionScope,
    reference_for,
)
from codex_web.execution_profile_seed import execution_profile_catalog_seed_payload
from codex_web.execution_profiles import (
    ADMINISTRATIVE_EXECUTION_PROFILE_SCHEMA_VERSION,
    COORDINATION_EXECUTION_PROFILE_SCHEMA_VERSION,
    HOME_OPERATOR_EXECUTION_PROFILE_SCHEMA_VERSION,
    EXECUTION_PROFILE_CATALOG_ID,
    EXECUTION_PROFILE_CATALOG_KIND,
    EXECUTION_PROFILE_CATALOG_SCHEMA_VERSION,
    ExecutionProfileCatalogDefinition,
    ExecutionProfileContract,
    validate_execution_profile_catalog,
    validate_administrative_execution_profile_catalog,
    validate_coordination_execution_profile_catalog,
    validate_home_execution_profile_catalog,
)
from codex_web.services.definitions import DefinitionKindSchema, DefinitionRegistryService


class ExecutionProfileDefinitionService:
    """Resolve canonical execution profiles exclusively from Definition Registry."""

    def __init__(self, registry: DefinitionRegistryService) -> None:
        self.registry = registry
        self.usage_loader = None
        self.agent_profiles = None

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
            # Revalidate operational declarations across process publications
            # and effective-time boundaries through the uncached lookup.
            now=time.time(),
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
        if profile.administrative_repository_ids and (
            record.scope_type != DefinitionScope.PROJECT
            or not project_id or record.scope_id != project_id
            or record.definition_schema_version not in {ADMINISTRATIVE_EXECUTION_PROFILE_SCHEMA_VERSION,
                                                       COORDINATION_EXECUTION_PROFILE_SCHEMA_VERSION}
        ):
            raise ValueError("administrative execution targets require an exact Project-scoped schema 1.1 definition")
        if profile.project_coordination and (
            record.scope_type != DefinitionScope.PROJECT or record.scope_id != project_id
            or record.definition_schema_version != COORDINATION_EXECUTION_PROFILE_SCHEMA_VERSION
        ):
            raise ValueError("project coordination requires an exact Project-scoped schema 1.2 definition")
        if profile.home_operator and (
            project_id != "home" or record.scope_type != DefinitionScope.PROJECT
            or record.scope_id != "home"
            or record.definition_schema_version != HOME_OPERATOR_EXECUTION_PROFILE_SCHEMA_VERSION
        ):
            raise ValueError("Home operator requires an exact Home-scoped schema 1.3 definition")
        return profile, reference_for(record)

    def authorize_administrative_target(
        self, assignment, *, operation_id: str, repository_id: str,
        actor, authority, now: float | None = None,
    ) -> ExecutionProfileContract:
        """Validate an explicitly published administrative envelope, never Git access.

        Authority for the operation is still evaluated by the broker/ActionIntent.
        A pinned declaration must also remain effective; superseding/quarantining
        its definition revokes the old assignment's administrative envelope.
        """
        observed = time.time() if now is None else float(now)
        if (assignment.organization_id != actor.organization_id
                or assignment.workspace_id != actor.workspace_id
                or not assignment.project_id
                or assignment.status not in {"claimed", "running"}
                or assignment.lease is None
                or assignment.lease.expires_at <= observed
                or assignment.lease.fence != assignment.fence
                or assignment.lease.worker_id != assignment.assigned_worker_id
                or (assignment.deadline_at is not None and assignment.deadline_at <= observed)):
            raise ValueError("administrative operation requires a matching live fenced assignment")
        binding = assignment.agent_profile
        if (binding is None or not binding.authority_role_id
                or binding.execution_profile_id != assignment.execution_profile_id
                or binding.authority_role_id not in authority.role_ids_for_actor(
                    actor, project_id=assignment.project_id, now=observed)):
            raise ValueError("administrative operation requires an explicit live authority Role binding")
        if self.agent_profiles is None:
            raise ValueError("administrative Agent Profile validation is unavailable")
        try:
            current = self.agent_profiles.get(binding.profile_id, actor=actor)
        except Exception as exc:
            raise ValueError("administrative Agent Profile is unavailable") from exc
        if (current.lifecycle != "active" or current.revision != binding.profile_revision
                or current.record_id != binding.profile_record_id
                or current.authority_role_id != binding.authority_role_id
                or current.execution_profile_id != binding.execution_profile_id
                or current.role_id != binding.role_id):
            raise ValueError("administrative Agent Profile is disabled or its binding changed")
        profile, reference = self.resolve(
            assignment.execution_profile_id,
            organization_id=assignment.organization_id,
            workspace_id=assignment.workspace_id,
            project_id=assignment.project_id,
        )
        if reference != assignment.execution_profile_definition:
            raise ValueError("administrative execution definition is no longer effective")
        if (not profile.administrative_repository_ids
                or repository_id not in profile.administrative_repository_ids
                or operation_id not in profile.control_plane_operations):
            raise ValueError("administrative operation or repository target is not explicitly declared")
        # Scratch administration has no Git workspace or executable publication.
        administrative_operation = (
            operation_id.startswith(("procedure.", "executive.activation."))
            or operation_id.startswith("repository.read.")
            or operation_id.startswith("work_item.")
            or operation_id in {
                "repository.issue.create", "repository.issue.comment", "repository.issue.update",
                "repository.pull-request.upsert", "repository.pull-request.merge", "repository.pull-request.ready",
                "repository.check.rerun", "action_intent.reconcile",
                "project.read", "resource.read", "execution.read", "evidence.read",
                "control_plane.operations.list", "work_graph.edge.create",
            }
        )
        if not administrative_operation:
            raise ValueError("operation requires a separate scoped execution capability")
        return profile

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
    if not any(item["kind"] == EXECUTION_PROFILE_CATALOG_KIND
               and item["schema_version"] == ADMINISTRATIVE_EXECUTION_PROFILE_SCHEMA_VERSION
               for item in registry.schemas.metadata()):
        registry.register_schema(DefinitionKindSchema(
            kind=EXECUTION_PROFILE_CATALOG_KIND,
            schema_version=ADMINISTRATIVE_EXECUTION_PROFILE_SCHEMA_VERSION,
            validate=validate_administrative_execution_profile_catalog,
        ))
    if not any(item["kind"] == EXECUTION_PROFILE_CATALOG_KIND
               and item["schema_version"] == COORDINATION_EXECUTION_PROFILE_SCHEMA_VERSION
               for item in registry.schemas.metadata()):
        registry.register_schema(DefinitionKindSchema(
            kind=EXECUTION_PROFILE_CATALOG_KIND,
            schema_version=COORDINATION_EXECUTION_PROFILE_SCHEMA_VERSION,
            validate=validate_coordination_execution_profile_catalog,
        ))
    if not any(item["kind"] == EXECUTION_PROFILE_CATALOG_KIND
               and item["schema_version"] == HOME_OPERATOR_EXECUTION_PROFILE_SCHEMA_VERSION
               for item in registry.schemas.metadata()):
        registry.register_schema(DefinitionKindSchema(
            kind=EXECUTION_PROFILE_CATALOG_KIND,
            schema_version=HOME_OPERATOR_EXECUTION_PROFILE_SCHEMA_VERSION,
            validate=validate_home_execution_profile_catalog,
        ))
    service = ExecutionProfileDefinitionService(registry)
    service.bootstrap()
    return service
