from __future__ import annotations

from typing import Any

from codex_web.authority import (
    AUTHORITY_ROLE_CATALOG_ID,
    AUTHORITY_ROLE_CATALOG_KIND,
    AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
    AuthorityRoleBinding,
    AuthorityRoleCatalogDefinition,
)
from codex_web.definitions import (
    DefinitionDraftCreate,
    DefinitionLifecycle,
    DefinitionPublishRequest,
    DefinitionScope,
)
from codex_web.identity import AuthenticationActor
from codex_web.services.authority_roles import AuthorityRoleService
from codex_web.services.definitions import DefinitionRegistryService


class AuthorityAccessManagementError(RuntimeError):
    pass


class AuthorityAccessManagementService:
    """Versioned direct Role-binding changes over the canonical authority catalog."""

    def __init__(
        self,
        authority: AuthorityRoleService,
        registry: DefinitionRegistryService,
    ) -> None:
        self.authority = authority
        self.registry = registry

    @staticmethod
    def _binding_visible_to_scope(
        binding: AuthorityRoleBinding,
        actor: AuthenticationActor,
        project_id: str | None,
    ) -> bool:
        if binding.organization_id not in {None, actor.organization_id}:
            return False
        if binding.workspace_id not in {None, actor.workspace_id}:
            return False
        if project_id is not None and binding.project_ids:
            return project_id in binding.project_ids
        return True

    @staticmethod
    def _delegation_visible_to_scope(
        delegation,
        actor: AuthenticationActor,
        project_id: str | None,
    ) -> bool:
        if delegation.organization_id not in {None, actor.organization_id}:
            return False
        if delegation.workspace_id not in {None, actor.workspace_id}:
            return False
        if project_id is not None and delegation.project_ids:
            return project_id in delegation.project_ids
        return True

    @staticmethod
    def _target_scope(
        actor: AuthenticationActor,
        project_id: str | None,
    ) -> tuple[DefinitionScope, str]:
        if project_id:
            return DefinitionScope.PROJECT, project_id
        return DefinitionScope.WORKSPACE, actor.workspace_id

    def _published_target_record(
        self,
        *,
        scope_type: DefinitionScope,
        scope_id: str,
    ):
        published = [
            item
            for item in self.registry.list_records(
                kind=AUTHORITY_ROLE_CATALOG_KIND,
                definition_id=AUTHORITY_ROLE_CATALOG_ID,
                scope_type=scope_type,
                scope_id=scope_id,
            )
            if item.lifecycle == DefinitionLifecycle.PUBLISHED
        ]
        if len(published) > 1:
            raise AuthorityAccessManagementError(
                "multiple published authority catalogs exist for target scope"
            )
        return published[0] if published else None

    def _base_catalog(
        self,
        *,
        actor: AuthenticationActor,
        project_id: str | None,
        target_record,
    ) -> AuthorityRoleCatalogDefinition:
        source = target_record or self.authority.catalog_record(
            actor=actor,
            project_id=project_id,
        )
        catalog = AuthorityRoleCatalogDefinition.model_validate(source.payload)
        if target_record is not None:
            return catalog
        return catalog.model_copy(
            update={
                "bindings": tuple(
                    item
                    for item in catalog.bindings
                    if self._binding_visible_to_scope(item, actor, project_id)
                ),
                "delegations": tuple(
                    item
                    for item in catalog.delegations
                    if self._delegation_visible_to_scope(item, actor, project_id)
                ),
            }
        )

    def _stage(
        self,
        *,
        actor: AuthenticationActor,
        project_id: str | None,
        catalog: AuthorityRoleCatalogDefinition,
        reason: str,
    ) -> dict[str, Any]:
        scope_type, scope_id = self._target_scope(actor, project_id)
        active = self._published_target_record(
            scope_type=scope_type,
            scope_id=scope_id,
        )
        draft = self.registry.create_draft(
            DefinitionDraftCreate(
                definition_id=AUTHORITY_ROLE_CATALOG_ID,
                kind=AUTHORITY_ROLE_CATALOG_KIND,
                definition_schema_version=AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
                scope_type=scope_type,
                scope_id=scope_id,
                payload=catalog.model_dump(mode="json"),
                actor=actor.identity_id,
                reason=reason,
                derived_from_record_id=active.record_id if active is not None else None,
            )
        )
        validated = self.registry.validate(
            draft.record_id,
            actor=actor.identity_id,
        )
        assessment = self.registry.publication_assessment(validated.record_id)
        if assessment.requires_independent_approval:
            return {
                "status": "pending_approval",
                "record": validated,
                "assessment": assessment,
            }

        published = self.registry.publish(
            validated.record_id,
            DefinitionPublishRequest(
                actor=actor.identity_id,
                reason=reason,
                expected_active_revision=active.revision if active is not None else None,
            ),
        )
        return {
            "status": "published",
            "record": published,
            "assessment": assessment,
        }

    def add_direct_binding(
        self,
        *,
        actor: AuthenticationActor,
        identity_id: str,
        role_id: str,
        project_id: str | None = None,
        reason: str,
    ) -> dict[str, Any]:
        scope_type, scope_id = self._target_scope(actor, project_id)
        active = self._published_target_record(
            scope_type=scope_type,
            scope_id=scope_id,
        )
        catalog = self._base_catalog(
            actor=actor,
            project_id=project_id,
            target_record=active,
        )
        roles = {item.id: item for item in catalog.roles}
        role = roles.get(role_id)
        if role is None or role.lifecycle != "active":
            raise AuthorityAccessManagementError(
                "direct assignment requires an active canonical operational Role"
            )

        project_ids = (project_id,) if project_id else ()
        duplicate = next(
            (
                item
                for item in catalog.bindings
                if item.role_id == role_id
                and item.subject_kind == "identity"
                and item.subject_id == identity_id
                and item.organization_id == actor.organization_id
                and item.workspace_id == actor.workspace_id
                and item.project_ids == project_ids
            ),
            None,
        )
        if duplicate is not None:
            return {
                "status": "already_effective",
                "binding": duplicate,
                "record": active
                or self.authority.catalog_record(
                    actor=actor,
                    project_id=project_id,
                ),
                "assessment": None,
            }

        binding = AuthorityRoleBinding(
            role_id=role_id,
            subject_kind="identity",
            subject_id=identity_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            project_ids=project_ids,
        )
        updated = catalog.model_copy(
            update={"bindings": (*catalog.bindings, binding)}
        )
        result = self._stage(
            actor=actor,
            project_id=project_id,
            catalog=updated,
            reason=reason,
        )
        return {**result, "binding": binding}

    def remove_direct_binding(
        self,
        *,
        actor: AuthenticationActor,
        binding_id: str,
        project_id: str | None = None,
        reason: str,
    ) -> dict[str, Any]:
        scope_type, scope_id = self._target_scope(actor, project_id)
        active = self._published_target_record(
            scope_type=scope_type,
            scope_id=scope_id,
        )
        catalog = self._base_catalog(
            actor=actor,
            project_id=project_id,
            target_record=active,
        )
        binding = next(
            (
                item
                for item in catalog.bindings
                if item.id == binding_id
                and item.subject_kind == "identity"
                and self._binding_visible_to_scope(item, actor, project_id)
            ),
            None,
        )
        if binding is None:
            raise AuthorityAccessManagementError(
                "direct authority binding not found in requested scope"
            )

        updated = catalog.model_copy(
            update={
                "bindings": tuple(
                    item for item in catalog.bindings if item.id != binding_id
                )
            }
        )
        result = self._stage(
            actor=actor,
            project_id=project_id,
            catalog=updated,
            reason=reason,
        )
        return {**result, "binding": binding}
