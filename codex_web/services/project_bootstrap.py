from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from codex_web.bootstrap_engine import (
    BootstrapAuditEvent,
    BootstrapCheck,
    BootstrapDisposition,
    BootstrapExecutionStatus,
    BootstrapOperation,
    BootstrapRollbackClass,
    ProjectBootstrapExecution,
    ProjectBootstrapPlan,
    ProjectBootstrapPreflight,
    stable_bootstrap_id,
    stable_payload_digest,
)
from codex_web.canonical_materialization import MaterializationDisposition
from codex_web.identity import AuthenticationActor, TenantScope
from codex_web.legacy_project_migration import MigrationDisposition
from codex_web.project_bootstrap import (
    ProjectBootstrapManifest,
    ProjectBootstrapManifestError,
    resolve_bootstrap_repository_paths,
)
from codex_web.resources import ResourceType
from codex_web.services.canonical_materialization import (
    CanonicalMaterializationService,
)
from codex_web.services.execution_workers import ExecutionWorkerService
from codex_web.services.identity import IdentityService
from codex_web.services.legacy_project_migration import (
    LegacyProjectMigrationService,
)
from codex_web.services.projects import ProjectService
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.secrets import SecretBroker
from codex_web.storage.project_bootstrap import ProjectBootstrapStore
from codex_web.storage.state_store import StateStore


class ProjectBootstrapError(RuntimeError):
    pass


class ProjectBootstrapBlocked(ProjectBootstrapError):
    pass


class ProjectBootstrapPlanStale(ProjectBootstrapError):
    pass


class ProjectBootstrapConcurrentApply(ProjectBootstrapError):
    pass


class ProjectBootstrapApprovalRequired(ProjectBootstrapError):
    pass


HealthProbe = Callable[
    [str, ProjectBootstrapManifest, AuthenticationActor],
    dict[str, Any],
]
ReadinessProbe = Callable[
    [str, AuthenticationActor],
    dict[str, Any],
]
AuthorizationCheck = Callable[[AuthenticationActor], None]


class ProjectBootstrapService:
    LEASE_SECONDS = 15 * 60.0

    def __init__(
        self,
        *,
        projects: ProjectService,
        resources: ResourceCatalogService,
        secrets: SecretBroker,
        workers: ExecutionWorkerService,
        state_store: StateStore,
        canonical_materialization: CanonicalMaterializationService,
        legacy_migration: LegacyProjectMigrationService,
        store: ProjectBootstrapStore,
        task_source_health: HealthProbe | None = None,
        environment_health: HealthProbe | None = None,
        readiness_probe: ReadinessProbe | None = None,
        authorization_check: AuthorizationCheck | None = None,
        operational_state_inspection: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.projects = projects
        self.resources = resources
        self.secrets = secrets
        self.workers = workers
        self.state_store = state_store
        self.canonical_materialization = canonical_materialization
        self.legacy_migration = legacy_migration
        self.store = store
        self.task_source_health = task_source_health
        self.environment_health = environment_health
        self.readiness_probe = readiness_probe
        self.authorization_check = (
            authorization_check or IdentityService.require_admin
        )
        self.operational_state_inspection = operational_state_inspection
        self.clock = clock

    def _authorize(
        self,
        actor: AuthenticationActor,
        manifest: ProjectBootstrapManifest | None = None,
    ) -> None:
        self.authorization_check(actor)
        if manifest is None:
            return
        desired = TenantScope(
            organization_id=manifest.project.organization,
            workspace_id=manifest.project.workspace,
        )
        if actor.tenant != desired:
            raise ProjectBootstrapBlocked(
                "manifest tenant does not match authenticated actor scope"
            )

    def _project(self, project_id: str):
        project = next(
            (
                item
                for item in self.projects.repository.load()
                if item.id == project_id
            ),
            None,
        )
        if project is None:
            raise ProjectBootstrapError("Project not found")
        return project

    @staticmethod
    def _check(
        check_id: str,
        domain: str,
        resource_ref: str,
        disposition: BootstrapDisposition,
        reason_code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        operator_action_required: bool = False,
    ) -> BootstrapCheck:
        return BootstrapCheck(
            id=check_id,
            domain=domain,
            resource_ref=resource_ref,
            disposition=disposition,
            reason_code=reason_code,
            message=message,
            details=details or {},
            operator_action_required=operator_action_required,
        )

    @staticmethod
    def _operation(
        operation_id: str,
        domain: str,
        resource_ref: str,
        disposition: BootstrapDisposition,
        reason_code: str,
        message: str,
        *,
        current: dict[str, Any] | None = None,
        desired: dict[str, Any] | None = None,
        dependencies: tuple[str, ...] = (),
        operator_action_required: bool = False,
        rollback: BootstrapRollbackClass = BootstrapRollbackClass.NOT_APPLICABLE,
        provider: str = "none",
        provider_operation_id: str | None = None,
    ) -> BootstrapOperation:
        return BootstrapOperation(
            id=operation_id,
            domain=domain,
            resource_ref=resource_ref,
            disposition=disposition,
            reason_code=reason_code,
            message=message,
            current=current or {},
            desired=desired or {},
            dependencies=dependencies,
            operator_action_required=operator_action_required,
            rollback=rollback,
            provider=provider,
            provider_operation_id=provider_operation_id,
        )

    @staticmethod
    def _canonical_disposition(item) -> BootstrapDisposition:
        if item.disposition == MaterializationDisposition.UNCHANGED:
            return BootstrapDisposition.READY
        if item.disposition == MaterializationDisposition.SKIPPED:
            return BootstrapDisposition.SKIP
        if item.disposition in {
            MaterializationDisposition.UNRESOLVED,
            MaterializationDisposition.OPERATOR_ACTION_REQUIRED,
        }:
            return BootstrapDisposition.BLOCKED
        if item.apply_kind == "repository_create":
            return BootstrapDisposition.CREATE
        return BootstrapDisposition.MIGRATE

    @staticmethod
    def _legacy_disposition(item) -> BootstrapDisposition:
        if item.disposition == MigrationDisposition.UNCHANGED:
            return BootstrapDisposition.READY
        if item.disposition == MigrationDisposition.BLOCKED:
            return BootstrapDisposition.BLOCKED
        if item.disposition == MigrationDisposition.APPROVAL_REQUIRED:
            return BootstrapDisposition.UPDATE
        return BootstrapDisposition.MIGRATE

    def _manifest_paths(
        self,
        manifest: ProjectBootstrapManifest,
    ) -> dict[str, Path]:
        try:
            return resolve_bootstrap_repository_paths(
                manifest,
                workspace_mapper=self.projects.repository.workspace_mapper,
            )
        except ProjectBootstrapManifestError as exc:
            raise ProjectBootstrapBlocked(str(exc)) from exc

    def _legacy_plan_if_available(
        self,
        project_id: str,
        *,
        actor: AuthenticationActor,
    ):
        project = self._project(project_id)
        if (
            project.organization_id != actor.organization_id
            or project.workspace_id != actor.workspace_id
        ):
            return None
        return self.legacy_migration.plan(project_id, actor=actor)

    def preflight(
        self,
        project_id: str,
        manifest: ProjectBootstrapManifest,
        *,
        actor: AuthenticationActor,
        migrate_legacy: bool = True,
    ) -> ProjectBootstrapPreflight:
        self._authorize(actor, manifest)
        project = self._project(project_id)
        checks: list[BootstrapCheck] = []

        status = dict(self.state_store.status())
        database_ok = bool(status.get("ok", True))
        schema = status.get("schemaVersion")
        supported_schema = status.get("supportedSchemaVersion", schema)
        schema_ok = (
            schema is None
            or supported_schema is None
            or int(schema) <= int(supported_schema)
        )
        checks.append(
            self._check(
                "database:state",
                "database",
                "state-store",
                (
                    BootstrapDisposition.READY
                    if database_ok and schema_ok
                    else BootstrapDisposition.BLOCKED
                ),
                (
                    "state_store_ready"
                    if database_ok and schema_ok
                    else "state_store_integrity_or_schema_invalid"
                ),
                (
                    "State store integrity and schema are supported."
                    if database_ok and schema_ok
                    else "State store integrity/schema must be repaired before bootstrap."
                ),
                details={
                    "backend": status.get("backend"),
                    "schema_version": schema,
                    "supported_schema_version": supported_schema,
                    "integrity": status.get("integrity"),
                    "document_count": status.get("documents"),
                },
            )
        )

        checks.append(
            self._check(
                "bootstrap:versions",
                "bootstrap_contract",
                project_id,
                BootstrapDisposition.READY,
                "bootstrap_versions_supported",
                "Manifest/bootstrap engine versions are supported.",
                details={
                    "manifest_api_version": manifest.api_version,
                    "bootstrap_engine_version": "1.0",
                    "canonical_materialization_version": "1.0",
                },
            )
        )

        source_scope = TenantScope(
            organization_id=project.organization_id,
            workspace_id=project.workspace_id,
        )
        desired_scope = actor.tenant
        if source_scope == desired_scope:
            scope_disposition = BootstrapDisposition.READY
            scope_reason = "project_scope_ready"
            scope_message = "Project already belongs to the requested tenant."
        elif source_scope == CanonicalMaterializationService.GENERIC_SCOPE:
            scope_disposition = (
                BootstrapDisposition.MIGRATE
                if migrate_legacy
                else BootstrapDisposition.BLOCKED
            )
            scope_reason = (
                "legacy_generic_scope_migratable"
                if migrate_legacy
                else "legacy_scope_requires_migration"
            )
            scope_message = (
                "Legacy Local / Default ownership can be rebound deterministically."
                if migrate_legacy
                else "Legacy Project ownership requires migration."
            )
        else:
            scope_disposition = BootstrapDisposition.BLOCKED
            scope_reason = "project_scope_conflict"
            scope_message = (
                "Project belongs to a different non-generic tenant and will not "
                "be reassigned automatically."
            )
        checks.append(
            self._check(
                "project:scope",
                "project_scope",
                project_id,
                scope_disposition,
                scope_reason,
                scope_message,
                details={
                    "current_organization_id": project.organization_id,
                    "current_workspace_id": project.workspace_id,
                    "desired_organization_id": actor.organization_id,
                    "desired_workspace_id": actor.workspace_id,
                },
                operator_action_required=(
                    scope_disposition == BootstrapDisposition.BLOCKED
                ),
            )
        )

        paths = self._manifest_paths(manifest)
        checks.append(
            self._check(
                "repositories:manifest-paths",
                "repositories",
                project_id,
                BootstrapDisposition.READY,
                "manifest_repository_paths_valid",
                "All manifest repositories resolve beneath the approved workspace root.",
                details={
                    "repository_count": len(paths),
                    "repository_ids": sorted(paths),
                },
            )
        )

        confirm_generic = desired_scope == CanonicalMaterializationService.GENERIC_SCOPE
        canonical_plan = self.canonical_materialization.plan(
            project_id,
            actor=actor,
            confirm_generic_target=confirm_generic,
            preferred_gitlab_secret_id=(
                manifest.task_source.secret_ref
                if manifest.task_source is not None
                and manifest.task_source.type.casefold() == "gitlab"
                else None
            ),
        )
        discovered_paths = {
            Path(str(item.metadata["filesystem_path"])).resolve()
            for item in canonical_plan.operations
            if item.domain == "repository_resource"
            and item.metadata.get("filesystem_path")
        }
        declared_paths = {item.resolve() for item in paths.values()}
        topology_ok = discovered_paths == declared_paths
        checks.append(
            self._check(
                "repositories:topology",
                "repositories",
                project_id,
                (
                    BootstrapDisposition.READY
                    if topology_ok
                    else BootstrapDisposition.BLOCKED
                ),
                (
                    "repository_topology_deterministic"
                    if topology_ok
                    else "repository_topology_mismatch"
                ),
                (
                    "Manifest repository topology matches deterministic discovery."
                    if topology_ok
                    else "Manifest repository topology differs from deterministic discovery."
                ),
                details={
                    "declared_count": len(declared_paths),
                    "discovered_count": len(discovered_paths),
                },
                operator_action_required=not topology_ok,
            )
        )

        selection = manifest.execution.repository_selection
        default_ids = [
            item.id for item in manifest.repositories if item.default
        ]
        selection_ready = bool(manifest.repositories)
        if selection == "single":
            selection_ready = len(manifest.repositories) == 1
        elif selection == "default":
            selection_ready = len(default_ids) == 1
        elif selection in {"explicit", "coordinated"}:
            # Multi-repository modes are deterministic because callers either
            # name a target or opt into the complete Project repository set;
            # bootstrap never guesses a default.
            selection_ready = bool(manifest.repositories)
        checks.append(
            self._check(
                "execution:repository-selection",
                "execution_target",
                project_id,
                (
                    BootstrapDisposition.READY
                    if selection_ready
                    else BootstrapDisposition.BLOCKED
                ),
                (
                    "execution_target_policy_deterministic"
                    if selection_ready
                    else "execution_target_policy_ambiguous"
                ),
                (
                    "Repository execution-target policy is deterministic."
                    if selection_ready
                    else "Repository execution-target policy is ambiguous."
                ),
                details={
                    "repository_selection": selection,
                    "repository_count": len(manifest.repositories),
                    "default_repository_ids": default_ids,
                },
                operator_action_required=not selection_ready,
            )
        )

        manifest_secret_ready = False
        if manifest.task_source and manifest.task_source.secret_ref:
            try:
                reference = self.secrets.metadata(
                    manifest.task_source.secret_ref,
                    actor=actor,
                    require_use=True,
                )
                secret_ok = reference.status().value == "active"
                manifest_secret_ready = secret_ok
            except Exception:
                secret_ok = False
            checks.append(
                self._check(
                    "task-source:secret-reference",
                    "secret_reference",
                    manifest.task_source.secret_ref,
                    (
                        BootstrapDisposition.READY
                        if secret_ok
                        else BootstrapDisposition.BLOCKED
                    ),
                    (
                        "secret_reference_ready"
                        if secret_ok
                        else "secret_reference_missing_or_unauthorized"
                    ),
                    (
                        "TaskSource SecretReference exists and is authorized."
                        if secret_ok
                        else "TaskSource SecretReference is missing, inactive, or unauthorized."
                    ),
                    details={"secret_reference_id": manifest.task_source.secret_ref},
                    operator_action_required=not secret_ok,
                )
            )

        if manifest.task_source is not None:
            provider_health = (
                self.task_source_health(project_id, manifest, actor)
                if self.task_source_health is not None
                else {"available": True, "code": "health_probe_not_configured"}
            )
            available = bool(provider_health.get("available", False))
            checks.append(
                self._check(
                    "task-source:reachability",
                    "task_source",
                    manifest.task_source.type,
                    (
                        BootstrapDisposition.READY
                        if available
                        else BootstrapDisposition.WARNING
                    ),
                    str(
                        provider_health.get("code")
                        or (
                            "task_source_reachable"
                            if available
                            else "task_source_temporarily_unreachable"
                        )
                    ),
                    (
                        "TaskSource health is available."
                        if available
                        else "TaskSource health is degraded; local bootstrap planning can continue."
                    ),
                    details={
                        "source_type": manifest.task_source.type,
                        "available": available,
                    },
                )
            )

        worker = self.workers.execution_readiness(
            required_capabilities=manifest.execution.required_capabilities,
            execution_contract_version="thread-turn/1.0",
            actor=actor,
        )
        checks.append(
            self._check(
                "execution:worker",
                "execution_worker",
                project_id,
                (
                    BootstrapDisposition.READY
                    if worker.ready
                    else BootstrapDisposition.BLOCKED
                ),
                worker.code,
                worker.reason,
                details={
                    "required_capabilities": [
                        item.value for item in worker.required_capabilities
                    ],
                    "available_capabilities": [
                        item.value for item in worker.available_capabilities
                    ],
                    "eligible_worker_ids": list(worker.eligible_worker_ids),
                },
                operator_action_required=not worker.ready,
            )
        )

        if self.environment_health is not None:
            environment = self.environment_health(project_id, manifest, actor)
            environment_ok = bool(environment.get("available", False))
            checks.append(
                self._check(
                    "execution:environment",
                    "execution_environment",
                    manifest.execution.sandbox,
                    (
                        BootstrapDisposition.READY
                        if environment_ok
                        else BootstrapDisposition.BLOCKED
                    ),
                    str(
                        environment.get("code")
                        or (
                            "execution_environment_ready"
                            if environment_ok
                            else "execution_environment_unsupported"
                        )
                    ),
                    str(
                        environment.get("reason")
                        or (
                            "Execution environment supports the requested sandbox."
                            if environment_ok
                            else "Execution environment does not support the requested sandbox."
                        )
                    ),
                    operator_action_required=not environment_ok,
                )
            )

        if self.operational_state_inspection is not None:
            operational = self.operational_state_inspection()
            for item in operational.stores:
                checks.append(
                    self._check(
                        f"operational-state:{item.store}",
                        "operational_state",
                        item.store,
                        (
                            BootstrapDisposition.WARNING
                            if item.warning
                            else BootstrapDisposition.READY
                        ),
                        item.reason_code,
                        (
                            "Operational state exceeds a bounded-inspection "
                            "threshold; review the explicit compaction workflow."
                            if item.warning
                            else "Operational state is within the configured threshold."
                        ),
                        details={
                            "count": item.count,
                            "count_exact": item.count_exact,
                            "bytes": item.bytes,
                            "oldest_at": item.oldest_at,
                            "newest_at": item.newest_at,
                            **item.details,
                        },
                    )
                )
        else:
            document_count = int(status.get("documents") or 0)
            checks.append(
                self._check(
                    "operational-state:size",
                    "operational_state",
                    "state-store",
                    (
                        BootstrapDisposition.WARNING
                        if document_count >= 50_000
                        else BootstrapDisposition.READY
                    ),
                    (
                        "large_state_requires_bounded_inspection"
                        if document_count >= 50_000
                        else "state_size_within_bootstrap_threshold"
                    ),
                    (
                        "Large canonical state detected; bounded inspection/compaction may be required."
                        if document_count >= 50_000
                        else "Canonical state size does not trigger the bootstrap warning threshold."
                    ),
                    details={"document_count": document_count},
                )
            )

        current_source = project.authoritative_task_source
        task_source_candidate, _task_source_candidate_error = (
            self.canonical_materialization.derived_gitlab_task_source(
                project_id
            )
        )
        explicit_gitlab_reference = bool(
            manifest.task_source is not None
            and manifest.task_source.type.casefold() == "gitlab"
            and manifest.task_source.secret_ref
            and manifest_secret_ready
            and (
                current_source is None
                or current_source.source_type.casefold() == "gitlab"
            )
            and (
                current_source is not None
                or task_source_candidate is not None
            )
        )
        superseded_legacy_reasons = {
            "gitlab_credential_missing",
            "task_source_credential_unresolved",
        }

        for blocker in canonical_plan.blockers:
            if (
                explicit_gitlab_reference
                and blocker.reason_code in superseded_legacy_reasons
            ):
                checks.append(
                    self._check(
                        f"canonical:{blocker.id}",
                        blocker.domain,
                        blocker.record_ref,
                        BootstrapDisposition.READY,
                        "manifest_secret_reference_supersedes_legacy_credential",
                        (
                            "Explicit canonical SecretReference satisfies the "
                            "credential requirement; legacy credential migration "
                            "is not required."
                        ),
                        details={
                            "secret_reference_id": (
                                manifest.task_source.secret_ref
                                if manifest.task_source is not None
                                else ""
                            )
                        },
                    )
                )
                continue
            checks.append(
                self._check(
                    f"canonical:{blocker.id}",
                    blocker.domain,
                    blocker.record_ref,
                    BootstrapDisposition.BLOCKED,
                    blocker.reason_code,
                    blocker.message,
                    operator_action_required=True,
                )
            )

        legacy_plan = (
            self._legacy_plan_if_available(project_id, actor=actor)
            if migrate_legacy
            else None
        )
        if legacy_plan is not None:
            for blocker in legacy_plan.blockers:
                checks.append(
                    self._check(
                        f"legacy:{blocker}",
                        "legacy_migration",
                        blocker,
                        BootstrapDisposition.BLOCKED,
                        "legacy_migration_blocked",
                        "Legacy Project/thread migration has an ambiguous mapping.",
                        operator_action_required=True,
                    )
                )

        return ProjectBootstrapPreflight(
            project_id=project_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            manifest_digest=manifest.digest(),
            checks=tuple(checks),
            generated_at=self.clock(),
        )

    def plan(
        self,
        project_id: str,
        manifest: ProjectBootstrapManifest,
        *,
        actor: AuthenticationActor,
        migrate_legacy: bool = True,
    ) -> ProjectBootstrapPlan:
        self._authorize(actor, manifest)
        project = self._project(project_id)
        preflight = self.preflight(
            project_id,
            manifest,
            actor=actor,
            migrate_legacy=migrate_legacy,
        )
        desired_scope = actor.tenant
        confirm_generic = desired_scope == CanonicalMaterializationService.GENERIC_SCOPE
        canonical = self.canonical_materialization.plan(
            project_id,
            actor=actor,
            confirm_generic_target=confirm_generic,
            preferred_gitlab_secret_id=(
                manifest.task_source.secret_ref
                if manifest.task_source is not None
                and manifest.task_source.type.casefold() == "gitlab"
                else None
            ),
        )
        legacy = (
            self._legacy_plan_if_available(project_id, actor=actor)
            if migrate_legacy
            else None
        )
        operations: list[BootstrapOperation] = []

        if project.name != manifest.project.name:
            operations.append(
                self._operation(
                    "project:settings:name",
                    "project",
                    project_id,
                    BootstrapDisposition.UPDATE,
                    "project_name_update",
                    "Project name will be reconciled to manifest desired state.",
                    current={"name": project.name},
                    desired={"name": manifest.project.name},
                    rollback=BootstrapRollbackClass.REVERSIBLE,
                    provider="bootstrap",
                )
            )
        else:
            operations.append(
                self._operation(
                    "project:settings:name",
                    "project",
                    project_id,
                    BootstrapDisposition.READY,
                    "project_name_ready",
                    "Project name matches manifest desired state.",
                    current={"name": project.name},
                    desired={"name": manifest.project.name},
                )
            )

        desired_repository_policy = (
            manifest.execution.repository_selection
            if manifest.execution.repository_selection
            in {"explicit", "coordinated"}
            else "deterministic"
        )
        operations.append(
            self._operation(
                "project:settings:repository-selection",
                "execution_policy",
                project_id,
                (
                    BootstrapDisposition.UPDATE
                    if project.repository_selection_policy
                    != desired_repository_policy
                    else BootstrapDisposition.READY
                ),
                (
                    "project_repository_selection_policy_update"
                    if project.repository_selection_policy
                    != desired_repository_policy
                    else "project_repository_selection_policy_ready"
                ),
                (
                    "Project repository-selection policy will be reconciled "
                    "to manifest desired state."
                    if project.repository_selection_policy
                    != desired_repository_policy
                    else (
                        "Project repository-selection policy matches manifest "
                        "desired state."
                    )
                ),
                current={
                    "repository_selection_policy": (
                        project.repository_selection_policy
                    )
                },
                desired={
                    "repository_selection_policy": desired_repository_policy
                },
                rollback=(
                    BootstrapRollbackClass.REVERSIBLE
                    if project.repository_selection_policy
                    != desired_repository_policy
                    else BootstrapRollbackClass.NOT_APPLICABLE
                ),
                provider=(
                    "bootstrap"
                    if project.repository_selection_policy
                    != desired_repository_policy
                    else "none"
                ),
            )
        )

        sandbox_approval = (
            project.sandbox != manifest.execution.sandbox
            and manifest.execution.sandbox == "danger-full-access"
        )
        operations.append(
            self._operation(
                "project:settings:sandbox",
                "execution_policy",
                project_id,
                (
                    BootstrapDisposition.UPDATE
                    if project.sandbox != manifest.execution.sandbox
                    else BootstrapDisposition.READY
                ),
                (
                    "project_sandbox_update"
                    if project.sandbox != manifest.execution.sandbox
                    else "project_sandbox_ready"
                ),
                (
                    "Project sandbox will be reconciled to manifest desired state."
                    if project.sandbox != manifest.execution.sandbox
                    else "Project sandbox matches manifest desired state."
                ),
                current={"sandbox": project.sandbox},
                desired={"sandbox": manifest.execution.sandbox},
                operator_action_required=sandbox_approval,
                rollback=(
                    BootstrapRollbackClass.REVERSIBLE
                    if project.sandbox != manifest.execution.sandbox
                    else BootstrapRollbackClass.NOT_APPLICABLE
                ),
                provider=(
                    "bootstrap"
                    if project.sandbox != manifest.execution.sandbox
                    else "none"
                ),
            )
        )

        current_source = project.authoritative_task_source
        task_source_candidate, task_source_candidate_error = (
            self.canonical_materialization.derived_gitlab_task_source(
                project_id
            )
        )
        explicit_gitlab_reference = bool(
            manifest.task_source is not None
            and manifest.task_source.type.casefold() == "gitlab"
            and manifest.task_source.secret_ref
            and (
                current_source is None
                or current_source.source_type.casefold() == "gitlab"
            )
            and (
                current_source is not None
                or task_source_candidate is not None
            )
        )

        for item in canonical.operations:
            disposition = self._canonical_disposition(item)
            operations.append(
                self._operation(
                    f"canonical:{item.id}",
                    item.domain,
                    item.record_ref,
                    disposition,
                    item.reason_code,
                    item.message,
                    dependencies=tuple(
                        f"canonical:{value}" for value in item.dependencies
                    ),
                    operator_action_required=(
                        item.disposition
                        == MaterializationDisposition.OPERATOR_ACTION_REQUIRED
                    ),
                    rollback=(
                        BootstrapRollbackClass.COMPENSATING
                        if item.apply_kind is not None
                        else BootstrapRollbackClass.NOT_APPLICABLE
                    ),
                    provider="canonical-materialization",
                    provider_operation_id=item.id,
                )
            )

        effective_canonical = canonical

        if manifest.task_source is not None:
            desired_type = manifest.task_source.type.casefold()
            desired_secret = manifest.task_source.secret_ref
            if (
                current_source is not None
                and current_source.source_type.casefold() != desired_type
            ):
                operations.append(
                    self._operation(
                        "task-source:manifest",
                        "task_source",
                        project_id,
                        BootstrapDisposition.BLOCKED,
                        "task_source_provider_conflict",
                        (
                            "Manifest TaskSource provider conflicts with the "
                            "existing authoritative provider."
                        ),
                        current={
                            "source_type": current_source.source_type,
                            "source_instance": current_source.source_instance,
                            "scope": current_source.scope,
                            "secret_reference_id": (
                                current_source.credential_secret_id or ""
                            ),
                        },
                        desired={
                            "source_type": manifest.task_source.type,
                            "secret_reference_id": desired_secret or "",
                        },
                        operator_action_required=True,
                    )
                )
            elif current_source is not None:
                secret_matches = (
                    not desired_secret
                    or current_source.credential_secret_id == desired_secret
                )
                operations.append(
                    self._operation(
                        "task-source:manifest",
                        "task_source",
                        project_id,
                        (
                            BootstrapDisposition.READY
                            if secret_matches
                            else BootstrapDisposition.UPDATE
                        ),
                        (
                            "task_source_manifest_ready"
                            if secret_matches
                            else "task_source_secret_reference_update"
                        ),
                        (
                            "Authoritative TaskSource matches manifest intent."
                            if secret_matches
                            else (
                                "Authoritative TaskSource will retain its "
                                "provider/scope and use the manifest SecretReference."
                            )
                        ),
                        current={
                            "source_type": current_source.source_type,
                            "source_instance": current_source.source_instance,
                            "scope": current_source.scope,
                            "secret_reference_id": (
                                current_source.credential_secret_id or ""
                            ),
                        },
                        desired={
                            "source_type": manifest.task_source.type,
                            "source_instance": current_source.source_instance,
                            "scope": current_source.scope,
                            "secret_reference_id": desired_secret or "",
                        },
                        rollback=(
                            BootstrapRollbackClass.REVERSIBLE
                            if not secret_matches
                            else BootstrapRollbackClass.NOT_APPLICABLE
                        ),
                        provider=(
                            "bootstrap"
                            if not secret_matches
                            else "none"
                        ),
                    )
                )
            elif (
                desired_type == "gitlab"
                and desired_secret
                and task_source_candidate is not None
            ):
                delegated = next(
                    (
                        item
                        for item in canonical.operations
                        if item.domain == "task_source"
                        and item.apply_kind == "gitlab_task_source"
                    ),
                    None,
                )
                operations.append(
                    self._operation(
                        "task-source:manifest",
                        "task_source",
                        project_id,
                        (
                            BootstrapDisposition.READY
                            if delegated is not None
                            else BootstrapDisposition.BLOCKED
                        ),
                        (
                            "task_source_materialization_delegated"
                            if delegated is not None
                            else "task_source_binding_missing"
                        ),
                        (
                            "TaskSource desired state is handled by canonical "
                            "materialization using the explicit SecretReference."
                            if delegated is not None
                            else "TaskSource binding cannot be materialized safely."
                        ),
                        desired={
                            "source_type": task_source_candidate.source_type,
                            "source_instance": task_source_candidate.source_instance,
                            "scope": task_source_candidate.scope,
                            "secret_reference_id": desired_secret,
                        },
                        operator_action_required=(delegated is None),
                    )
                )
            else:
                delegated = next(
                    (
                        item
                        for item in canonical.operations
                        if item.domain == "task_source"
                        and item.apply_kind is not None
                    ),
                    None,
                )
                if delegated is not None and not desired_secret:
                    operations.append(
                        self._operation(
                            "task-source:manifest",
                            "task_source",
                            project_id,
                            BootstrapDisposition.READY,
                            "task_source_materialization_delegated",
                            (
                                "TaskSource desired state is deterministically "
                                "handled by canonical materialization."
                            ),
                            provider="none",
                        )
                    )
                else:
                    operations.append(
                        self._operation(
                            "task-source:manifest",
                            "task_source",
                            project_id,
                            BootstrapDisposition.BLOCKED,
                            (
                                task_source_candidate_error
                                or "task_source_binding_missing"
                            ),
                            (
                                "TaskSource provider/scope cannot be derived "
                                "safely from current state and manifest intent."
                            ),
                            desired={
                                "source_type": manifest.task_source.type,
                                "secret_reference_id": desired_secret or "",
                            },
                            operator_action_required=True,
                        )
                    )

        if migrate_legacy:
            if legacy is None:
                operations.append(
                    self._operation(
                        "legacy:deferred-after-scope",
                        "legacy_migration",
                        project_id,
                        BootstrapDisposition.MIGRATE,
                        "legacy_migration_deferred_until_scope_materialized",
                        "Legacy thread/profile migration will be planned after canonical tenant materialization.",
                        dependencies=("canonical:project:scope",),
                        rollback=BootstrapRollbackClass.COMPENSATING,
                        provider="legacy-project-migration",
                    )
                )
            else:
                for repository in legacy.repositories:
                    operations.append(
                        self._operation(
                            f"legacy:repository:{repository.key}",
                            "legacy_repository",
                            repository.absolute_path,
                            (
                                BootstrapDisposition.READY
                                if repository.existing_resource_id
                                else BootstrapDisposition.MIGRATE
                            ),
                            (
                                "legacy_repository_ready"
                                if repository.existing_resource_id
                                else "legacy_repository_materialize"
                            ),
                            (
                                "Legacy repository already has a canonical Resource."
                                if repository.existing_resource_id
                                else "Legacy repository will be materialized/bound."
                            ),
                            rollback=(
                                BootstrapRollbackClass.NOT_APPLICABLE
                                if repository.existing_resource_id
                                else BootstrapRollbackClass.COMPENSATING
                            ),
                            provider="legacy-project-migration",
                            provider_operation_id=f"repository:{repository.key}",
                        )
                    )
                for thread in legacy.threads:
                    disposition = self._legacy_disposition(thread)
                    operations.append(
                        self._operation(
                            f"legacy:thread:{thread.thread_id}",
                            "legacy_thread",
                            thread.thread_id,
                            disposition,
                            thread.reason_code,
                            thread.reason,
                            operator_action_required=(
                                thread.disposition
                                == MigrationDisposition.APPROVAL_REQUIRED
                            ),
                            rollback=(
                                BootstrapRollbackClass.COMPENSATING
                                if disposition
                                in {
                                    BootstrapDisposition.MIGRATE,
                                    BootstrapDisposition.UPDATE,
                                }
                                else BootstrapRollbackClass.NOT_APPLICABLE
                            ),
                            provider="legacy-project-migration",
                            provider_operation_id=f"thread:{thread.thread_id}",
                        )
                    )

        if manifest.integrations.slack is not None:
            operations.append(
                self._operation(
                    "integration:slack",
                    "integration",
                    "slack",
                    BootstrapDisposition.SKIP,
                    "slack_reconciliation_delegated",
                    "Slack connection/backfill desired state is retained for the dedicated integration reconciliation lifecycle.",
                    desired={
                        "connection_ref": manifest.integrations.slack.connection_ref,
                        "backfill_enabled": manifest.integrations.slack.backfill.enabled,
                    },
                )
            )

        for check in preflight.checks:
            if check.disposition not in {
                BootstrapDisposition.WARNING,
                BootstrapDisposition.BLOCKED,
            }:
                continue
            existing = {
                (item.disposition, item.reason_code)
                for item in operations
            }
            key = (check.disposition, check.reason_code)
            if key in existing:
                continue
            operations.append(
                self._operation(
                    f"preflight:{check.id}",
                    check.domain,
                    check.resource_ref,
                    check.disposition,
                    check.reason_code,
                    check.message,
                    current=check.details,
                    operator_action_required=check.operator_action_required,
                )
            )

        snapshot = {
            "manifest_digest": manifest.digest(),
            "project": {
                "id": project.id,
                "organization_id": project.organization_id,
                "workspace_id": project.workspace_id,
                "name": project.name,
                "sandbox": project.sandbox,
                "repository_selection_policy": (
                    project.repository_selection_policy
                ),
                "task_source": (
                    project.authoritative_task_source.model_dump(mode="json")
                    if project.authoritative_task_source
                    else None
                ),
            },
            "canonical_plan_id": canonical.id,
            "legacy_plan_id": legacy.id if legacy is not None else None,
            "preflight": [
                {
                    "id": item.id,
                    "disposition": item.disposition.value,
                    "reason_code": item.reason_code,
                }
                for item in preflight.checks
            ],
            "operations": [
                {
                    "id": item.id,
                    "disposition": item.disposition.value,
                    "reason_code": item.reason_code,
                    "dependencies": item.dependencies,
                    "operator_action_required": item.operator_action_required,
                    "provider_operation_id": item.provider_operation_id,
                }
                for item in operations
            ],
        }
        snapshot_digest = stable_payload_digest(snapshot)
        plan_id = stable_bootstrap_id(
            "bootstrap-plan",
            {
                "project_id": project_id,
                "organization_id": actor.organization_id,
                "workspace_id": actor.workspace_id,
                "snapshot_digest": snapshot_digest,
                "migrate_legacy": migrate_legacy,
            },
        )
        return ProjectBootstrapPlan(
            id=plan_id,
            project_id=project_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            manifest=manifest,
            manifest_digest=manifest.digest(),
            snapshot_digest=snapshot_digest,
            migrate_legacy=migrate_legacy,
            preflight=preflight,
            operations=tuple(operations),
            canonical_materialization_plan=effective_canonical,
            legacy_migration_plan=legacy,
            generated_at=self.clock(),
        )

    def _persist(
        self,
        execution: ProjectBootstrapExecution,
        *,
        expected_lease_owner: str | None = None,
    ) -> ProjectBootstrapExecution:
        saved: list[ProjectBootstrapExecution] = []

        def mutate(state):
            if expected_lease_owner is not None:
                current = next(
                    (
                        item
                        for item in state.executions
                        if item.id == execution.id
                    ),
                    None,
                )
                if (
                    current is None
                    or current.lease_owner != expected_lease_owner
                ):
                    raise ProjectBootstrapConcurrentApply(
                        "bootstrap apply lease was lost to another executor"
                    )
            state.executions = [
                item
                for item in state.executions
                if item.id != execution.id
            ]
            state.executions.append(execution)
            saved.append(execution)
            return state

        self.store.update(mutate)
        return saved[0]

    def _acquire(
        self,
        plan: ProjectBootstrapPlan,
        *,
        actor: AuthenticationActor,
    ) -> ProjectBootstrapExecution:
        now = self.clock()
        lease_owner = f"{actor.identity_id}:{uuid.uuid4().hex}"
        result: list[ProjectBootstrapExecution] = []

        def mutate(state):
            existing = next(
                (
                    item
                    for item in state.executions
                    if item.plan_id == plan.id
                    and item.organization_id == actor.organization_id
                    and item.workspace_id == actor.workspace_id
                ),
                None,
            )
            for item in state.executions:
                if (
                    item.project_id != plan.project_id
                    or item.organization_id != actor.organization_id
                    or item.workspace_id != actor.workspace_id
                    or item.status != BootstrapExecutionStatus.APPLYING
                    or item.lease_expires_at is None
                    or item.lease_expires_at <= now
                ):
                    continue
                if existing is None or item.id != existing.id:
                    raise ProjectBootstrapConcurrentApply(
                        "another bootstrap apply is active for this Project"
                    )
                if item.lease_owner:
                    raise ProjectBootstrapConcurrentApply(
                        "bootstrap execution already holds an active apply lease"
                    )

            if existing is not None and existing.status == BootstrapExecutionStatus.APPLIED:
                result.append(existing)
                return state

            execution = existing or ProjectBootstrapExecution(
                id=stable_bootstrap_id(
                    "bootstrap-execution",
                    {
                        "plan_id": plan.id,
                        "organization_id": actor.organization_id,
                        "workspace_id": actor.workspace_id,
                    },
                ),
                plan_id=plan.id,
                project_id=plan.project_id,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                manifest_digest=plan.manifest_digest,
                plan=plan,
                created_at=now,
                updated_at=now,
            )
            execution.status = BootstrapExecutionStatus.APPLYING
            execution.lease_owner = lease_owner
            execution.lease_expires_at = now + self.LEASE_SECONDS
            execution.updated_at = now
            state.executions = [
                item for item in state.executions if item.id != execution.id
            ]
            state.executions.append(execution)
            result.append(execution)
            return state

        self.store.update(mutate)
        return result[0]

    def _checkpoint(
        self,
        execution: ProjectBootstrapExecution,
        operation: BootstrapOperation,
        *,
        actor: AuthenticationActor,
        provider_execution_id: str | None = None,
        fail_after_operations: int | None = None,
        applied_this_run: list[int],
        lease_owner: str,
    ) -> ProjectBootstrapExecution:
        if operation.id in execution.completed_operation_ids:
            return execution
        self._authorize(actor, execution.plan.manifest)
        completed = tuple(
            sorted({*execution.completed_operation_ids, operation.id})
        )
        audit = BootstrapAuditEvent(
            id=stable_bootstrap_id(
                "bootstrap-audit",
                {
                    "execution_id": execution.id,
                    "operation_id": operation.id,
                    "outcome": "applied",
                },
            ),
            execution_id=execution.id,
            operation_id=operation.id,
            project_id=execution.project_id,
            organization_id=execution.organization_id,
            workspace_id=execution.workspace_id,
            actor_identity_id=actor.identity_id,
            outcome="applied",
            provider_execution_id=provider_execution_id,
            recorded_at=self.clock(),
        )
        audit_by_id = {
            item.id: item for item in (*execution.audit_events, audit)
        }
        execution.completed_operation_ids = completed
        execution.audit_events = tuple(
            audit_by_id[key] for key in sorted(audit_by_id)
        )
        execution.updated_at = self.clock()
        execution.lease_expires_at = self.clock() + self.LEASE_SECONDS
        execution = self._persist(
            execution,
            expected_lease_owner=lease_owner,
        )
        applied_this_run[0] += 1
        if (
            fail_after_operations is not None
            and applied_this_run[0] >= fail_after_operations
        ):
            raise ProjectBootstrapError(
                "injected bootstrap interruption after operation checkpoint"
            )
        return execution

    def _update_project_settings(
        self,
        plan: ProjectBootstrapPlan,
        *,
        actor: AuthenticationActor,
    ) -> None:
        self._authorize(actor, plan.manifest)
        projects = self.projects.repository.load()
        changed = False
        updated = []
        for project in projects:
            if project.id != plan.project_id:
                updated.append(project)
                continue
            if (
                project.organization_id != actor.organization_id
                or project.workspace_id != actor.workspace_id
            ):
                raise ProjectBootstrapBlocked(
                    "Project scope must be materialized before Project settings"
                )
            desired = plan.manifest
            source = project.authoritative_task_source
            if desired.task_source is not None:
                desired_secret = desired.task_source.secret_ref
                if source is None:
                    candidate, error = (
                        self.canonical_materialization.derived_gitlab_task_source(
                            plan.project_id
                        )
                    )
                    if candidate is None:
                        raise ProjectBootstrapBlocked(
                            error or "TaskSource cannot be derived safely"
                        )
                    source = candidate.model_copy(
                        update={
                            "credential_secret_id": desired_secret,
                        }
                    )
                elif (
                    source.source_type.casefold()
                    != desired.task_source.type.casefold()
                ):
                    raise ProjectBootstrapBlocked(
                        "authoritative TaskSource provider changed during apply"
                    )
                elif desired_secret:
                    source = source.model_copy(
                        update={
                            "credential_secret_id": desired_secret,
                        }
                    )
            replacement = project.model_copy(
                update={
                    "name": desired.project.name,
                    "sandbox": desired.execution.sandbox,
                    "repository_selection_policy": (
                        desired.execution.repository_selection
                        if desired.execution.repository_selection
                        in {"explicit", "coordinated"}
                        else "deterministic"
                    ),
                    "authoritative_task_source": source,
                }
            )
            changed = changed or replacement != project
            updated.append(replacement)
        if changed:
            self.projects.repository.save(updated)

    def apply(
        self,
        plan: ProjectBootstrapPlan,
        *,
        actor: AuthenticationActor,
        approve_authority_changes: bool = False,
        fail_after_operations: int | None = None,
    ) -> ProjectBootstrapExecution:
        self._authorize(actor, plan.manifest)
        if (
            plan.organization_id != actor.organization_id
            or plan.workspace_id != actor.workspace_id
        ):
            raise ProjectBootstrapError("bootstrap plan belongs to another tenant")

        project_executions = self.store.executions_for_project(
            plan.project_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        now = self.clock()
        active_other = next(
            (
                item
                for item in project_executions
                if item.status == BootstrapExecutionStatus.APPLYING
                and item.lease_owner
                and item.lease_expires_at is not None
                and item.lease_expires_at > now
            ),
            None,
        )
        if active_other is not None:
            raise ProjectBootstrapConcurrentApply(
                "another bootstrap apply is active for this Project"
            )

        existing = next(
            (
                item
                for item in project_executions
                if item.plan_id == plan.id
            ),
            None,
        )
        if existing is None:
            current = self.plan(
                plan.project_id,
                plan.manifest,
                actor=actor,
                migrate_legacy=plan.migrate_legacy,
            )
            if current.id != plan.id:
                raise ProjectBootstrapPlanStale(
                    "bootstrap plan is stale; run preflight/plan again"
                )
            if plan.blockers:
                raise ProjectBootstrapBlocked(
                    "bootstrap plan contains blocked operations"
                )
            if (
                any(
                    item.applicable and item.operator_action_required
                    for item in plan.operations
                )
                and not approve_authority_changes
            ):
                raise ProjectBootstrapApprovalRequired(
                    "bootstrap plan contains material authority changes"
                )

        execution = self._acquire(plan, actor=actor)
        if execution.status == BootstrapExecutionStatus.APPLIED:
            return execution
        lease_owner = str(execution.lease_owner or "")
        if not lease_owner:
            raise ProjectBootstrapConcurrentApply(
                "bootstrap execution has no active apply lease"
            )

        applied_this_run = [0]
        try:
            self._authorize(actor, plan.manifest)
            canonical_ops = [
                item
                for item in plan.operations
                if item.provider == "canonical-materialization"
                and item.applicable
                and item.id not in execution.completed_operation_ids
            ]
            if canonical_ops:
                provider_plan = plan.canonical_materialization_plan
                if provider_plan is None:
                    raise ProjectBootstrapBlocked(
                        "canonical materialization plan is unavailable"
                    )
                provider_execution = self.canonical_materialization.apply(
                    provider_plan,
                    actor=actor,
                )
                for operation in canonical_ops:
                    execution = self._checkpoint(
                        execution,
                        operation,
                        actor=actor,
                        provider_execution_id=provider_execution.id,
                        fail_after_operations=fail_after_operations,
                        applied_this_run=applied_this_run,
                        lease_owner=lease_owner,
                    )

            direct_ops = [
                item
                for item in plan.operations
                if item.provider == "bootstrap"
                and item.applicable
                and item.id not in execution.completed_operation_ids
            ]
            if direct_ops:
                self._update_project_settings(plan, actor=actor)
                for operation in direct_ops:
                    execution = self._checkpoint(
                        execution,
                        operation,
                        actor=actor,
                        fail_after_operations=fail_after_operations,
                        applied_this_run=applied_this_run,
                        lease_owner=lease_owner,
                    )

            legacy_ops = [
                item
                for item in plan.operations
                if item.provider == "legacy-project-migration"
                and item.applicable
                and item.id not in execution.completed_operation_ids
            ]
            if legacy_ops:
                self._authorize(actor, plan.manifest)
                provider_plan = plan.legacy_migration_plan
                if provider_plan is None:
                    provider_plan = self.legacy_migration.plan(
                        plan.project_id,
                        actor=actor,
                    )
                if provider_plan.blockers:
                    raise ProjectBootstrapBlocked(
                        "legacy migration contains blocked mappings"
                    )
                provider_execution = self.legacy_migration.apply(
                    provider_plan,
                    actor=actor,
                    approve_material_authority_changes=approve_authority_changes,
                )
                for operation in legacy_ops:
                    execution = self._checkpoint(
                        execution,
                        operation,
                        actor=actor,
                        provider_execution_id=provider_execution.id,
                        fail_after_operations=fail_after_operations,
                        applied_this_run=applied_this_run,
                        lease_owner=lease_owner,
                    )

            warnings = tuple(
                item.reason_code
                for item in plan.operations
                if item.disposition == BootstrapDisposition.WARNING
            )
            readiness = (
                self.readiness_probe(plan.project_id, actor)
                if self.readiness_probe is not None
                else {
                    "available": False,
                    "code": "project_readiness_not_installed",
                }
            )
            execution.status = BootstrapExecutionStatus.APPLIED
            execution.warnings = warnings
            execution.readiness = readiness
            execution.last_error_code = None
            execution.completed_at = self.clock()
            execution.updated_at = execution.completed_at
            execution.lease_owner = None
            execution.lease_expires_at = None
            return self._persist(
                execution,
                expected_lease_owner=lease_owner,
            )
        except Exception as exc:
            execution.status = BootstrapExecutionStatus.PARTIAL
            execution.last_error_code = type(exc).__name__
            execution.updated_at = self.clock()
            execution.lease_owner = None
            execution.lease_expires_at = None
            try:
                self._persist(
                    execution,
                    expected_lease_owner=lease_owner,
                )
            except ProjectBootstrapConcurrentApply:
                # A replacement executor owns the lease/state now. Never let a
                # stale worker overwrite its progress while unwinding.
                pass
            raise

    def status(
        self,
        project_id: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[ProjectBootstrapExecution, ...]:
        self._authorize(actor)
        return self.store.executions_for_project(
            project_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )

    @staticmethod
    def human_report(
        value: ProjectBootstrapPreflight | ProjectBootstrapPlan | ProjectBootstrapExecution,
    ) -> str:
        lines = [
            f"Project bootstrap {getattr(value, 'version', '1.0')} "
            f"for Project {value.project_id}"
        ]
        if isinstance(value, ProjectBootstrapPreflight):
            rows = value.checks
        else:
            plan = value.plan if isinstance(value, ProjectBootstrapExecution) else value
            rows = plan.operations
        for item in rows:
            lines.append(
                f"- [{item.disposition.value}] {item.domain} "
                f"{item.resource_ref}: {item.reason_code} — {item.message}"
            )
        return "\n".join(lines)
