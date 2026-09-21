from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from codex_web.canonical_materialization import (
    CANONICAL_MATERIALIZATION_VERSION,
    CanonicalMaterializationExecution,
    CanonicalMaterializationOperation,
    CanonicalMaterializationPlan,
    MaterializationDisposition,
    MaterializationExecutionStatus,
)
from codex_web.identity import AuthenticationActor, TenantScope
from codex_web.models import (
    GitLabRoutingSettings,
    Project,
    TaskSourceConfiguration,
    WorkItemState,
)
from codex_web.resources import (
    ResourceAlias,
    ResourceCreate,
    ResourceProvenance,
    ResourceType,
)
from codex_web.secrets import SecretCreate, SecretStatus
from codex_web.services.identity import IdentityService
from codex_web.services.projects import ProjectService
from codex_web.services.resources import (
    ResourceAmbiguousError,
    ResourceCatalogService,
    ResourceNotFoundError,
)
from codex_web.services.secrets import SecretBroker
from codex_web.storage.canonical_materialization import (
    CanonicalMaterializationStore,
)


class CanonicalMaterializationError(RuntimeError):
    pass


class CanonicalMaterializationPlanStale(
    CanonicalMaterializationError
):
    pass


class CanonicalMaterializationBlocked(
    CanonicalMaterializationError
):
    pass


class CanonicalMaterializationService:
    """Domain materializers for legacy/imported canonical state.

    Bootstrap orchestration/checkpoint semantics live in #487. This service
    owns deterministic domain preview/apply operations and intentionally
    contains no provider mutation.
    """

    GENERIC_SCOPE = TenantScope(
        organization_id="local",
        workspace_id="default",
    )

    def __init__(
        self,
        *,
        projects: ProjectService,
        resources: ResourceCatalogService,
        work_items,
        secrets: SecretBroker,
        load_gitlab_routing: Callable[[], GitLabRoutingSettings],
        legacy_gitlab_token: Callable[[str], str | None],
        gitlab_api_base: str,
        store: CanonicalMaterializationStore,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.projects = projects
        self.resources = resources
        self.work_items = work_items
        self.secrets = secrets
        self.load_gitlab_routing = load_gitlab_routing
        self.legacy_gitlab_token = legacy_gitlab_token
        self.gitlab_api_base = str(gitlab_api_base).rstrip("/")
        self.store = store
        self.clock = clock

    @staticmethod
    def _require_admin(actor: AuthenticationActor) -> None:
        IdentityService.require_admin(actor)

    def _legacy_project(self, project_id: str) -> Project:
        project = next(
            (
                item
                for item in self.projects.repository.load()
                if item.id == project_id
            ),
            None,
        )
        if project is None:
            raise CanonicalMaterializationError(
                "legacy Project not found"
            )
        return project

    @staticmethod
    def _safe_root(project: Project) -> Path:
        root = Path(project.path).resolve(strict=True)
        if not root.is_dir():
            raise CanonicalMaterializationError(
                "Project path is not a directory"
            )
        return root

    @staticmethod
    def _git_root(path: Path) -> bool:
        marker = path / ".git"
        return marker.is_dir() or marker.is_file()

    def _discover_repositories(self, project: Project) -> tuple[Path, ...]:
        root = self._safe_root(project)
        discovered: list[Path] = []
        if self._git_root(root):
            discovered.append(root)

        for current, dirs, _files in os.walk(
            root,
            followlinks=False,
        ):
            current_path = Path(current)
            dirs[:] = [
                name
                for name in dirs
                if name != ".git"
                and not (current_path / name).is_symlink()
            ]
            if current_path == root:
                continue
            if not self._git_root(current_path):
                continue
            resolved = current_path.resolve(strict=True)
            if resolved.is_relative_to(root):
                discovered.append(resolved)
            dirs[:] = []

        return tuple(
            sorted(
                {item.resolve() for item in discovered},
                key=lambda item: (len(item.parts), str(item)),
            )
        )

    @staticmethod
    def _repo_key(root: Path, path: Path) -> str:
        if path == root:
            return "root"
        relative = path.relative_to(root)
        raw = "-".join(relative.parts).casefold()
        normalized = "".join(
            char if char.isalnum() or char in "-._" else "-"
            for char in raw
        ).strip("-._")
        return normalized or hashlib.sha256(
            str(relative).encode()
        ).hexdigest()[:12]

    def _routing_paths(
        self,
        project_id: str,
    ) -> tuple[str, ...]:
        routing = self.load_gitlab_routing()
        project = routing.projects.get(project_id)
        if project is None or not project.enabled:
            return ()
        return tuple(
            sorted(
                {
                    str(value or "").strip().strip("/")
                    for value in project.project_paths
                    if str(value or "").strip().strip("/")
                }
            )
        )

    @staticmethod
    def _gitlab_aliases_for_repositories(
        repositories: tuple[Path, ...],
        routing_paths: tuple[str, ...],
    ) -> dict[Path, tuple[str, ...]]:
        mapping: dict[Path, list[str]] = {
            item: [] for item in repositories
        }
        if len(repositories) == 1 and len(routing_paths) == 1:
            mapping[repositories[0]].append(routing_paths[0])
        else:
            for project_path in routing_paths:
                leaf = project_path.rsplit("/", 1)[-1].casefold()
                matches = [
                    repo
                    for repo in repositories
                    if repo.name.casefold() == leaf
                ]
                if len(matches) == 1:
                    mapping[matches[0]].append(project_path)
        return {
            key: tuple(sorted(set(values)))
            for key, values in mapping.items()
        }

    @staticmethod
    def _operation(
        *,
        operation_id: str,
        domain: str,
        record_ref: str,
        disposition: MaterializationDisposition,
        reason_code: str,
        message: str,
        apply_kind: str | None = None,
        dependencies: tuple[str, ...] = (),
        metadata: dict[str, Any] | None = None,
        operator_action: str | None = None,
    ) -> CanonicalMaterializationOperation:
        return CanonicalMaterializationOperation(
            id=operation_id,
            domain=domain,
            record_ref=record_ref,
            disposition=disposition,
            reason_code=reason_code,
            message=message,
            apply_kind=apply_kind,
            dependencies=dependencies,
            metadata=metadata or {},
            operator_action=operator_action,
        )

    def _resource_for_path(
        self,
        path: Path,
        *,
        actor: AuthenticationActor,
    ):
        try:
            return self.resources.resolve(
                str(path),
                actor=actor,
                expected_type=ResourceType.REPOSITORY,
                alias_namespace="filesystem",
            )
        except ResourceNotFoundError:
            return None
        except ResourceAmbiguousError as exc:
            raise CanonicalMaterializationError(
                f"ambiguous canonical repository alias: {path}"
            ) from exc

    def _secret_by_id(
        self,
        secret_id: str | None,
        *,
        actor: AuthenticationActor,
    ):
        normalized = str(secret_id or "").strip()
        if not normalized:
            return None
        try:
            reference = self.secrets.metadata(
                normalized,
                actor=actor,
                require_use=True,
            )
        except Exception:
            return None
        return (
            reference
            if reference.status() == SecretStatus.ACTIVE
            else None
        )

    def _secret_reference(
        self,
        project_id: str,
        *,
        actor: AuthenticationActor,
    ):
        purpose = f"legacy-materialization:{project_id}:task-source"
        matches = [
            item
            for item in self.secrets.list(actor)
            if (item.provider or "").casefold() == "gitlab"
            and item.purpose == purpose
            and item.status() == SecretStatus.ACTIVE
        ]
        if len(matches) > 1:
            raise CanonicalMaterializationError(
                "multiple active canonical GitLab migration credentials"
            )
        return matches[0] if matches else None

    def _legacy_token_available(self, project_id: str) -> bool:
        # The value is intentionally reduced to a boolean immediately and is
        # never placed into a plan/report/log.
        return bool(self.legacy_gitlab_token(project_id))

    @staticmethod
    def _task_source_scope(
        routing_paths: tuple[str, ...],
        work_items: tuple[WorkItemState, ...],
    ) -> tuple[str | None, str | None]:
        groups = {
            value.split("/", 1)[0]
            for value in routing_paths
            if value
        }
        for item in work_items:
            identity = item.source_identity
            if (
                identity is not None
                and identity.source_type.casefold() == "gitlab"
            ):
                external = str(identity.external_id or "").split(
                    "#",
                    1,
                )[0].strip("/")
                if external:
                    groups.add(external.split("/", 1)[0])
            ref = str(item.ref or "")
            if "#" in ref:
                prefix = ref.split("#", 1)[0].strip("/")
                if prefix and "/" in prefix:
                    groups.add(prefix.split("/", 1)[0])
        if len(groups) == 1:
            return next(iter(groups)), None
        if not groups:
            return None, "task_source_scope_missing"
        return None, "task_source_scope_ambiguous"

    @staticmethod
    def _repo_for_work_item(
        item: WorkItemState,
        repositories: tuple[tuple[Path, str, tuple[str, ...]], ...],
    ) -> tuple[str | None, str | None]:
        candidates: set[str] = set()
        if item.project_path:
            try:
                path = Path(item.project_path).resolve(strict=False)
            except Exception:
                path = None
            if path is not None:
                matches = [
                    resource_id
                    for repo_path, resource_id, _aliases in repositories
                    if path == repo_path
                    or path.is_relative_to(repo_path)
                ]
                if matches:
                    deepest = max(
                        (
                            row
                            for row in repositories
                            if row[1] in matches
                        ),
                        key=lambda row: len(row[0].parts),
                    )
                    candidates.add(deepest[1])

        source_paths: set[str] = set()
        identity = item.source_identity
        if (
            identity is not None
            and identity.source_type.casefold() == "gitlab"
        ):
            source_paths.add(
                str(identity.external_id or "").split("#", 1)[0]
            )
        ref = str(item.ref or "")
        if "#" in ref:
            source_paths.add(ref.split("#", 1)[0])
        for source_path in source_paths:
            normalized = source_path.strip("/").casefold()
            for _repo_path, resource_id, aliases in repositories:
                if normalized in {
                    value.casefold() for value in aliases
                }:
                    candidates.add(resource_id)

        if len(candidates) == 1:
            return next(iter(candidates)), None
        if len(candidates) > 1:
            return None, "work_item_repository_ambiguous"
        if len(repositories) == 1:
            return repositories[0][1], None
        if not repositories:
            return None, "work_item_repository_missing"
        return None, "work_item_repository_ambiguous"

    @staticmethod
    def _plan_id(payload: dict[str, Any]) -> str:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        return "materialization-plan-" + hashlib.sha256(
            encoded
        ).hexdigest()[:24]

    def plan(
        self,
        project_id: str,
        *,
        actor: AuthenticationActor,
        confirm_generic_target: bool = False,
    ) -> CanonicalMaterializationPlan:
        self._require_admin(actor)
        project = self._legacy_project(project_id)
        source_scope = TenantScope(
            organization_id=project.organization_id,
            workspace_id=project.workspace_id,
        )
        target_scope = actor.tenant
        root = self._safe_root(project)
        routing_paths = self._routing_paths(project.id)
        discovered = self._discover_repositories(project)
        aliases = self._gitlab_aliases_for_repositories(
            discovered,
            routing_paths,
        )
        all_work_items = tuple(
            item
            for item in self.work_items.load().values()
            if item.project_id == project.id
        )
        operations: list[CanonicalMaterializationOperation] = []

        scope_operation_id = "project:scope"
        scope_compatible = source_scope == target_scope
        if (
            source_scope == self.GENERIC_SCOPE
            and target_scope == self.GENERIC_SCOPE
            and not confirm_generic_target
        ):
            operations.append(
                self._operation(
                    operation_id=scope_operation_id,
                    domain="project_scope",
                    record_ref=project.id,
                    disposition=MaterializationDisposition.OPERATOR_ACTION_REQUIRED,
                    reason_code="generic_scope_requires_confirmation",
                    message=(
                        "Project still uses Local / Default; explicit "
                        "confirmation is required to keep that tenant."
                    ),
                    operator_action=(
                        "Confirm Local / Default as intentional ownership "
                        "or choose the intended organization/workspace."
                    ),
                )
            )
            scope_compatible = False
        elif scope_compatible:
            operations.append(
                self._operation(
                    operation_id=scope_operation_id,
                    domain="project_scope",
                    record_ref=project.id,
                    disposition=MaterializationDisposition.UNCHANGED,
                    reason_code="project_scope_canonical",
                    message="Project already has explicit target tenant ownership.",
                )
            )
        elif source_scope == self.GENERIC_SCOPE:
            operations.append(
                    self._operation(
                        operation_id=scope_operation_id,
                        domain="project_scope",
                        record_ref=project.id,
                        disposition=MaterializationDisposition.MIGRATED,
                        reason_code="legacy_generic_scope_rebind",
                        message=(
                            "Legacy Local / Default Project will be rebound "
                            "to the explicitly requested tenant."
                        ),
                        apply_kind="project_scope",
                        metadata={
                            "target_organization_id": target_scope.organization_id,
                            "target_workspace_id": target_scope.workspace_id,
                        },
                    )
            )
            scope_compatible = True
        else:
            operations.append(
                self._operation(
                    operation_id=scope_operation_id,
                    domain="project_scope",
                    record_ref=project.id,
                    disposition=MaterializationDisposition.OPERATOR_ACTION_REQUIRED,
                    reason_code="project_scope_conflict",
                    message=(
                        "Project already belongs to a different non-generic "
                        "tenant and will not be reassigned automatically."
                    ),
                    operator_action=(
                        "Resolve tenant ownership explicitly before canonical "
                        "materialization."
                    ),
                )
            )
            scope_compatible = False

        # Existing legacy-scoped Resources are not silently moved across a
        # tenant boundary because Resource identity itself carries authority.
        if source_scope != target_scope:
            catalog = self.resources.store.load()
            legacy_bound = [
                binding
                for binding in catalog.project_bindings
                if binding.project_id == project.id
                and (
                    binding.organization_id,
                    binding.workspace_id,
                )
                == (
                    source_scope.organization_id,
                    source_scope.workspace_id,
                )
            ]
            if legacy_bound:
                operations.append(
                    self._operation(
                        operation_id="resources:legacy-scope",
                        domain="resources",
                        record_ref=project.id,
                        disposition=MaterializationDisposition.OPERATOR_ACTION_REQUIRED,
                        reason_code="legacy_resource_scope_conflict",
                        message=(
                            "Legacy Project already has canonical Resources in "
                            "its old tenant; Resource identities are not moved "
                            "silently across tenants."
                        ),
                        operator_action=(
                            "Reconcile or explicitly migrate the existing "
                            "Resource identities before applying."
                        ),
                    )
                )
                scope_compatible = False

        repository_rows: list[
            tuple[Path, str, tuple[str, ...]]
        ] = []
        for path in discovered:
            key = self._repo_key(root, path)
            operation_id = f"repository:{key}"
            existing = self._resource_for_path(
                path,
                actor=actor,
            )
            if existing is not None:
                resource_id = existing.id
                disposition = MaterializationDisposition.UNCHANGED
                reason = "repository_resource_exists"
                message = "Canonical repository Resource already exists."
                apply_kind = (
                    "repository_bind"
                    if existing.id
                    not in self.resources.resource_ids_for_project(
                        project.model_copy(
                            update={
                                "organization_id": target_scope.organization_id,
                                "workspace_id": target_scope.workspace_id,
                            }
                        )
                    )
                    and scope_compatible
                    else None
                )
                if apply_kind:
                    disposition = MaterializationDisposition.MIGRATED
                    reason = "repository_binding_missing"
                    message = (
                        "Existing canonical repository Resource will be bound "
                        "to the Project."
                    )
            else:
                resource_id = f"planned-repository:{key}"
                disposition = (
                    MaterializationDisposition.MIGRATED
                    if scope_compatible
                    else MaterializationDisposition.UNRESOLVED
                )
                reason = (
                    "repository_resource_missing"
                    if scope_compatible
                    else "project_scope_unresolved"
                )
                message = (
                    "Canonical repository Resource will be created and bound."
                    if scope_compatible
                    else "Repository cannot be materialized until Project scope is resolved."
                )
                apply_kind = "repository_create" if scope_compatible else None
            gitlab_aliases = aliases[path]
            operations.append(
                self._operation(
                    operation_id=operation_id,
                    domain="repository_resource",
                    record_ref=str(path),
                    disposition=disposition,
                    reason_code=reason,
                    message=message,
                    apply_kind=apply_kind,
                    dependencies=(
                        (scope_operation_id,)
                        if source_scope != target_scope
                        else ()
                    ),
                    metadata={
                        "repository_key": key,
                        "filesystem_path": str(path),
                        "resource_reference": resource_id,
                        "gitlab_paths": list(gitlab_aliases),
                    },
                )
            )
            repository_rows.append(
                (path, resource_id, gitlab_aliases)
            )

        if not discovered:
            operations.append(
                self._operation(
                    operation_id="repositories:none",
                    domain="repository_resource",
                    record_ref=project.id,
                    disposition=MaterializationDisposition.UNRESOLVED,
                    reason_code="repository_missing",
                    message="No Git repository was discovered under the Project root.",
                    operator_action=(
                        "Bind the intended canonical repository Resource "
                        "explicitly."
                    ),
                )
            )

        current_source = project.authoritative_task_source
        bound_secret = self._secret_by_id(
            (
                current_source.credential_secret_id
                if current_source is not None
                else None
            ),
            actor=actor,
        )
        secret = (
            bound_secret
            or self._secret_reference(project.id, actor=actor)
        )
        token_available = False
        if secret is None:
            token_available = self._legacy_token_available(project.id)
        secret_operation_id = "secret:gitlab"
        if secret is not None:
            operations.append(
                self._operation(
                    operation_id=secret_operation_id,
                    domain="secret_reference",
                    record_ref=project.id,
                    disposition=MaterializationDisposition.UNCHANGED,
                    reason_code="gitlab_secret_reference_exists",
                    message="Canonical GitLab SecretReference already exists.",
                    metadata={
                        "secret_reference_id": secret.id,
                    },
                )
            )
        elif routing_paths and token_available and scope_compatible:
            operations.append(
                self._operation(
                    operation_id=secret_operation_id,
                    domain="secret_reference",
                    record_ref=project.id,
                    disposition=MaterializationDisposition.MIGRATED,
                    reason_code="legacy_gitlab_credential_available",
                    message=(
                        "Legacy GitLab credential will be moved behind a "
                        "canonical SecretReference."
                    ),
                    apply_kind="gitlab_secret",
                    dependencies=(
                        (scope_operation_id,)
                        if source_scope != target_scope
                        else ()
                    ),
                )
            )
        elif routing_paths:
            operations.append(
                self._operation(
                    operation_id=secret_operation_id,
                    domain="secret_reference",
                    record_ref=project.id,
                    disposition=MaterializationDisposition.UNRESOLVED,
                    reason_code="gitlab_credential_missing",
                    message=(
                        "GitLab routing exists but no canonical or supported "
                        "legacy credential is available."
                    ),
                    operator_action=(
                        "Create/import a canonical GitLab SecretReference."
                    ),
                )
            )
        else:
            operations.append(
                self._operation(
                    operation_id=secret_operation_id,
                    domain="secret_reference",
                    record_ref=project.id,
                    disposition=MaterializationDisposition.SKIPPED,
                    reason_code="gitlab_not_configured",
                    message="No legacy GitLab TaskSource configuration was found.",
                )
            )

        task_scope, task_scope_error = self._task_source_scope(
            routing_paths,
            all_work_items,
        )
        task_operation_id = "task-source:gitlab"
        secret_reference = secret.id if secret is not None else None
        if current_source is not None:
            matching = (
                current_source.source_type.casefold() == "gitlab"
                and current_source.source_instance.rstrip("/")
                == self.gitlab_api_base
                and (
                    task_scope is None
                    or current_source.scope == task_scope
                )
                and (
                    not current_source.credential_secret_id
                    or bound_secret is not None
                )
            )
            operations.append(
                self._operation(
                    operation_id=task_operation_id,
                    domain="task_source",
                    record_ref=project.id,
                    disposition=(
                        MaterializationDisposition.UNCHANGED
                        if matching
                        else MaterializationDisposition.OPERATOR_ACTION_REQUIRED
                    ),
                    reason_code=(
                        "task_source_binding_exists"
                        if matching
                        else "task_source_binding_conflict"
                    ),
                    message=(
                        "Project already has a matching authoritative TaskSource."
                        if matching
                        else "Project already has a conflicting authoritative TaskSource."
                    ),
                    operator_action=(
                        None
                        if matching
                        else "Resolve the authoritative TaskSource conflict explicitly."
                    ),
                    metadata={
                        "source_type": current_source.source_type,
                        "source_instance": current_source.source_instance,
                        "scope": current_source.scope,
                        "secret_reference_id": (
                            current_source.credential_secret_id
                            or ""
                        ),
                    },
                )
            )
        elif not routing_paths and not any(
            item.source_identity is not None
            and item.source_identity.source_type.casefold() == "gitlab"
            for item in all_work_items
        ):
            operations.append(
                self._operation(
                    operation_id=task_operation_id,
                    domain="task_source",
                    record_ref=project.id,
                    disposition=MaterializationDisposition.SKIPPED,
                    reason_code="task_source_not_configured",
                    message="No GitLab TaskSource can be safely derived.",
                )
            )
        elif task_scope_error:
            operations.append(
                self._operation(
                    operation_id=task_operation_id,
                    domain="task_source",
                    record_ref=project.id,
                    disposition=MaterializationDisposition.UNRESOLVED,
                    reason_code=task_scope_error,
                    message=(
                        "Authoritative GitLab TaskSource scope cannot be "
                        "derived unambiguously."
                    ),
                    operator_action=(
                        "Select the authoritative GitLab group/scope explicitly."
                    ),
                )
            )
        elif (
            secret is None
            and not (routing_paths and token_available)
        ):
            operations.append(
                self._operation(
                    operation_id=task_operation_id,
                    domain="task_source",
                    record_ref=project.id,
                    disposition=MaterializationDisposition.UNRESOLVED,
                    reason_code="task_source_credential_unresolved",
                    message=(
                        "TaskSource binding requires a canonical GitLab "
                        "SecretReference."
                    ),
                    operator_action="Materialize/provide the GitLab credential.",
                )
            )
        elif scope_compatible:
            operations.append(
                self._operation(
                    operation_id=task_operation_id,
                    domain="task_source",
                    record_ref=project.id,
                    disposition=MaterializationDisposition.MIGRATED,
                    reason_code="task_source_binding_missing",
                    message=(
                        "Project-bound authoritative GitLab TaskSource will be created."
                    ),
                    apply_kind="gitlab_task_source",
                    dependencies=(
                        secret_operation_id,
                        *(
                            (scope_operation_id,)
                            if source_scope != target_scope
                            else ()
                        ),
                    ),
                    metadata={
                        "source_type": "gitlab",
                        "source_instance": self.gitlab_api_base,
                        "scope": task_scope or "",
                        "secret_reference_id": secret_reference or "planned",
                    },
                )
            )

        # Work Item plans refer to stable repository operation IDs rather than
        # random Resource IDs for repositories that do not exist yet.
        repo_by_path = {
            path: f"repository:{self._repo_key(root, path)}"
            for path in discovered
        }
        planned_rows = tuple(
            (
                path,
                (
                    resource.id
                    if (
                        resource := self._resource_for_path(
                            path,
                            actor=actor,
                        )
                    )
                    is not None
                    else repo_by_path[path]
                ),
                aliases[path],
            )
            for path in discovered
        )
        for item in sorted(all_work_items, key=lambda value: value.ref):
            operation_id = f"work-item:{item.ref}"
            if item.resource_ids and (
                item.organization_id == target_scope.organization_id
                and item.workspace_id == target_scope.workspace_id
            ):
                operations.append(
                    self._operation(
                        operation_id=operation_id,
                        domain="work_item",
                        record_ref=item.ref,
                        disposition=MaterializationDisposition.UNCHANGED,
                        reason_code="work_item_resources_exist",
                        message="Work Item already has canonical Resource associations.",
                    )
                )
                continue
            target_ref, reason = self._repo_for_work_item(
                item,
                planned_rows,
            )
            if target_ref is None:
                operations.append(
                    self._operation(
                        operation_id=operation_id,
                        domain="work_item",
                        record_ref=item.ref,
                        disposition=MaterializationDisposition.UNRESOLVED,
                        reason_code=reason or "work_item_repository_unresolved",
                        message=(
                            "Work Item repository association cannot be "
                            "derived unambiguously."
                        ),
                        operator_action=(
                            "Associate the Work Item with an explicit canonical repository Resource."
                        ),
                    )
                )
                continue
            dependency = (
                target_ref
                if target_ref.startswith("repository:")
                else None
            )
            operations.append(
                self._operation(
                    operation_id=operation_id,
                    domain="work_item",
                    record_ref=item.ref,
                    disposition=(
                        MaterializationDisposition.MIGRATED
                        if scope_compatible
                        else MaterializationDisposition.UNRESOLVED
                    ),
                    reason_code=(
                        "work_item_resource_materialization"
                        if scope_compatible
                        else "project_scope_unresolved"
                    ),
                    message=(
                        "Work Item tenant/Resource association will be materialized."
                        if scope_compatible
                        else "Work Item cannot be materialized until Project scope is resolved."
                    ),
                    apply_kind=(
                        "work_item"
                        if scope_compatible
                        else None
                    ),
                    dependencies=tuple(
                        value
                        for value in (
                            dependency,
                            (
                                scope_operation_id
                                if source_scope != target_scope
                                else None
                            ),
                        )
                        if value
                    ),
                    metadata={
                        "repository_reference": target_ref,
                    },
                )
            )

        for domain, reason in (
            (
                "attention",
                "historical_attention_not_safely_derivable",
            ),
            (
                "approval",
                "historical_approval_not_safely_derivable",
            ),
        ):
            operations.append(
                self._operation(
                    operation_id=f"{domain}:historical",
                    domain=domain,
                    record_ref=project.id,
                    disposition=MaterializationDisposition.SKIPPED,
                    reason_code=reason,
                    message=(
                        f"Historical {domain} state is not fabricated from "
                        "legacy records because authority/action history is incomplete."
                    ),
                )
            )

        raw = {
            "version": CANONICAL_MATERIALIZATION_VERSION,
            "project_id": project.id,
            "source_organization_id": source_scope.organization_id,
            "source_workspace_id": source_scope.workspace_id,
            "target_organization_id": target_scope.organization_id,
            "target_workspace_id": target_scope.workspace_id,
            "project_path": str(root),
            "operations": [
                item.model_dump(mode="json")
                for item in operations
            ],
        }
        return CanonicalMaterializationPlan(
            id=self._plan_id(raw),
            project_id=project.id,
            source_organization_id=source_scope.organization_id,
            source_workspace_id=source_scope.workspace_id,
            target_organization_id=target_scope.organization_id,
            target_workspace_id=target_scope.workspace_id,
            project_path=str(root),
            operations=tuple(operations),
            generated_at=self.clock(),
        )

    def _save_execution(
        self,
        execution: CanonicalMaterializationExecution,
    ) -> CanonicalMaterializationExecution:
        def mutate(state):
            state.executions = [
                item
                for item in state.executions
                if item.id != execution.id
                and not (
                    item.plan_id == execution.plan_id
                    and item.organization_id
                    == execution.organization_id
                    and item.workspace_id
                    == execution.workspace_id
                )
            ]
            state.executions.append(execution)
            return state

        saved = self.store.update(mutate)
        return next(
            item
            for item in saved.executions
            if item.id == execution.id
        )

    def apply(
        self,
        plan: CanonicalMaterializationPlan,
        *,
        actor: AuthenticationActor,
        fail_after_operations: int | None = None,
    ) -> CanonicalMaterializationExecution:
        self._require_admin(actor)
        if (
            plan.target_organization_id != actor.organization_id
            or plan.target_workspace_id != actor.workspace_id
        ):
            raise CanonicalMaterializationBlocked(
                "materialization plan belongs to a different target tenant"
            )

        existing = next(
            (
                item
                for item in self.store.executions_for_project(
                    plan.project_id,
                    organization_id=actor.organization_id,
                    workspace_id=actor.workspace_id,
                )
                if item.plan_id == plan.id
            ),
            None,
        )
        if existing is None:
            current = self.plan(
                plan.project_id,
                actor=actor,
                confirm_generic_target=(
                    plan.target_organization_id == "local"
                    and plan.target_workspace_id == "default"
                ),
            )
            if current.id != plan.id:
                raise CanonicalMaterializationPlanStale(
                    "materialization plan is stale; run dry-run again"
                )

        execution = existing or CanonicalMaterializationExecution(
            id=f"materialization-{plan.id.removeprefix('materialization-plan-')}",
            plan_id=plan.id,
            project_id=plan.project_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            plan=plan,
            status=MaterializationExecutionStatus.PLANNED,
            created_at=self.clock(),
            updated_at=self.clock(),
        )
        if execution.status == MaterializationExecutionStatus.APPLIED:
            return execution

        completed = set(execution.applied_operation_ids)
        results = {
            item.id: item
            for item in (execution.records or plan.operations)
        }
        execution.status = MaterializationExecutionStatus.APPLYING
        execution.updated_at = self.clock()
        execution = self._save_execution(execution)
        applied_this_run = 0

        def checkpoint(
            operation: CanonicalMaterializationOperation,
            result: CanonicalMaterializationOperation,
        ) -> None:
            nonlocal execution, applied_this_run
            completed.add(operation.id)
            results[operation.id] = result
            applied_this_run += 1
            execution.applied_operation_ids = tuple(sorted(completed))
            execution.records = tuple(
                results[item.id]
                for item in plan.operations
            )
            execution.updated_at = self.clock()
            execution = self._save_execution(execution)
            if (
                fail_after_operations is not None
                and applied_this_run >= fail_after_operations
            ):
                raise CanonicalMaterializationError(
                    "injected materialization interruption"
                )

        try:
            project = self._legacy_project(plan.project_id)
            resource_ids_by_operation: dict[str, str] = {}
            for operation in plan.operations:
                if operation.id in completed:
                    result = results.get(operation.id, operation)
                    reference = result.metadata.get("resource_id")
                    if isinstance(reference, str) and reference:
                        resource_ids_by_operation[operation.id] = reference
                    continue
                if operation.apply_kind is None:
                    checkpoint(operation, operation)
                    continue
                for dependency in operation.dependencies:
                    if dependency not in completed:
                        raise CanonicalMaterializationBlocked(
                            f"operation dependency not applied: {dependency}"
                        )

                result = operation
                if operation.apply_kind == "project_scope":
                    projects = self.projects.repository.load()
                    updated = project.model_copy(
                        update={
                            "organization_id": actor.organization_id,
                            "workspace_id": actor.workspace_id,
                        }
                    )
                    self.projects.repository.save(
                        [
                            updated if item.id == project.id else item
                            for item in projects
                        ]
                    )
                    project = updated
                elif operation.apply_kind in {
                    "repository_create",
                    "repository_bind",
                }:
                    path = Path(
                        str(operation.metadata["filesystem_path"])
                    ).resolve(strict=True)
                    resource = self._resource_for_path(
                        path,
                        actor=actor,
                    )
                    if resource is None:
                        resource = self.resources.create(
                            ResourceCreate(
                                resource_type=ResourceType.REPOSITORY,
                                name=path.name or project.name,
                                aliases=[
                                    ResourceAlias(
                                        namespace="filesystem",
                                        value=str(path),
                                    ),
                                    *[
                                        ResourceAlias(
                                            namespace="gitlab",
                                            provider="gitlab",
                                            value=value,
                                        )
                                        for value in operation.metadata.get(
                                            "gitlab_paths",
                                            [],
                                        )
                                    ],
                                ],
                                provenance=ResourceProvenance(
                                    provider="legacy-materialization",
                                    provider_instance=(
                                        CANONICAL_MATERIALIZATION_VERSION
                                    ),
                                    external_id=str(path),
                                    discovered_at=self.clock(),
                                    last_seen_at=self.clock(),
                                ),
                            ),
                            actor=actor,
                        )
                    self.resources.bind_project(
                        project=project,
                        resource_id=resource.id,
                        actor=actor,
                        purpose="canonical-materialization-v1",
                    )
                    resource_ids_by_operation[
                        operation.id
                    ] = resource.id
                    result = operation.model_copy(
                        update={
                            "metadata": {
                                **operation.metadata,
                                "resource_id": resource.id,
                            }
                        }
                    )
                elif operation.apply_kind == "gitlab_secret":
                    reference = self._secret_reference(
                        project.id,
                        actor=actor,
                    )
                    if reference is None:
                        value = self.legacy_gitlab_token(project.id)
                        if not value:
                            raise CanonicalMaterializationBlocked(
                                "legacy GitLab credential disappeared before apply"
                            )
                        reference = self.secrets.create(
                            SecretCreate(
                                name=f"{project.name} GitLab task source",
                                value=value,
                                provider="gitlab",
                                purpose=(
                                    f"legacy-materialization:{project.id}:task-source"
                                ),
                            ),
                            actor=actor,
                        )
                    result = operation.model_copy(
                        update={
                            "metadata": {
                                **operation.metadata,
                                "secret_reference_id": reference.id,
                            }
                        }
                    )
                elif operation.apply_kind == "gitlab_task_source":
                    reference = self._secret_reference(
                        project.id,
                        actor=actor,
                    )
                    if reference is None:
                        raise CanonicalMaterializationBlocked(
                            "canonical GitLab SecretReference is unavailable"
                        )
                    source = TaskSourceConfiguration(
                        source_type="gitlab",
                        source_instance=str(
                            operation.metadata["source_instance"]
                        ),
                        scope=str(operation.metadata["scope"]),
                        credential_secret_id=reference.id,
                    )
                    project = self.projects.set_authoritative_task_source(
                        project.id,
                        source,
                        scope=actor.tenant,
                    )
                    result = operation.model_copy(
                        update={
                            "metadata": {
                                **operation.metadata,
                                "secret_reference_id": reference.id,
                            }
                        }
                    )
                elif operation.apply_kind == "work_item":
                    item = self.work_items.get(operation.record_ref)
                    if item is None:
                        raise CanonicalMaterializationBlocked(
                            f"Work Item disappeared: {operation.record_ref}"
                        )
                    repository_reference = str(
                        operation.metadata["repository_reference"]
                    )
                    resource_id = (
                        resource_ids_by_operation.get(repository_reference)
                        if repository_reference.startswith("repository:")
                        else repository_reference
                    )
                    if not resource_id:
                        raise CanonicalMaterializationBlocked(
                            "Work Item repository Resource is unavailable"
                        )
                    updated = item.model_copy(
                        update={
                            "organization_id": actor.organization_id,
                            "workspace_id": actor.workspace_id,
                            "resource_ids": [resource_id],
                        }
                    )
                    self.work_items.put(updated.ref, updated)
                    result = operation.model_copy(
                        update={
                            "metadata": {
                                **operation.metadata,
                                "resource_id": resource_id,
                            }
                        }
                    )
                else:
                    raise CanonicalMaterializationError(
                        f"unsupported materialization apply kind: {operation.apply_kind}"
                    )
                checkpoint(operation, result)

            execution.status = MaterializationExecutionStatus.APPLIED
            execution.completed_at = self.clock()
            execution.updated_at = execution.completed_at
            execution.last_error = None
            execution.records = tuple(
                results[item.id]
                for item in plan.operations
            )
            return self._save_execution(execution)
        except Exception as exc:
            execution.status = MaterializationExecutionStatus.PARTIAL
            execution.last_error = str(exc)[:1000]
            execution.updated_at = self.clock()
            self._save_execution(execution)
            raise

    def status(
        self,
        project_id: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[CanonicalMaterializationExecution, ...]:
        return self.store.executions_for_project(
            project_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
