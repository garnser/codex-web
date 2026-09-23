from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from codex_web.bootstrap_engine import (
    BootstrapDisposition,
    BootstrapExecutionStatus,
)
from codex_web.configuration import ConfigurationContext, SecretReference as ConfigurationSecretReference
from codex_web.execution_workers import (
    CodexExecutionAuthenticationMode,
    ExecutionRuntimeBinding,
    WorkerCapability,
)
from codex_web.identity import AuthenticationActor
from codex_web.project_readiness import (
    ProjectReadinessCheck,
    ProjectReadinessSnapshot,
    ReadinessCheckStatus,
)
from codex_web.resources import ResourceType
from codex_web.runtime_credentials import (
    DEFAULT_RUNTIME_CREDENTIAL_CONFIGS,
    RuntimeAuthenticationConfigurationError,
    RuntimeAuthenticationStatus,
    runtime_authentication_preflight,
)
from codex_web.services.configuration import ConfigurationError, ConfigurationNotFoundError, ConfigurationService
from codex_web.services.execution_workers import ExecutionWorkerService
from codex_web.services.projects import ProjectService
from codex_web.services.resources import (
    RepositoryTargetSelectionError,
    ResourceCatalogService,
)
from codex_web.services.secrets import SecretBroker
from codex_web.storage.project_bootstrap import ProjectBootstrapStore
from codex_web.storage.project_readiness import ProjectReadinessStore


EnvironmentProbe = Callable[[Any, AuthenticationActor], dict[str, Any]]
WorkItemLoader = Callable[[], dict[str, Any]]


class ProjectReadinessService:
    """Compute canonical Project semantic and execution readiness."""

    def __init__(
        self,
        *,
        projects: ProjectService,
        resources: ResourceCatalogService,
        secrets: SecretBroker,
        workers: ExecutionWorkerService,
        bootstrap: ProjectBootstrapStore,
        store: ProjectReadinessStore,
        load_work_items: WorkItemLoader,
        environment_probe: EnvironmentProbe | None = None,
        configuration: ConfigurationService | None = None,
        runtime_binding: ExecutionRuntimeBinding | None = None,
        runtime_credential_configs: Mapping[tuple[str, str], str] | None = None,
        permitted_codex_authentication_modes: tuple[CodexExecutionAuthenticationMode, ...] | None = None,
        local_session_probe: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.projects = projects
        self.resources = resources
        self.secrets = secrets
        self.workers = workers
        self.bootstrap = bootstrap
        self.store = store
        self.load_work_items = load_work_items
        self.environment_probe = environment_probe
        self.configuration = configuration
        self.runtime_binding = runtime_binding
        self.runtime_credential_configs = dict(
            runtime_credential_configs or DEFAULT_RUNTIME_CREDENTIAL_CONFIGS
        )
        self.permitted_codex_authentication_modes = permitted_codex_authentication_modes
        self.local_session_probe = local_session_probe
        self.clock = clock

    @staticmethod
    def _check(
        check_id: str,
        domain: str,
        status: ReadinessCheckStatus,
        code: str,
        message: str,
        *,
        affected_type: str | None = None,
        affected_id: str | None = None,
        remediation: str | None = None,
        remediation_route: str | None = None,
        required: bool = True,
        details: dict[str, Any] | None = None,
    ) -> ProjectReadinessCheck:
        return ProjectReadinessCheck(
            id=check_id,
            domain=domain,
            status=status,
            code=code,
            message=message,
            affected_type=affected_type,
            affected_id=affected_id,
            remediation=remediation,
            remediation_route=remediation_route,
            required=required,
            details=details or {},
        )

    def evaluate(
        self,
        project_id: str,
        *,
        actor: AuthenticationActor,
        record: bool = False,
    ) -> ProjectReadinessSnapshot:
        project = self.projects.get(project_id, actor.tenant)
        checks: list[ProjectReadinessCheck] = []

        checks.append(
            self._check(
                "project:scope",
                "project_scope",
                ReadinessCheckStatus.READY,
                "project_scope_ready",
                "Project ownership matches the active Organization/Workspace.",
                affected_type="project",
                affected_id=project.id,
            )
        )

        bootstrap_rows = self.bootstrap.executions_for_project(
            project.id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        latest_bootstrap = bootstrap_rows[0] if bootstrap_rows else None
        bootstrap_version = (
            latest_bootstrap.plan.version
            if latest_bootstrap is not None
            else None
        )
        bootstrap_execution_id = (
            latest_bootstrap.id
            if latest_bootstrap is not None
            else None
        )
        if latest_bootstrap is None:
            checks.append(
                self._check(
                    "bootstrap:status",
                    "bootstrap",
                    ReadinessCheckStatus.NOT_APPLICABLE,
                    "bootstrap_not_required_or_not_run",
                    (
                        "No ProjectBootstrap execution is recorded. Existing "
                        "canonical Projects are evaluated from canonical state."
                    ),
                    affected_type="project",
                    affected_id=project.id,
                    required=False,
                    remediation_route=(
                        f"/api/projects/{project.id}/bootstrap/status"
                    ),
                )
            )
        elif latest_bootstrap.status != BootstrapExecutionStatus.APPLIED:
            checks.append(
                self._check(
                    "bootstrap:status",
                    "bootstrap",
                    ReadinessCheckStatus.BLOCKED,
                    "bootstrap_incomplete",
                    (
                        "Project bootstrap is not complete: "
                        f"{latest_bootstrap.status.value}."
                    ),
                    affected_type="bootstrap_execution",
                    affected_id=latest_bootstrap.id,
                    remediation=(
                        "Resume or reconcile the Project bootstrap before "
                        "attempting execution."
                    ),
                    remediation_route=(
                        f"/api/projects/{project.id}/bootstrap/status"
                    ),
                    details={
                        "bootstrap_status": latest_bootstrap.status.value,
                        "last_error_code": latest_bootstrap.last_error_code,
                    },
                )
            )
        else:
            blocked_plan_ops = [
                item
                for item in latest_bootstrap.plan.operations
                if item.disposition == BootstrapDisposition.BLOCKED
            ]
            checks.append(
                self._check(
                    "bootstrap:status",
                    "bootstrap",
                    (
                        ReadinessCheckStatus.BLOCKED
                        if blocked_plan_ops
                        else ReadinessCheckStatus.READY
                    ),
                    (
                        "bootstrap_unresolved_blockers"
                        if blocked_plan_ops
                        else "bootstrap_applied"
                    ),
                    (
                        "Applied bootstrap still contains unresolved blockers."
                        if blocked_plan_ops
                        else "Project bootstrap completed successfully."
                    ),
                    affected_type="bootstrap_execution",
                    affected_id=latest_bootstrap.id,
                    remediation=(
                        "Re-run bootstrap planning and resolve blocked operations."
                        if blocked_plan_ops
                        else None
                    ),
                    remediation_route=(
                        f"/api/projects/{project.id}/bootstrap/status"
                    ),
                    details={
                        "blocked_operation_ids": [
                            item.id for item in blocked_plan_ops[:20]
                        ],
                        "blocked_operation_count": len(blocked_plan_ops),
                    },
                )
            )

        bound = self.resources.project_resources(project, actor=actor)
        repositories = [
            item
            for item in bound
            if item.resource_type == ResourceType.REPOSITORY
            and str(getattr(item.lifecycle, "value", item.lifecycle))
            == "active"
        ]
        if repositories:
            checks.append(
                self._check(
                    "repository:resources",
                    "repository",
                    ReadinessCheckStatus.READY,
                    "repository_resources_ready",
                    "Project has active canonical repository Resources.",
                    affected_type="project",
                    affected_id=project.id,
                    remediation_route=(
                        f"/api/projects/{project.id}/resources"
                    ),
                    details={
                        "repository_count": len(repositories),
                        "repository_ids": [
                            item.id for item in repositories[:20]
                        ],
                    },
                )
            )
        else:
            checks.append(
                self._check(
                    "repository:resources",
                    "repository",
                    ReadinessCheckStatus.BLOCKED,
                    "repository_resource_missing",
                    (
                        "Project has no active canonical repository Resource "
                        "for repository execution."
                    ),
                    affected_type="project",
                    affected_id=project.id,
                    remediation=(
                        "Bootstrap or bind the intended repository Resource."
                    ),
                    remediation_route=(
                        f"/api/projects/{project.id}/resources"
                    ),
                )
            )

        if project.repository_selection_policy in {"explicit", "coordinated"}:
            coordinated = project.repository_selection_policy == "coordinated"
            checks.append(
                self._check(
                    "repository:execution-target",
                    "execution_target",
                    (
                        ReadinessCheckStatus.READY
                        if repositories
                        else ReadinessCheckStatus.BLOCKED
                    ),
                    (
                        (
                            "repository_coordinated_scope_ready"
                            if coordinated
                            else "repository_target_required_per_turn"
                        )
                        if repositories
                        else "repository_target_missing"
                    ),
                    (
                        (
                            "Project repository policy fixes every active bound "
                            "repository as the coordinated writable scope."
                            if coordinated
                            else "Project repository policy requires each executable "
                            "turn to provide a contextual repository target."
                        )
                        if repositories
                        else (
                            "Project has no active canonical repository "
                            "Resource for repository execution."
                        )
                    ),
                    affected_type="project",
                    affected_id=project.id,
                    remediation=(
                        None
                        if repositories
                        else "Bootstrap or bind at least one repository Resource."
                    ),
                    remediation_route=(
                        f"/api/projects/{project.id}/resources"
                    ),
                    details={
                        "policy": project.repository_selection_policy,
                        "target_resolution": (
                            "project_repository_set"
                            if coordinated
                            else "required_per_turn"
                        ),
                        "repository_count": len(repositories),
                        "repository_ids": [
                            item.id for item in repositories[:20]
                        ],
                    },
                )
            )
        else:
            try:
                target = self.resources.resolve_repository_target(
                    project,
                    actor=actor,
                )
                checks.append(
                    self._check(
                        "repository:execution-target",
                        "execution_target",
                        ReadinessCheckStatus.READY,
                        "repository_execution_target_ready",
                        "Project has a deterministic repository execution target.",
                        affected_type="repository",
                        affected_id=target.mutable_repository_id,
                        remediation_route=(
                            f"/api/projects/{project.id}/resources"
                        ),
                        details={
                            "policy": "deterministic",
                            "target_resolution": "project_default",
                            "mutable_repository_id": target.mutable_repository_id,
                            "source": str(
                                getattr(target.source, "value", target.source)
                            ),
                        },
                    )
                )
            except RepositoryTargetSelectionError as exc:
                checks.append(
                    self._check(
                        "repository:execution-target",
                        "execution_target",
                        ReadinessCheckStatus.BLOCKED,
                        exc.code,
                        str(exc),
                        affected_type="project",
                        affected_id=project.id,
                        remediation=(
                            "Bind/select one deterministic repository execution "
                            "target for the Project or change repository selection "
                            "policy to explicit."
                        ),
                        remediation_route=(
                            f"/api/projects/{project.id}/resources"
                        ),
                        details={
                            "policy": "deterministic",
                            "target_resolution": "project_default",
                        },
                    )
                )

        bootstrap_task_source_required = bool(
            latest_bootstrap is not None
            and latest_bootstrap.plan.manifest.task_source is not None
        )
        task_source_required = bool(
            project.authoritative_task_source is not None
            or bootstrap_task_source_required
        )
        source = project.authoritative_task_source
        if not task_source_required:
            checks.append(
                self._check(
                    "task-source:binding",
                    "task_source",
                    ReadinessCheckStatus.NOT_APPLICABLE,
                    "task_source_not_required",
                    "This Project does not require an authoritative TaskSource.",
                    affected_type="project",
                    affected_id=project.id,
                    required=False,
                )
            )
            checks.append(
                self._check(
                    "task-source:secret-reference",
                    "secret_reference",
                    ReadinessCheckStatus.NOT_APPLICABLE,
                    "task_source_secret_not_required",
                    "No TaskSource credential reference is required.",
                    affected_type="project",
                    affected_id=project.id,
                    required=False,
                )
            )
        elif source is None:
            checks.append(
                self._check(
                    "task-source:binding",
                    "task_source",
                    ReadinessCheckStatus.BLOCKED,
                    "task_source_missing",
                    "Project requires an authoritative TaskSource but none is bound.",
                    affected_type="project",
                    affected_id=project.id,
                    remediation=(
                        "Complete TaskSource bootstrap/reconciliation."
                    ),
                    remediation_route=(
                        f"/api/projects/{project.id}/bootstrap/plan"
                    ),
                )
            )
            checks.append(
                self._check(
                    "task-source:secret-reference",
                    "secret_reference",
                    ReadinessCheckStatus.BLOCKED,
                    "task_source_secret_reference_missing",
                    (
                        "TaskSource credential readiness cannot be established "
                        "until the authoritative TaskSource is bound."
                    ),
                    affected_type="project",
                    affected_id=project.id,
                    remediation_route=(
                        f"/api/projects/{project.id}/bootstrap/plan"
                    ),
                )
            )
        else:
            checks.append(
                self._check(
                    "task-source:binding",
                    "task_source",
                    ReadinessCheckStatus.READY,
                    "task_source_ready",
                    "Authoritative TaskSource is bound.",
                    affected_type="task_source",
                    affected_id=source.source_type,
                    details={
                        "source_type": source.source_type,
                        "source_instance": source.source_instance,
                        "scope": source.scope,
                    },
                )
            )
            secret_id = str(source.credential_secret_id or "").strip()
            if not secret_id:
                checks.append(
                    self._check(
                        "task-source:secret-reference",
                        "secret_reference",
                        ReadinessCheckStatus.BLOCKED,
                        "task_source_secret_reference_missing",
                        (
                            "Authoritative TaskSource has no canonical "
                            "SecretReference."
                        ),
                        affected_type="task_source",
                        affected_id=source.source_type,
                        remediation=(
                            "Bind an authorized canonical SecretReference."
                        ),
                        remediation_route="/api/secrets",
                    )
                )
            else:
                try:
                    reference = self.secrets.metadata(
                        secret_id,
                        actor=actor,
                        require_use=True,
                    )
                    active = reference.status().value == "active"
                except Exception:
                    active = False
                checks.append(
                    self._check(
                        "task-source:secret-reference",
                        "secret_reference",
                        (
                            ReadinessCheckStatus.READY
                            if active
                            else ReadinessCheckStatus.BLOCKED
                        ),
                        (
                            "secret_reference_ready"
                            if active
                            else "secret_reference_missing_or_unauthorized"
                        ),
                        (
                            "TaskSource SecretReference is active and authorized."
                            if active
                            else (
                                "TaskSource SecretReference is missing, inactive, "
                                "or unauthorized."
                            )
                        ),
                        affected_type="secret_reference",
                        affected_id=secret_id,
                        remediation=(
                            None
                            if active
                            else (
                                "Repair or replace the TaskSource SecretReference "
                                "authorization."
                            )
                        ),
                        remediation_route="/api/secrets",
                        details={"secret_reference_id": secret_id},
                    )
                )

        auth_context = ConfigurationContext(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            project_id=project.id,
        )
        try:
            auth = runtime_authentication_preflight(
                self.runtime_binding,
                configuration=self.configuration,
                context=auth_context,
                credential_mapping=self.runtime_credential_configs,
                permitted_codex_modes=self.permitted_codex_authentication_modes,
                secret_metadata=lambda secret_id: self.secrets.metadata(
                    secret_id,
                    actor=actor,
                    require_use=True,
                ),
                local_session_probe=self.local_session_probe,
            )
        except RuntimeAuthenticationConfigurationError as exc:
            checks.append(
                self._check(
                    "runtime:credential-reference",
                    "authentication",
                    ReadinessCheckStatus.BLOCKED,
                    "authentication_mode_invalid",
                    str(exc),
                    affected_type="configuration",
                    affected_id="codex.execution.authentication_mode",
                    remediation="Select one supported Codex authentication mode explicitly.",
                    remediation_route="/api/configuration",
                )
            )
            auth = None

        if auth is None:
            checks.append(
                self._check(
                    "runtime:credential-reference",
                    "authentication",
                    ReadinessCheckStatus.NOT_APPLICABLE,
                    "credential_reference_not_required",
                    "No runtime authentication requirement applies.",
                    affected_type="project",
                    affected_id=project.id,
                    required=False,
                )
            )
        else:
            details = auth.public()
            requirement = auth.requirement
            not_required = auth.status == RuntimeAuthenticationStatus.NOT_REQUIRED
            status = (
                ReadinessCheckStatus.NOT_APPLICABLE
                if not_required
                else (
                    ReadinessCheckStatus.READY
                    if auth.available
                    else ReadinessCheckStatus.BLOCKED
                )
            )
            affected_type = (
                "configuration"
                if requirement.credential_config_key
                else "agent_runtime"
            )
            affected_id = (
                requirement.credential_config_key
                or f"{requirement.provider_id}/{requirement.runtime_id}"
            )
            checks.append(
                self._check(
                    "runtime:credential-reference",
                    "authentication",
                    status,
                    auth.code,
                    auth.message,
                    affected_type=affected_type,
                    affected_id=affected_id,
                    remediation=auth.remediation,
                    remediation_route=auth.remediation_route,
                    required=not not_required,
                    details=details,
                )
            )

        worker = self.workers.execution_readiness(
            required_capabilities=(
                WorkerCapability.GIT,
                WorkerCapability.COMMAND_EXECUTION,
            ),
            execution_contract_version="thread-turn/1.0",
            actor=actor,
        )
        checks.append(
            self._check(
                "execution:worker",
                "execution_worker",
                (
                    ReadinessCheckStatus.READY
                    if worker.ready
                    else ReadinessCheckStatus.BLOCKED
                ),
                worker.code,
                worker.reason,
                affected_type="execution_worker",
                remediation=(
                    None if worker.ready else worker.remediation
                ),
                remediation_route="/api/execution-workers/readiness",
                details={
                    "eligible_worker_ids": list(worker.eligible_worker_ids),
                    "required_capabilities": [
                        item.value for item in worker.required_capabilities
                    ],
                    "available_capabilities": [
                        item.value for item in worker.available_capabilities
                    ],
                },
            )
        )

        if self.environment_probe is None:
            environment = {
                "available": True,
                "code": "sandbox_profile_supported",
                "reason": "Project sandbox is a supported canonical mode.",
            }
        else:
            environment = self.environment_probe(project, actor)
        environment_ready = bool(environment.get("available", False))
        checks.append(
            self._check(
                "execution:sandbox",
                "sandbox_profile",
                (
                    ReadinessCheckStatus.READY
                    if environment_ready
                    else ReadinessCheckStatus.BLOCKED
                ),
                str(
                    environment.get("code")
                    or (
                        "sandbox_profile_supported"
                        if environment_ready
                        else "sandbox_profile_unsupported"
                    )
                ),
                str(
                    environment.get("reason")
                    or (
                        "Execution environment supports the Project sandbox."
                        if environment_ready
                        else "Execution environment cannot enforce the Project sandbox."
                    )
                ),
                affected_type="sandbox_profile",
                affected_id=project.sandbox,
                remediation=(
                    None
                    if environment_ready
                    else str(
                        environment.get("remediation")
                        or "Select or repair a supported execution environment."
                    )
                ),
                remediation_route="/api/execution-workers/readiness",
            )
        )

        open_items = [
            item
            for item in self.load_work_items().values()
            if item.project_id == project.id
            and item.current_stage != "closed"
        ]
        bound_resource_ids = {item.id for item in bound}
        unresolved_items = [
            item
            for item in open_items
            if (
                item.organization_id != actor.organization_id
                or item.workspace_id != actor.workspace_id
                or not item.resource_ids
                or not set(item.resource_ids).intersection(bound_resource_ids)
            )
        ]
        if not open_items:
            checks.append(
                self._check(
                    "work-items:resource-associations",
                    "work_item",
                    ReadinessCheckStatus.NOT_APPLICABLE,
                    "work_item_associations_not_required",
                    "No open Work Items require repository association migration.",
                    affected_type="project",
                    affected_id=project.id,
                    required=False,
                )
            )
        elif unresolved_items:
            checks.append(
                self._check(
                    "work-items:resource-associations",
                    "work_item",
                    ReadinessCheckStatus.BLOCKED,
                    "work_item_resource_association_unresolved",
                    (
                        "Open Work Items still have unresolved tenant or "
                        "repository Resource associations."
                    ),
                    affected_type="project",
                    affected_id=project.id,
                    remediation=(
                        "Complete canonical Work Item Resource association migration."
                    ),
                    remediation_route="/api/work-items",
                    details={
                        "unresolved_count": len(unresolved_items),
                        "work_item_refs": [
                            item.ref for item in unresolved_items[:20]
                        ],
                    },
                )
            )
        else:
            checks.append(
                self._check(
                    "work-items:resource-associations",
                    "work_item",
                    ReadinessCheckStatus.READY,
                    "work_item_associations_ready",
                    "Open Work Items have canonical tenant/Resource associations.",
                    affected_type="project",
                    affected_id=project.id,
                    details={"open_work_item_count": len(open_items)},
                )
            )

        semantic_domains = {
            "project_scope",
            "bootstrap",
            "repository",
            "execution_target",
            "task_source",
            "secret_reference",
            "work_item",
        }
        semantic_ready = not any(
            item.status == ReadinessCheckStatus.BLOCKED
            and item.required
            and item.domain in semantic_domains
            for item in checks
        )
        execution_ready = semantic_ready and not any(
            item.status == ReadinessCheckStatus.BLOCKED
            and item.required
            for item in checks
        )
        if not execution_ready:
            overall = ReadinessCheckStatus.BLOCKED
        elif any(
            item.status == ReadinessCheckStatus.WARNING
            for item in checks
        ):
            overall = ReadinessCheckStatus.WARNING
        else:
            overall = ReadinessCheckStatus.READY

        previous = self.store.get(
            project.id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        generated_at = self.clock()
        snapshot = ProjectReadinessSnapshot(
            project_id=project.id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            semantic_ready=semantic_ready,
            execution_ready=execution_ready,
            status=overall,
            checks=tuple(checks),
            bootstrap_version=bootstrap_version,
            bootstrap_execution_id=bootstrap_execution_id,
            migration_version=bootstrap_version,
            last_successful_verification_at=(
                previous.last_successful_verification_at
                if previous is not None
                else None
            ),
            generated_at=generated_at,
        )
        if record:
            saved = self.store.record(snapshot)
            snapshot = snapshot.model_copy(
                update={
                    "last_successful_verification_at": (
                        saved.last_successful_verification_at
                    )
                }
            )
        return snapshot
