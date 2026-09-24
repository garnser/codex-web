from __future__ import annotations

import time
from typing import Any

from codex_web.authority import (
    AUTHORITY_ROLE_CATALOG_ID,
    AUTHORITY_ROLE_CATALOG_KIND,
    AuthorityRoleCatalogDefinition,
)
from codex_web.definitions import DefinitionLifecycle, reference_for
from codex_web.identity import AuthenticationActor
from codex_web.services.authority_roles import AuthorityRoleService
from codex_web.services.definitions import DefinitionConflictError, DefinitionRegistryService


class AuthorityPolicyExplorerService:
    """Read-only policy provenance, impact and exact-revision simulation support."""

    def __init__(
        self,
        authority: AuthorityRoleService,
        registry: DefinitionRegistryService,
    ) -> None:
        self.authority = authority
        self.registry = registry

    @staticmethod
    def _definition_summary(record) -> dict[str, Any]:
        return {
            "reference": reference_for(record).model_dump(mode="json"),
            "scope_type": record.scope_type.value,
            "scope_id": record.scope_id,
            "lifecycle": record.lifecycle.value,
            "effective_from": record.effective_from,
            "effective_until": record.effective_until,
        }

    @staticmethod
    def _role_map(catalog: AuthorityRoleCatalogDefinition):
        return {item.id: item for item in catalog.roles}

    def _grant_rows(
        self,
        catalog: AuthorityRoleCatalogDefinition,
        *,
        root_role_id: str,
        source_type: str,
        source_id: str,
        delegation_expires_at: float | None = None,
    ) -> list[dict[str, Any]]:
        roles = self._role_map(catalog)
        rows: list[dict[str, Any]] = []

        def walk(role_id: str, path: tuple[str, ...], seen: set[str]) -> None:
            if role_id in seen:
                raise ValueError("authority role inheritance cycle detected at runtime")
            role = roles.get(role_id)
            if role is None:
                raise ValueError(f"authority role not found: {role_id}")
            current_path = (*path, role_id)
            if role.lifecycle == "disabled":
                return
            for grant in role.grants:
                rows.append(
                    {
                        "root_role_id": root_role_id,
                        "role_id": role.id,
                        "role_name": role.name,
                        "role_lifecycle": role.lifecycle,
                        "inheritance_path": list(current_path),
                        "source_type": source_type,
                        "source_id": source_id,
                        "delegation_expires_at": delegation_expires_at,
                        "grant": grant.model_dump(mode="json"),
                    }
                )
            for inherited in sorted(role.inherits):
                walk(inherited, current_path, {*seen, role_id})

        walk(root_role_id, (), set())
        return rows

    def effective(
        self,
        *,
        actor: AuthenticationActor,
        project_id: str | None = None,
        role_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        evaluated_at = time.time() if now is None else now
        record = self.authority.catalog_record(
            actor=actor,
            project_id=project_id,
            now=evaluated_at,
        )
        catalog = AuthorityRoleCatalogDefinition.model_validate(record.payload)
        roles = self._role_map(catalog)

        assignments: list[dict[str, Any]] = []
        grants: list[dict[str, Any]] = []
        for binding in catalog.bindings:
            if not self.authority._binding_matches(binding, actor, project_id):
                continue
            role = roles[binding.role_id]
            assignments.append(
                {
                    "source_type": "binding",
                    "source_id": binding.id,
                    "role_id": binding.role_id,
                    "role_lifecycle": role.lifecycle,
                    "subject_kind": binding.subject_kind,
                    "subject_id": binding.subject_id,
                    "project_ids": list(binding.project_ids),
                }
            )
            grants.extend(
                self._grant_rows(
                    catalog,
                    root_role_id=binding.role_id,
                    source_type="binding",
                    source_id=binding.id,
                )
            )

        for delegation in catalog.delegations:
            if not self.authority._delegation_matches(
                delegation,
                actor,
                project_id,
                evaluated_at,
            ):
                continue
            role = roles[delegation.role_id]
            assignments.append(
                {
                    "source_type": "delegation",
                    "source_id": delegation.id,
                    "role_id": delegation.role_id,
                    "role_lifecycle": role.lifecycle,
                    "subject_kind": "identity",
                    "subject_id": delegation.delegate_identity_id,
                    "project_ids": list(delegation.project_ids),
                    "expires_at": delegation.expires_at,
                    "delegated_by_identity_id": delegation.delegated_by_identity_id,
                }
            )
            grants.extend(
                self._grant_rows(
                    catalog,
                    root_role_id=delegation.role_id,
                    source_type="delegation",
                    source_id=delegation.id,
                    delegation_expires_at=delegation.expires_at,
                )
            )

        role_view = None
        if role_id:
            selected = roles.get(role_id)
            if selected is None:
                raise LookupError(f"authority role not found: {role_id}")
            role_view = {
                "role": selected.model_dump(mode="json"),
                "effective_grants": self._grant_rows(
                    catalog,
                    root_role_id=role_id,
                    source_type="role",
                    source_id=role_id,
                ),
            }

        unique: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for row in grants:
            grant = row["grant"]
            key = (
                str(row["source_type"]),
                str(row["source_id"]),
                str(row["role_id"]),
                str(grant["id"]),
            )
            unique[key] = row

        permission_matrix = [
            {
                "capability": row["grant"]["capability"],
                "level": row["grant"]["level"],
                "role_id": row["role_id"],
                "grant_id": row["grant"]["id"],
                "source_type": row["source_type"],
                "source_id": row["source_id"],
                "inheritance_path": row["inheritance_path"],
                "project_ids": row["grant"]["project_ids"],
                "resource_ids": row["grant"]["resource_ids"],
                "resource_types": row["grant"]["resource_types"],
                "resource_risks": row["grant"]["resource_risks"],
                "resource_sensitivities": row["grant"]["resource_sensitivities"],
                "environments": row["grant"]["environments"],
                "max_amount_usd": row["grant"]["max_amount_usd"],
                "max_input_tokens": row["grant"]["max_input_tokens"],
                "max_output_tokens": row["grant"]["max_output_tokens"],
                "max_model_calls": row["grant"]["max_model_calls"],
                "max_autonomous_risk": row["grant"]["max_autonomous_risk"],
                "approvals": row["grant"]["approvals"],
            }
            for row in sorted(
                unique.values(),
                key=lambda item: (
                    item["grant"]["capability"],
                    item["role_id"],
                    item["grant"]["id"],
                    item["source_id"],
                ),
            )
        ]
        return {
            "actor": {
                "identity_id": actor.identity_id,
                "principal_kind": actor.principal_kind.value,
                "organization_id": actor.organization_id,
                "workspace_id": actor.workspace_id,
                "team_ids": list(actor.team_ids),
            },
            "project_id": project_id,
            "definition": self._definition_summary(record),
            "assignments": assignments,
            "permission_matrix": permission_matrix,
            "role_view": role_view,
            "evaluated_at": evaluated_at,
        }

    @staticmethod
    def _resource_value(value: Any) -> str:
        return str(getattr(value, "value", value))

    @classmethod
    def _permission_applies_to_resource(
        cls,
        row: dict[str, Any],
        resource: Any,
    ) -> bool:
        grant_resource_ids = set(row.get("resource_ids") or ())
        if grant_resource_ids and resource.id not in grant_resource_ids:
            return False

        grant_resource_types = {
            cls._resource_value(value)
            for value in (row.get("resource_types") or ())
        }
        if grant_resource_types and cls._resource_value(resource.resource_type) not in grant_resource_types:
            return False

        grant_resource_risks = {
            cls._resource_value(value)
            for value in (row.get("resource_risks") or ())
        }
        if grant_resource_risks and cls._resource_value(resource.risk) not in grant_resource_risks:
            return False

        grant_resource_sensitivities = {
            cls._resource_value(value)
            for value in (row.get("resource_sensitivities") or ())
        }
        if (
            grant_resource_sensitivities
            and cls._resource_value(resource.sensitivity)
            not in grant_resource_sensitivities
        ):
            return False
        return True

    def effective_for_resource(
        self,
        *,
        actor: AuthenticationActor,
        resource: Any,
        project_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Project the canonical effective authority relevant to one Resource.

        This is an explanatory read model, not an authorization decision for a
        specific action. Capability/level, environment, budget and approval
        requirements remain visible on the returned grants and are still
        evaluated by AuthorityRoleService when an action is attempted.
        """
        result = self.effective(
            actor=actor,
            project_id=project_id,
            now=now,
        )
        rows = [
            row
            for row in result["permission_matrix"]
            if self._permission_applies_to_resource(row, resource)
        ]
        source_ids = {
            str(row["source_id"])
            for row in rows
            if row.get("source_id")
        }
        return {
            **result,
            "resource": {
                "id": resource.id,
                "name": resource.name,
                "resource_type": self._resource_value(resource.resource_type),
            },
            "assignments": [
                item
                for item in result["assignments"]
                if str(item.get("source_id") or "") in source_ids
            ],
            "permission_matrix": rows,
        }

    @staticmethod
    def _changed_ids(old_items, new_items) -> set[str]:
        old = {item.id: item.model_dump(mode="json") for item in old_items}
        new = {item.id: item.model_dump(mode="json") for item in new_items}
        return {
            item_id
            for item_id in set(old) | set(new)
            if old.get(item_id) != new.get(item_id)
        }

    def impact(self, record_id: str) -> dict[str, Any]:
        candidate = self.registry.get_record(record_id)
        if (
            candidate.kind != AUTHORITY_ROLE_CATALOG_KIND
            or candidate.definition_id != AUTHORITY_ROLE_CATALOG_ID
        ):
            raise ValueError("impact preview requires the canonical authority Role catalog")
        if candidate.lifecycle not in {
            DefinitionLifecycle.DRAFT,
            DefinitionLifecycle.VALIDATED,
        }:
            raise DefinitionConflictError(
                "impact preview requires a draft or validated authority definition"
            )

        same_slot = [
            item
            for item in self.registry.list_records(
                kind=candidate.kind,
                definition_id=candidate.definition_id,
            )
            if item.scope_type == candidate.scope_type
            and item.scope_id == candidate.scope_id
            and item.lifecycle == DefinitionLifecycle.PUBLISHED
        ]
        if len(same_slot) > 1:
            raise DefinitionConflictError(
                "multiple active authority definitions exist for one canonical slot"
            )
        active = same_slot[0] if same_slot else None
        assessment = self.registry.publication_assessment(candidate.record_id)
        new_catalog = AuthorityRoleCatalogDefinition.model_validate(candidate.payload)
        old_catalog = (
            AuthorityRoleCatalogDefinition.model_validate(active.payload)
            if active is not None
            else AuthorityRoleCatalogDefinition(roles=())
        )

        changed_roles = self._changed_ids(old_catalog.roles, new_catalog.roles)
        changed_bindings = self._changed_ids(old_catalog.bindings, new_catalog.bindings)
        changed_delegations = self._changed_ids(
            old_catalog.delegations,
            new_catalog.delegations,
        )

        identities: set[str] = set()
        teams: set[str] = set()
        projects: set[str] = set()
        all_projects = False

        def collect(catalog: AuthorityRoleCatalogDefinition) -> None:
            nonlocal all_projects
            for role in catalog.roles:
                if role.id not in changed_roles:
                    continue
                for grant in role.grants:
                    if grant.project_ids:
                        projects.update(grant.project_ids)
                    else:
                        all_projects = True
            for binding in catalog.bindings:
                if binding.id not in changed_bindings and binding.role_id not in changed_roles:
                    continue
                if binding.subject_kind == "identity":
                    identities.add(binding.subject_id)
                else:
                    teams.add(binding.subject_id)
                if binding.project_ids:
                    projects.update(binding.project_ids)
                else:
                    all_projects = True
            for delegation in catalog.delegations:
                if (
                    delegation.id not in changed_delegations
                    and delegation.role_id not in changed_roles
                ):
                    continue
                identities.add(delegation.delegate_identity_id)
                if delegation.project_ids:
                    projects.update(delegation.project_ids)
                else:
                    all_projects = True

        collect(old_catalog)
        collect(new_catalog)
        active_usage = self.registry.usage(active.record_id) if active is not None else {
            "items": [],
            "count": 0,
            "reference": None,
        }
        return {
            "candidate": self._definition_summary(candidate),
            "active": self._definition_summary(active) if active is not None else None,
            "assessment": {
                "requires_independent_approval": assessment.requires_independent_approval,
                "reasons": list(assessment.reasons),
            },
            "diff": (
                self.registry.diff(active.record_id, candidate.record_id)
                if active is not None
                else {
                    "changed": True,
                    "changed_paths": ["$"],
                    "left": None,
                    "right": {
                        "record_id": candidate.record_id,
                        "revision": candidate.revision,
                        "checksum": candidate.checksum,
                    },
                }
            ),
            "affected": {
                "role_ids": sorted(changed_roles),
                "binding_ids": sorted(changed_bindings),
                "delegation_ids": sorted(changed_delegations),
                "identity_ids": sorted(identities),
                "team_ids": sorted(teams),
                "project_ids": sorted(projects),
                "all_projects_in_scope": all_projects,
                "active_work": active_usage["items"],
                "active_work_count": active_usage["count"],
            },
        }
