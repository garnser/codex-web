from __future__ import annotations

import time
from collections import deque
from typing import Iterable

from codex_web.identity import AuthenticationActor, MembershipRole, PrincipalKind, TenantScope
from codex_web.models import Project
from codex_web.resources import (
    ProjectResourceBinding,
    Resource,
    ResourceAlias,
    ResourceCreate,
    ResourceLifecycle,
    ResourceRelationship,
    RepositoryExecutionTarget,
    RepositoryTargetEvidence,
    RepositoryTargetSource,
    ResourceRelationshipType,
    ResourceType,
    ResourceUpdate,
)
from codex_web.services.identity import AuthorizationError, TenantIsolationError
from codex_web.storage.resource_catalog import ResourceCatalogStore


class ResourceCatalogError(RuntimeError):
    pass


class ResourceNotFoundError(ResourceCatalogError):
    pass


class ResourceAmbiguousError(ResourceCatalogError):
    pass


class ResourceConflictError(ResourceCatalogError):
    pass


class RepositoryTargetSelectionError(ResourceCatalogError):
    code = "repository_target_invalid"


class RepositoryTargetAmbiguousError(RepositoryTargetSelectionError):
    code = "repository_target_ambiguous"


class RepositoryTargetUnauthorizedError(RepositoryTargetSelectionError):
    code = "repository_target_unauthorized"


class RepositoryTargetConflictError(RepositoryTargetSelectionError):
    code = "repository_target_conflict"


class RepositoryTargetMissingError(RepositoryTargetSelectionError):
    code = "repository_target_missing"


class ResourceCatalogService:
    def __init__(self, store: ResourceCatalogStore) -> None:
        self.store = store

    @staticmethod
    def _admin(actor: AuthenticationActor) -> bool:
        if actor.principal_kind == PrincipalKind.SERVICE:
            return "resource:admin" in actor.service_scopes
        return actor.has_role(MembershipRole.OWNER, MembershipRole.ADMIN)

    @staticmethod
    def _scope_matches(resource: Resource, scope: TenantScope) -> bool:
        return (
            resource.organization_id == scope.organization_id
            and resource.workspace_id == scope.workspace_id
        )

    @staticmethod
    def _require_scope(actor: AuthenticationActor, scope: TenantScope) -> None:
        if actor.tenant != scope:
            raise TenantIsolationError("cross-tenant resource access denied")

    @classmethod
    def _require_admin(cls, actor: AuthenticationActor) -> None:
        if not cls._admin(actor):
            raise AuthorizationError("resource administration authority required")

    def list(
        self,
        actor: AuthenticationActor,
        *,
        resource_type: ResourceType | None = None,
        lifecycle: ResourceLifecycle | None = None,
    ) -> list[Resource]:
        resources = [
            item
            for item in self.store.load().resources
            if self._scope_matches(item, actor.tenant)
        ]
        if resource_type is not None:
            resources = [item for item in resources if item.resource_type == resource_type]
        if lifecycle is not None:
            resources = [item for item in resources if item.lifecycle == lifecycle]
        return sorted(resources, key=lambda item: (item.resource_type.value, item.name.casefold(), item.id))

    def get(self, resource_id: str, actor: AuthenticationActor) -> Resource:
        item = next(
            (
                resource
                for resource in self.store.load().resources
                if resource.id == resource_id
                and self._scope_matches(resource, actor.tenant)
            ),
            None,
        )
        if item is None:
            raise ResourceNotFoundError("resource not found")
        return item

    @staticmethod
    def _alias_conflicts(
        resources: Iterable[Resource],
        aliases: Iterable[ResourceAlias],
        *,
        excluding_id: str | None = None,
    ) -> list[tuple[str, str, str]]:
        candidate = {alias.key() for alias in aliases}
        conflicts: list[tuple[str, str, str]] = []
        for resource in resources:
            if resource.id == excluding_id:
                continue
            for alias in resource.aliases:
                if alias.key() in candidate:
                    conflicts.append(alias.key())
        return conflicts

    def create(
        self,
        payload: ResourceCreate,
        *,
        actor: AuthenticationActor,
    ) -> Resource:
        self._require_admin(actor)
        resource = Resource(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            **payload.model_dump(),
        )
        state = self.store.load()
        scoped = [
            item
            for item in state.resources
            if self._scope_matches(item, actor.tenant)
        ]
        conflicts = self._alias_conflicts(scoped, resource.aliases)
        if conflicts:
            raise ResourceConflictError(
                f"resource alias already bound: {conflicts[0]}"
            )

        def apply(current):
            current.resources.append(resource)
            return current

        self.store.update(apply)
        return resource

    def update(
        self,
        resource_id: str,
        payload: ResourceUpdate,
        *,
        actor: AuthenticationActor,
    ) -> Resource:
        self._require_admin(actor)
        current = self.get(resource_id, actor)
        changes = payload.model_dump(exclude_unset=True)
        for key in ("name", "sensitivity", "risk", "lifecycle", "aliases"):
            if changes.get(key) is None:
                changes.pop(key, None)
        changes["updated_at"] = time.time()
        updated = Resource.model_validate(
            {**current.model_dump(mode="json"), **changes}
        )
        state = self.store.load()
        scoped = [
            item
            for item in state.resources
            if self._scope_matches(item, actor.tenant)
        ]
        conflicts = self._alias_conflicts(
            scoped,
            updated.aliases,
            excluding_id=current.id,
        )
        if conflicts:
            raise ResourceConflictError(
                f"resource alias already bound: {conflicts[0]}"
            )

        def apply(catalog):
            catalog.resources = [
                updated if item.id == resource_id else item
                for item in catalog.resources
            ]
            return catalog

        self.store.update(apply)
        return updated

    def resolve(
        self,
        value: str,
        *,
        actor: AuthenticationActor,
        expected_type: ResourceType | None = None,
        alias_namespace: str | None = None,
        provider: str | None = None,
    ) -> Resource:
        normalized = str(value or "").strip()
        if not normalized:
            raise ResourceNotFoundError("resource identifier is required")
        resources = self.list(actor)
        by_id = [item for item in resources if item.id == normalized]
        candidates = by_id
        if not candidates:
            alias_key_value = normalized.casefold()
            candidates = []
            for item in resources:
                for alias in item.aliases:
                    if alias.value.casefold() != alias_key_value:
                        continue
                    if alias_namespace and alias.namespace.casefold() != alias_namespace.casefold():
                        continue
                    if provider and (alias.provider or "").casefold() != provider.casefold():
                        continue
                    candidates.append(item)
                    break
        if expected_type is not None:
            candidates = [item for item in candidates if item.resource_type == expected_type]
        unique = {item.id: item for item in candidates}
        if not unique:
            raise ResourceNotFoundError("resource could not be resolved")
        if len(unique) > 1:
            raise ResourceAmbiguousError(
                "resource alias is ambiguous; use a stable canonical resource id"
            )
        result = next(iter(unique.values()))
        if result.lifecycle in {ResourceLifecycle.DISABLED, ResourceLifecycle.DELETED}:
            raise ResourceNotFoundError("resource is not active for privileged use")
        return result

    def resolve_repository_target(
        self,
        project: Project,
        *,
        actor: AuthenticationActor,
        work_item_resource_ids: Iterable[str] = (),
        work_item_ref: str | None = None,
        explicit_repository_id: str | None = None,
        thread_profile_repository_id: str | None = None,
        routing_repository_id: str | None = None,
        read_only_repository_ids: Iterable[str] = (),
        orchestration_only: bool = False,
    ) -> RepositoryExecutionTarget:
        """Resolve one mutable repository from canonical execution context.

        Direct selectors are validated independently and must converge on the
        same active Project-bound repository. Precedence determines provenance
        only after convergence; it never hides contradictory authority.
        """

        if (
            project.organization_id != actor.organization_id
            or project.workspace_id != actor.workspace_id
        ):
            raise TenantIsolationError(
                "cross-tenant repository target denied"
            )

        state = self.store.load()
        bound = self.project_resources(project, actor=actor)
        repositories = {
            item.id: item
            for item in bound
            if item.resource_type == ResourceType.REPOSITORY
            and item.lifecycle == ResourceLifecycle.ACTIVE
        }

        def require_bound(repository_id: str, source: str) -> str:
            normalized = str(repository_id or "").strip()
            if not normalized:
                raise RepositoryTargetMissingError(
                    f"{source} repository target is empty"
                )
            scoped = next(
                (
                    item
                    for item in state.resources
                    if item.id == normalized
                    and self._scope_matches(item, actor.tenant)
                ),
                None,
            )
            if scoped is None:
                raise RepositoryTargetUnauthorizedError(
                    f"{source} repository target is unavailable in this tenant"
                )
            if scoped.resource_type != ResourceType.REPOSITORY:
                raise RepositoryTargetUnauthorizedError(
                    f"{source} target is not a repository resource"
                )
            if scoped.lifecycle != ResourceLifecycle.ACTIVE:
                raise RepositoryTargetUnauthorizedError(
                    f"{source} repository is not active"
                )
            if normalized not in repositories:
                raise RepositoryTargetUnauthorizedError(
                    f"{source} repository is not bound to project"
                )
            return normalized

        work_item_repo_ids: list[str] = []
        for resource_id in dict.fromkeys(
            str(value).strip()
            for value in work_item_resource_ids
            if str(value).strip()
        ):
            scoped = next(
                (
                    item
                    for item in state.resources
                    if item.id == resource_id
                    and self._scope_matches(item, actor.tenant)
                ),
                None,
            )
            if scoped is None:
                raise RepositoryTargetUnauthorizedError(
                    "work-item resource is unavailable in this tenant"
                )
            if scoped.resource_type != ResourceType.REPOSITORY:
                continue
            work_item_repo_ids.append(
                require_bound(scoped.id, "work item")
            )

        if len(work_item_repo_ids) > 1:
            raise RepositoryTargetAmbiguousError(
                "work item targets multiple repositories; explicit work-item "
                "repository scope is required"
            )

        routing_id = str(routing_repository_id or "").strip() or None
        if routing_id is None:
            routing_bindings = [
                item
                for item in state.project_bindings
                if item.project_id == project.id
                and item.organization_id == project.organization_id
                and item.workspace_id == project.workspace_id
                and item.purpose == "execution-default"
            ]
            if len(routing_bindings) > 1:
                raise RepositoryTargetAmbiguousError(
                    "project has multiple execution-default repository bindings"
                )
            if routing_bindings:
                routing_id = routing_bindings[0].resource_id

        selectors: list[
            tuple[
                RepositoryTargetSource,
                str,
                str | None,
            ]
        ] = []
        if work_item_repo_ids:
            selectors.append(
                (
                    RepositoryTargetSource.WORK_ITEM,
                    work_item_repo_ids[0],
                    work_item_ref,
                )
            )
        if explicit_repository_id:
            explicit_id = require_bound(
                explicit_repository_id,
                "explicit",
            )
            selectors.append(
                (
                    RepositoryTargetSource.EXPLICIT,
                    explicit_id,
                    explicit_id,
                )
            )
        if thread_profile_repository_id:
            profile_id = require_bound(
                thread_profile_repository_id,
                "thread profile",
            )
            selectors.append(
                (
                    RepositoryTargetSource.THREAD_PROFILE,
                    profile_id,
                    profile_id,
                )
            )
        if routing_id:
            canonical_routing_id = require_bound(
                routing_id,
                "routing",
            )
            selectors.append(
                (
                    RepositoryTargetSource.ROUTING_RULE,
                    canonical_routing_id,
                    canonical_routing_id,
                )
            )

        if orchestration_only:
            if selectors:
                raise RepositoryTargetConflictError(
                    "orchestration-only execution conflicts with repository "
                    "target selectors"
                )
            return RepositoryExecutionTarget(
                organization_id=project.organization_id,
                workspace_id=project.workspace_id,
                project_id=project.id,
                mutable_repository_id=None,
                read_only_repository_ids=(),
                source=RepositoryTargetSource.ORCHESTRATION_ONLY,
                source_ref="explicit",
                selection_evidence=(),
            )

        selected_id: str | None = None
        source: RepositoryTargetSource | None = None
        source_ref: str | None = None
        evidence: tuple[RepositoryTargetEvidence, ...] = ()

        if selectors:
            unique_targets = {
                repository_id
                for _source, repository_id, _source_ref in selectors
            }
            if len(unique_targets) > 1:
                summary = ", ".join(
                    f"{selector_source.value}={repository_id}"
                    for selector_source, repository_id, _source_ref
                    in selectors
                )
                raise RepositoryTargetConflictError(
                    "repository target selectors conflict: " + summary
                )
            selected_id = next(iter(unique_targets))
            source, _selected, source_ref = selectors[0]
            evidence = tuple(
                RepositoryTargetEvidence(
                    source=selector_source,
                    repository_id=repository_id,
                    source_ref=selector_ref,
                )
                for selector_source, repository_id, selector_ref
                in selectors
            )
        elif project.repository_selection_policy == "explicit":
            if not repositories:
                raise RepositoryTargetMissingError(
                    "project has no active canonical repository resource"
                )
            raise RepositoryTargetMissingError(
                "project requires a contextual repository target for each "
                "repository execution"
            )
        elif len(repositories) == 1:
            selected_id = next(iter(repositories))
            source = RepositoryTargetSource.SINGLE_REPOSITORY
            source_ref = selected_id
            evidence = (
                RepositoryTargetEvidence(
                    source=source,
                    repository_id=selected_id,
                    source_ref=selected_id,
                ),
            )
        elif not repositories:
            raise RepositoryTargetMissingError(
                "project has no active canonical repository resource"
            )
        else:
            raise RepositoryTargetAmbiguousError(
                "project has multiple active repositories and no deterministic "
                "execution target"
            )

        read_only: list[str] = []
        for repository_id in read_only_repository_ids:
            normalized = require_bound(
                repository_id,
                "read-only context",
            )
            if normalized != selected_id and normalized not in read_only:
                read_only.append(normalized)

        return RepositoryExecutionTarget(
            organization_id=project.organization_id,
            workspace_id=project.workspace_id,
            project_id=project.id,
            mutable_repository_id=selected_id,
            read_only_repository_ids=tuple(read_only),
            source=source,
            source_ref=source_ref,
            selection_evidence=evidence,
        )

    def add_relationship(
        self,
        *,
        from_resource_id: str,
        to_resource_id: str,
        relationship_type: ResourceRelationshipType,
        actor: AuthenticationActor,
    ) -> ResourceRelationship:
        self._require_admin(actor)
        if from_resource_id == to_resource_id:
            raise ResourceConflictError("resource cannot relate to itself")
        source = self.get(from_resource_id, actor)
        target = self.get(to_resource_id, actor)
        if source.tenant != target.tenant:
            raise TenantIsolationError("cross-tenant resource relationship denied")
        state = self.store.load()
        existing = next(
            (
                item
                for item in state.relationships
                if item.organization_id == actor.organization_id
                and item.workspace_id == actor.workspace_id
                and item.from_resource_id == source.id
                and item.to_resource_id == target.id
                and item.relationship_type == relationship_type
            ),
            None,
        )
        if existing is not None:
            return existing
        relationship = ResourceRelationship(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            from_resource_id=source.id,
            to_resource_id=target.id,
            relationship_type=relationship_type,
            created_by=actor.identity_id,
        )

        def apply(catalog):
            catalog.relationships.append(relationship)
            return catalog

        self.store.update(apply)
        return relationship

    def relationships(
        self,
        resource_id: str,
        *,
        actor: AuthenticationActor,
        direction: str = "both",
    ) -> list[ResourceRelationship]:
        self.get(resource_id, actor)
        if direction not in {"incoming", "outgoing", "both"}:
            raise ValueError("direction must be incoming, outgoing, or both")
        rows = []
        for item in self.store.load().relationships:
            if (
                item.organization_id != actor.organization_id
                or item.workspace_id != actor.workspace_id
            ):
                continue
            if direction in {"outgoing", "both"} and item.from_resource_id == resource_id:
                rows.append(item)
            elif direction in {"incoming", "both"} and item.to_resource_id == resource_id:
                rows.append(item)
        return sorted(rows, key=lambda item: (item.relationship_type.value, item.id))

    def traverse(
        self,
        resource_id: str,
        *,
        actor: AuthenticationActor,
        direction: str = "outgoing",
        relationship_types: set[ResourceRelationshipType] | None = None,
        max_depth: int = 10,
    ) -> list[Resource]:
        if max_depth < 1 or max_depth > 100:
            raise ValueError("max_depth must be between 1 and 100")
        if direction not in {"incoming", "outgoing", "both"}:
            raise ValueError("direction must be incoming, outgoing, or both")
        root = self.get(resource_id, actor)
        state = self.store.load()
        resources = {
            item.id: item
            for item in state.resources
            if self._scope_matches(item, actor.tenant)
        }
        relationships = [
            item
            for item in state.relationships
            if item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
            and (
                relationship_types is None
                or item.relationship_type in relationship_types
            )
        ]
        queue = deque([(root.id, 0)])
        visited = {root.id}
        result: list[Resource] = []
        while queue:
            current_id, depth = queue.popleft()
            if depth >= max_depth:
                continue
            adjacent: set[str] = set()
            for relation in relationships:
                if direction in {"outgoing", "both"} and relation.from_resource_id == current_id:
                    adjacent.add(relation.to_resource_id)
                if direction in {"incoming", "both"} and relation.to_resource_id == current_id:
                    adjacent.add(relation.from_resource_id)
            for adjacent_id in sorted(adjacent):
                if adjacent_id in visited or adjacent_id not in resources:
                    continue
                visited.add(adjacent_id)
                result.append(resources[adjacent_id])
                queue.append((adjacent_id, depth + 1))
        return result

    def bind_project(
        self,
        *,
        project: Project,
        resource_id: str,
        actor: AuthenticationActor,
        purpose: str | None = None,
    ) -> ProjectResourceBinding:
        self._require_admin(actor)
        if (
            project.organization_id != actor.organization_id
            or project.workspace_id != actor.workspace_id
        ):
            raise TenantIsolationError("cross-tenant project resource binding denied")
        resource = self.get(resource_id, actor)
        state = self.store.load()
        existing = next(
            (
                item
                for item in state.project_bindings
                if item.project_id == project.id
                and item.resource_id == resource.id
                and item.organization_id == actor.organization_id
                and item.workspace_id == actor.workspace_id
            ),
            None,
        )
        if existing is not None:
            return existing
        binding = ProjectResourceBinding(
            project_id=project.id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            resource_id=resource.id,
            purpose=purpose,
            created_by=actor.identity_id,
        )

        def apply(catalog):
            catalog.project_bindings.append(binding)
            return catalog

        self.store.update(apply)
        return binding

    def resource_ids_for_project(
        self,
        project: Project,
        *,
        alias_value: str | None = None,
        provider: str | None = None,
    ) -> list[str]:
        """Internal projection helper; caller must supply canonical Project scope."""

        state = self.store.load()
        ids = {
            item.resource_id
            for item in state.project_bindings
            if item.project_id == project.id
            and item.organization_id == project.organization_id
            and item.workspace_id == project.workspace_id
        }
        normalized_alias = str(alias_value or "").strip().strip("/").casefold()
        normalized_provider = str(provider or "").strip().casefold()
        valid = set()
        for item in state.resources:
            if (
                item.id not in ids
                or item.organization_id != project.organization_id
                or item.workspace_id != project.workspace_id
                or item.lifecycle
                in {ResourceLifecycle.DISABLED, ResourceLifecycle.DELETED}
            ):
                continue
            if normalized_alias:
                if item.resource_type != ResourceType.REPOSITORY:
                    continue
                alias_matches = any(
                    alias.value.strip().strip("/").casefold()
                    == normalized_alias
                    and (
                        not normalized_provider
                        or (alias.provider or "").strip().casefold()
                        == normalized_provider
                    )
                    for alias in item.aliases
                )
                provenance = item.provenance
                provenance_matches = bool(
                    provenance
                    and str(provenance.external_id or "")
                    .strip()
                    .strip("/")
                    .casefold()
                    == normalized_alias
                    and (
                        not normalized_provider
                        or str(provenance.provider or "").strip().casefold()
                        == normalized_provider
                    )
                )
                if not alias_matches and not provenance_matches:
                    continue
            valid.add(item.id)
        return sorted(valid)

    def project_resources(
        self,
        project: Project,
        *,
        actor: AuthenticationActor,
    ) -> list[Resource]:
        if (
            project.organization_id != actor.organization_id
            or project.workspace_id != actor.workspace_id
        ):
            raise TenantIsolationError("cross-tenant project resource lookup denied")
        state = self.store.load()
        ids = {
            item.resource_id
            for item in state.project_bindings
            if item.project_id == project.id
            and item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        }
        resources = [
            item
            for item in state.resources
            if item.id in ids and self._scope_matches(item, actor.tenant)
        ]
        return sorted(resources, key=lambda item: (item.resource_type.value, item.name.casefold()))

    def migrate_legacy_strings(
        self,
        *,
        project: Project,
        actor: AuthenticationActor,
        repositories: Iterable[str] = (),
        environments: Iterable[str] = (),
    ) -> list[Resource]:
        self._require_admin(actor)
        if project.organization_id != actor.organization_id or project.workspace_id != actor.workspace_id:
            raise TenantIsolationError("cross-tenant legacy resource migration denied")
        created_or_existing: list[Resource] = []
        for resource_type, values in (
            (ResourceType.REPOSITORY, repositories),
            (ResourceType.ENVIRONMENT, environments),
        ):
            for raw in values:
                value = str(raw or "").strip()
                if not value:
                    continue
                try:
                    resource = self.resolve(
                        value,
                        actor=actor,
                        expected_type=resource_type,
                        alias_namespace="legacy",
                    )
                except ResourceNotFoundError:
                    resource = self.create(
                        ResourceCreate(
                            resource_type=resource_type,
                            name=value,
                            aliases=[ResourceAlias(namespace="legacy", value=value)],
                        ),
                        actor=actor,
                    )
                self.bind_project(
                    project=project,
                    resource_id=resource.id,
                    actor=actor,
                    purpose="legacy-migration",
                )
                created_or_existing.append(resource)
        return created_or_existing
