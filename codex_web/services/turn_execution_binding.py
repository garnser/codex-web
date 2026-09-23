from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Mapping

from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.configuration import ConfigurationContext
from codex_web.definitions import DefinitionReference
from codex_web.execution_subjects import ExecutionSubject, ExecutionSubjectKind
from codex_web.execution_workspace_backend import ExecutionWorkspaceBackendError
from codex_web.execution_workers import (
    ExecutionAssignment,
    ExecutionAssignmentCreate,
    CodexExecutionAuthenticationMode,
    ExecutionRuntimeBinding,
    NetworkPolicy,
    WorkerCapability,
    WorkerResourceLimits,
)
from codex_web.execution_workspaces import (
    ExecutionWorkspace,
    ExecutionWorkspaceAcquire,
    LeaseMode,
)
from codex_web.execution_profiles import ExecutionProfileContract
from codex_web.identity import AuthenticationActor
from codex_web.models import ApprovalPolicy, Project, SandboxMode
from codex_web.resources import (
    RepositoryExecutionScope,
    RepositoryExecutionTarget,
    RepositoryTargetEvidence,
    RepositoryTargetSource,
    RepositoryWriteMode,
    Resource,
    ResourceLifecycle,
    ResourceType,
)
from codex_web.runtime_credentials import (
    DEFAULT_RUNTIME_CREDENTIAL_CONFIGS,
    RuntimeAuthenticationConfigurationError,
    RuntimeAuthenticationPreflight,
    runtime_authentication_preflight,
)
from codex_web.services.configuration import ConfigurationService
from codex_web.services.execution_profile_definitions import ExecutionProfileDefinitionService
from codex_web.services.execution_workers import (
    ExecutionWorkerError,
    ExecutionWorkerService,
    WorkerConflictError,
)
from codex_web.services.execution_workspaces import (
    ExecutionWorkspaceConflictError,
    ExecutionWorkspaceError,
    ExecutionWorkspaceLeaseError,
    ExecutionWorkspaceQuotaError,
    ExecutionWorkspaceService,
)
from codex_web.services.projects import ProjectService
from codex_web.services.resources import (
    RepositoryTargetSelectionError,
    ResourceCatalogService,
)
from codex_web.services.secrets import SecretBroker


THREAD_TURN_EXECUTION_CONTRACT_VERSION = "thread-turn/1.0"
THREAD_BOOTSTRAP_EXECUTION_CONTRACT_VERSION = "thread-bootstrap/1.0"
DEFAULT_TURN_DEADLINE_SECONDS = 15 * 60
THREAD_BOOTSTRAP_SESSION_SECONDS = 24 * 60 * 60


class TurnExecutionBindingError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "execution_binding_error",
        blocker: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.blocker = blocker or {
            "code": code,
            "message": message,
            "retryable": False,
        }

    def public(self) -> dict[str, object]:
        return dict(self.blocker)


@dataclass(frozen=True, slots=True)
class TurnExecutionBinding:
    thread_id: str | None
    execution_id: str
    project_id: str
    subject: ExecutionSubject
    workspace_id: str
    assignment_id: str
    resource_ids: tuple[str, ...]
    repository_resource_id: str | None
    repository_target: RepositoryExecutionTarget
    repository_scope: RepositoryExecutionScope
    base_revision: str | None
    sandbox: SandboxMode
    approval_policy: ApprovalPolicy
    secret_ref: str
    deadline_at: float | None
    runtime_binding: ExecutionRuntimeBinding | None = None
    execution_profile_id: str | None = None
    execution_profile_definition: DefinitionReference | None = None
    agent_profile: AgentProfileExecutionBinding | None = None

    def public(self) -> dict[str, object]:
        return {
            "thread_id": self.thread_id,
            "execution_id": self.execution_id,
            "project_id": self.project_id,
            "subject": self.subject.model_dump(mode="json"),
            "workspace_id": self.workspace_id,
            "assignment_id": self.assignment_id,
            "resource_ids": list(self.resource_ids),
            "repository_resource_id": self.repository_resource_id,
            "repository_target": self.repository_target.model_dump(mode="json"),
            "repository_scope": self.repository_scope.model_dump(mode="json"),
            "base_revision": self.base_revision,
            "sandbox": self.sandbox,
            "approval_policy": self.approval_policy,
            "secret_ref": self.secret_ref,
            "deadline_at": self.deadline_at,
            "runtime_binding": (
                self.runtime_binding.model_dump(mode="json")
                if self.runtime_binding is not None
                else None
            ),
            "execution_profile_id": self.execution_profile_id,
            "execution_profile_definition": (
                self.execution_profile_definition.model_dump(mode="json")
                if self.execution_profile_definition is not None
                else None
            ),
            "agent_profile": (
                self.agent_profile.model_dump(mode="json")
                if self.agent_profile is not None
                else None
            ),
        }


class TurnExecutionBindingService:
    """Prepare canonical worker/workspace state for one thread execution.

    This service is deterministic and does not launch Codex. It selects only a
    credential *reference* from typed configuration; SecretBroker and the worker
    service identity remain responsible for deciding whether that reference can
    actually be used.
    """

    def __init__(
        self,
        configuration: ConfigurationService,
        projects: ProjectService,
        resources: ResourceCatalogService,
        workspaces: ExecutionWorkspaceService,
        workers: ExecutionWorkerService,
        *,
        control_actor: AuthenticationActor,
        runtime_binding: ExecutionRuntimeBinding | None = None,
        runtime_credential_configs: Mapping[tuple[str, str], str] | None = None,
        permitted_codex_authentication_modes: tuple[CodexExecutionAuthenticationMode, ...] | None = None,
        secrets: SecretBroker | None = None,
        local_session_probe: Callable[[], bool] | None = None,
        execution_profiles: ExecutionProfileDefinitionService | None = None,
        control_plane_available: Callable[[], bool] | None = None,
        project_readiness: Callable[
            [str, AuthenticationActor], dict[str, object]
        ] | None = None,
        skill_worker_requirements: Callable[
            [tuple[DefinitionReference, ...], Project],
            tuple[WorkerCapability, ...],
        ] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.configuration = configuration
        self.projects = projects
        self.resources = resources
        self.workspaces = workspaces
        self.workers = workers
        self.control_actor = control_actor
        self.runtime_binding = runtime_binding
        self.execution_profiles = execution_profiles
        self.control_plane_available = control_plane_available or (lambda: True)
        self.project_readiness = project_readiness
        self.skill_worker_requirements = skill_worker_requirements
        self.runtime_credential_configs = dict(
            runtime_credential_configs or DEFAULT_RUNTIME_CREDENTIAL_CONFIGS
        )
        self.permitted_codex_authentication_modes = permitted_codex_authentication_modes
        self.secrets = secrets
        self.local_session_probe = local_session_probe
        self._clock = clock

    @staticmethod
    def _subject(thread_id: str) -> ExecutionSubject:
        normalized = str(thread_id or "").strip()
        if not normalized:
            raise TurnExecutionBindingError("thread execution requires a canonical thread id")
        return ExecutionSubject(kind=ExecutionSubjectKind.THREAD, ref=normalized)

    @staticmethod
    def _bootstrap_subject(bootstrap_id: str) -> ExecutionSubject:
        normalized = str(bootstrap_id or "").strip()
        if not normalized:
            raise TurnExecutionBindingError(
                "thread bootstrap execution requires an immutable bootstrap id"
            )
        return ExecutionSubject(
            kind=ExecutionSubjectKind.THREAD_BOOTSTRAP,
            ref=normalized,
        )

    def _project(self, project_id: str) -> Project:
        normalized = str(project_id or "").strip()
        if not normalized:
            raise TurnExecutionBindingError("thread execution requires a canonical project id")
        try:
            return self.projects.get(normalized, self.control_actor.tenant)
        except Exception as exc:
            raise TurnExecutionBindingError("thread execution project is unavailable") from exc

    def _project_resources(
        self,
        project: Project,
    ) -> tuple[Resource, ...]:
        resource_ids = self.resources.resource_ids_for_project(project)
        return tuple(
            item
            for item in (
                self.resources.get(resource_id, self.control_actor)
                for resource_id in resource_ids
            )
            if item.lifecycle == ResourceLifecycle.ACTIVE
        )

    def _execution_profile(
        self,
        project: Project,
        profile_id: str | None,
    ) -> tuple[ExecutionProfileContract | None, DefinitionReference | None]:
        if self.execution_profiles is None:
            if profile_id:
                raise TurnExecutionBindingError(
                    "execution profile resolution is unavailable",
                    code="execution_profile_incompatible",
                    blocker={
                        "code": "execution_profile_incompatible",
                        "message": "execution profile resolution is unavailable",
                        "retryable": False,
                        "target_type": "execution_profile",
                        "target_id": profile_id,
                        "remediation_route": "/api/execution-profiles",
                    },
                )
            return None, None
        try:
            return self.execution_profiles.resolve(
                profile_id,
                organization_id=project.organization_id,
                workspace_id=project.workspace_id,
                project_id=project.id,
            )
        except Exception as exc:
            target = profile_id or "default"
            raise TurnExecutionBindingError(
                f"execution profile is unavailable: {target}",
                code="execution_profile_incompatible",
                blocker={
                    "code": "execution_profile_incompatible",
                    "message": f"execution profile is unavailable: {target}",
                    "retryable": False,
                    "target_type": "execution_profile",
                    "target_id": target,
                    "remediation_route": "/api/execution-profiles",
                },
            ) from exc

    def _repository_target(
        self,
        project: Project,
        *,
        explicit_repository_id: str | None = None,
        read_only_repository_ids: tuple[str, ...] = (),
        work_item_resource_ids: tuple[str, ...] = (),
        work_item_ref: str | None = None,
        thread_profile_repository_id: str | None = None,
        routing_repository_id: str | None = None,
        orchestration_only: bool = False,
    ) -> RepositoryExecutionTarget:
        try:
            target = self.resources.resolve_repository_target(
                project,
                actor=self.control_actor,
                work_item_resource_ids=work_item_resource_ids,
                work_item_ref=work_item_ref,
                explicit_repository_id=explicit_repository_id,
                thread_profile_repository_id=thread_profile_repository_id,
                routing_repository_id=routing_repository_id,
                read_only_repository_ids=read_only_repository_ids,
                orchestration_only=orchestration_only,
            )
        except RepositoryTargetSelectionError as exc:
            raise TurnExecutionBindingError(
                f"{exc.code}: {exc}",
                code=exc.code,
                blocker={
                    "code": exc.code,
                    "message": str(exc),
                    "retryable": False,
                    "target_type": "repository",
                    "target_id": explicit_repository_id,
                    "remediation_route": (
                        f"/api/projects/{project.id}/resources"
                    ),
                },
            ) from exc
        if target.mutable_repository_id is None and not orchestration_only:
            raise TurnExecutionBindingError(
                "repository execution target requires a mutable repository",
                code="repository_target_missing",
                blocker={
                    "code": "repository_target_missing",
                    "message": (
                        "repository execution target requires a mutable repository"
                    ),
                    "retryable": False,
                    "target_type": "project",
                    "target_id": project.id,
                    "remediation_route": (
                        f"/api/projects/{project.id}/resources"
                    ),
                },
            )
        return target

    def _coordinated_repository_scope(
        self,
        project: Project,
        *,
        writable_repository_ids: tuple[str, ...],
        read_only_repository_ids: tuple[str, ...] = (),
        source: RepositoryTargetSource = RepositoryTargetSource.EXPLICIT,
        source_ref: str | None = None,
    ) -> tuple[RepositoryExecutionTarget, RepositoryExecutionScope]:
        writable = tuple(
            dict.fromkeys(
                str(value).strip()
                for value in writable_repository_ids
                if str(value).strip()
            )
        )
        if len(writable) < 2:
            raise TurnExecutionBindingError(
                "coordinated repository execution requires at least two writable repositories",
                code="repository_scope_invalid",
                blocker={
                    "code": "repository_scope_invalid",
                    "message": (
                        "coordinated repository execution requires at least "
                        "two writable repositories"
                    ),
                    "retryable": False,
                    "target_type": "project",
                    "target_id": project.id,
                    "remediation_route": f"/api/projects/{project.id}/resources",
                },
            )
        read_only = tuple(
            dict.fromkeys(
                str(value).strip()
                for value in read_only_repository_ids
                if str(value).strip()
            )
        )
        if set(writable) & set(read_only):
            raise TurnExecutionBindingError(
                "writable repositories cannot also be read-only context",
                code="repository_scope_conflict",
                blocker={
                    "code": "repository_scope_conflict",
                    "message": "writable repositories cannot also be read-only context",
                    "retryable": False,
                    "target_type": "project",
                    "target_id": project.id,
                    "remediation_route": f"/api/projects/{project.id}/resources",
                },
            )

        bound_ids = set(self.resources.resource_ids_for_project(project))
        for repository_id in (*writable, *read_only):
            if repository_id not in bound_ids:
                raise TurnExecutionBindingError(
                    f"repository {repository_id} is not bound to Project {project.id}",
                    code="repository_target_unauthorized",
                    blocker={
                        "code": "repository_target_unauthorized",
                        "message": (
                            f"repository {repository_id} is not bound to "
                            f"Project {project.id}"
                        ),
                        "retryable": False,
                        "target_type": "repository",
                        "target_id": repository_id,
                        "remediation_route": f"/api/projects/{project.id}/resources",
                    },
                )
            try:
                resource = self.resources.get(repository_id, self.control_actor)
            except Exception as exc:
                raise TurnExecutionBindingError(
                    f"repository {repository_id} is unavailable",
                    code="repository_target_unauthorized",
                    blocker={
                        "code": "repository_target_unauthorized",
                        "message": f"repository {repository_id} is unavailable",
                        "retryable": False,
                        "target_type": "repository",
                        "target_id": repository_id,
                        "remediation_route": f"/api/projects/{project.id}/resources",
                    },
                ) from exc
            if resource.resource_type != ResourceType.REPOSITORY:
                raise TurnExecutionBindingError(
                    f"resource {repository_id} is not a repository",
                    code="repository_target_wrong_type",
                    blocker={
                        "code": "repository_target_wrong_type",
                        "message": f"resource {repository_id} is not a repository",
                        "retryable": False,
                        "target_type": "repository",
                        "target_id": repository_id,
                        "remediation_route": f"/api/projects/{project.id}/resources",
                    },
                )
            if resource.lifecycle != ResourceLifecycle.ACTIVE:
                raise TurnExecutionBindingError(
                    f"repository {repository_id} is not active",
                    code="repository_target_inactive",
                    blocker={
                        "code": "repository_target_inactive",
                        "message": f"repository {repository_id} is not active",
                        "retryable": False,
                        "target_type": "repository",
                        "target_id": repository_id,
                        "remediation_route": f"/api/projects/{project.id}/resources",
                    },
                )

        evidence = tuple(
            RepositoryTargetEvidence(
                source=source,
                repository_id=repository_id,
                source_ref=source_ref,
            )
            for repository_id in writable
        )
        primary = RepositoryExecutionTarget(
            organization_id=project.organization_id,
            workspace_id=project.workspace_id,
            project_id=project.id,
            mutable_repository_id=writable[0],
            read_only_repository_ids=read_only,
            source=source,
            source_ref=source_ref,
            selection_evidence=evidence,
        )
        scope = RepositoryExecutionScope(
            organization_id=project.organization_id,
            workspace_id=project.workspace_id,
            project_id=project.id,
            writable_repository_ids=writable,
            read_only_repository_ids=read_only,
            write_mode=RepositoryWriteMode.COORDINATED,
            source=source,
            source_ref=source_ref,
            selection_evidence=evidence,
        )
        return primary, scope

    def _authentication_preflight(
        self,
        project: Project,
        subject: ExecutionSubject,
        runtime_binding: ExecutionRuntimeBinding | None,
    ) -> RuntimeAuthenticationPreflight:
        if runtime_binding is None:
            raise TurnExecutionBindingError(
                "agent runtime binding is required for authentication selection",
                code="authentication_mode_invalid",
                blocker={
                    "code": "authentication_mode_invalid",
                    "message": "agent runtime binding is required for authentication selection",
                    "retryable": False,
                    "target_type": "project",
                    "target_id": project.id,
                    "remediation_route": "/api/configuration",
                },
            )
        context = ConfigurationContext(
            organization_id=self.control_actor.organization_id,
            workspace_id=self.control_actor.workspace_id,
            project_id=project.id,
            subject_id=subject.key,
        )
        secret_metadata = (
            None
            if self.secrets is None
            else lambda secret_id: self.secrets.metadata(
                secret_id,
                actor=self.control_actor,
                require_use=True,
            )
        )
        try:
            preflight = runtime_authentication_preflight(
                runtime_binding,
                configuration=self.configuration,
                context=context,
                credential_mapping=self.runtime_credential_configs,
                permitted_codex_modes=self.permitted_codex_authentication_modes,
                secret_metadata=secret_metadata,
                local_session_probe=self.local_session_probe,
            )
        except RuntimeAuthenticationConfigurationError as exc:
            raise TurnExecutionBindingError(
                str(exc),
                code="authentication_mode_invalid",
                blocker={
                    "code": "authentication_mode_invalid",
                    "message": str(exc),
                    "retryable": False,
                    "target_type": "agent_runtime",
                    "target_id": f"{runtime_binding.provider_id}/{runtime_binding.runtime_id}",
                    "remediation_route": "/api/configuration",
                },
            ) from exc
        if preflight is None:
            raise TurnExecutionBindingError(
                "agent runtime authentication requirement is unavailable",
                code="authentication_mode_invalid",
            )
        if not preflight.available:
            requirement = preflight.requirement
            raise TurnExecutionBindingError(
                preflight.message,
                code=preflight.code,
                blocker={
                    "code": preflight.code,
                    "message": preflight.message,
                    "retryable": preflight.code in {
                        "local_session_unavailable",
                        "authentication_unavailable",
                    },
                    "target_type": "agent_runtime",
                    "target_id": (
                        f"{requirement.provider_id}/{requirement.runtime_id}"
                    ),
                    "authentication": preflight.public(),
                    "remediation": preflight.remediation,
                    "remediation_route": preflight.remediation_route,
                },
            )
        return preflight

    @staticmethod
    def _lease_mode(sandbox: SandboxMode) -> LeaseMode:
        if sandbox == "read-only":
            return LeaseMode.READ
        if sandbox in {"workspace-write", "danger-full-access"}:
            return LeaseMode.WRITE
        raise TurnExecutionBindingError(
            f"unsupported sandbox mode: {sandbox}",
            code="sandbox_profile_unsupported",
            blocker={
                "code": "sandbox_profile_unsupported",
                "message": f"unsupported sandbox mode: {sandbox}",
                "retryable": False,
                "target_type": "sandbox_profile",
                "target_id": str(sandbox),
                "remediation_route": "/api/execution-profiles",
            },
        )

    def _validate_sandbox_compatibility(
        self,
        sandbox: SandboxMode,
        *,
        runtime_binding: ExecutionRuntimeBinding | None,
        execution_profile: ExecutionProfileContract | None,
    ) -> LeaseMode:
        lease_mode = self._lease_mode(sandbox)
        if (
            runtime_binding is not None
            and runtime_binding.sandbox_profiles
            and sandbox not in runtime_binding.sandbox_profiles
        ):
            raise TurnExecutionBindingError(
                f"runtime does not support sandbox profile: {sandbox}",
                code="sandbox_profile_unsupported",
                blocker={
                    "code": "sandbox_profile_unsupported",
                    "message": f"runtime does not support sandbox profile: {sandbox}",
                    "retryable": False,
                    "target_type": "agent_runtime",
                    "target_id": (
                        f"{runtime_binding.provider_id}/{runtime_binding.runtime_id}"
                    ),
                    "incompatible_layer": "runtime",
                    "requested_sandbox_profile": sandbox,
                    "supported_sandbox_profiles": list(
                        runtime_binding.sandbox_profiles
                    ),
                    "remediation_route": "/api/agent-providers",
                },
            )
        if (
            execution_profile is not None
            and execution_profile.repository_access == "read-only"
            and sandbox != "read-only"
        ):
            raise TurnExecutionBindingError(
                (
                    f"execution profile {execution_profile.id} requires "
                    "read-only sandboxing"
                ),
                code="execution_profile_incompatible",
                blocker={
                    "code": "execution_profile_incompatible",
                    "message": (
                        f"execution profile {execution_profile.id} requires "
                        "read-only sandboxing"
                    ),
                    "retryable": False,
                    "target_type": "execution_profile",
                    "target_id": execution_profile.id,
                    "incompatible_layer": "execution_profile",
                    "requested_sandbox_profile": sandbox,
                    "supported_sandbox_profiles": ["read-only"],
                    "remediation_route": "/api/execution-profiles",
                },
            )
        return lease_mode

    def _existing_assignment(
        self,
        *,
        execution_id: str,
    ) -> ExecutionAssignment | None:
        matches = [
            item
            for item in self.workers.list_assignments(self.control_actor)
            if item.execution_id == execution_id
        ]
        if len(matches) > 1:
            raise TurnExecutionBindingError(
                "multiple canonical assignments exist for the same execution id"
            )
        return matches[0] if matches else None

    def _binding_from_existing(
        self,
        assignment: ExecutionAssignment,
        *,
        subject: ExecutionSubject,
        thread_id: str | None,
        project: Project,
        sandbox: SandboxMode,
        approval_policy: ApprovalPolicy,
        execution_contract_version: str,
        runtime_binding: ExecutionRuntimeBinding | None,
        repository_target: RepositoryExecutionTarget,
        repository_scope: RepositoryExecutionScope,
        execution_profile_id: str | None,
        execution_profile_definition: DefinitionReference | None,
        agent_profile: AgentProfileExecutionBinding | None,
    ) -> TurnExecutionBinding:
        if assignment.subject != subject:
            raise TurnExecutionBindingError(
                "execution id is already bound to a different execution subject"
            )
        if assignment.project_id != project.id:
            raise TurnExecutionBindingError(
                "execution id is already bound to a different project"
            )
        if (
            assignment.sandbox != sandbox
            or assignment.approval_policy != approval_policy
        ):
            raise TurnExecutionBindingError(
                "execution id is already bound to different execution controls"
            )
        if (
            assignment.repository_target is not None
            and assignment.repository_target != repository_target
        ):
            raise TurnExecutionBindingError(
                "execution id is already bound to a different repository target"
            )
        if (
            assignment.repository_scope is not None
            and assignment.repository_scope != repository_scope
        ):
            raise TurnExecutionBindingError(
                "execution id is already bound to a different repository scope"
            )
        if assignment.execution_profile_id is None:
            if (
                execution_profile_id not in {None, "repository-write"}
                or repository_target.mutable_repository_id is None
            ):
                raise TurnExecutionBindingError(
                    "legacy unprofiled execution cannot change execution profile"
                )
        elif (
            assignment.execution_profile_id != execution_profile_id
            or assignment.execution_profile_definition
            != execution_profile_definition
        ):
            raise TurnExecutionBindingError(
                "execution id is already bound to a different execution profile"
            )
        comparable_agent_profile = agent_profile
        if (
            assignment.agent_profile is not None
            and comparable_agent_profile is not None
            and assignment.agent_profile.selected_worker_id
            and comparable_agent_profile.selected_worker_id is None
        ):
            comparable_agent_profile = comparable_agent_profile.model_copy(
                update={
                    "selected_worker_id": (
                        assignment.agent_profile.selected_worker_id
                    )
                }
            )
        if assignment.agent_profile != comparable_agent_profile:
            raise TurnExecutionBindingError(
                "execution id is already bound to a different Agent Profile revision"
            )
        if assignment.execution_contract_version != execution_contract_version:
            raise TurnExecutionBindingError(
                "execution id is already bound to a different execution contract"
            )
        if (
            runtime_binding is not None
            and assignment.runtime_binding != runtime_binding
        ):
            raise TurnExecutionBindingError(
                "execution id is already bound to a different agent runtime"
            )
        if not assignment.execution_workspace_id:
            raise TurnExecutionBindingError(
                "existing thread assignment has no canonical execution workspace"
            )
        workspace = self.workspaces.get(
            assignment.execution_workspace_id,
            self.control_actor,
        )
        if (
            workspace.subject != assignment.subject
            or workspace.execution_id != assignment.execution_id
            or workspace.project_id != project.id
        ):
            raise TurnExecutionBindingError(
                "existing thread assignment no longer matches its execution workspace"
            )
        if repository_target.mutable_repository_id is None:
            if workspace.repository_resource_id is not None:
                raise TurnExecutionBindingError(
                    "orchestration workspace unexpectedly has repository authority"
                )
        elif workspace.repository_resource_id != repository_target.mutable_repository_id:
            raise TurnExecutionBindingError(
                "existing thread workspace no longer matches repository target"
            )
        if tuple(workspace.writable_repository_ids) != tuple(
            repository_scope.writable_repository_ids
        ):
            raise TurnExecutionBindingError(
                "existing thread workspace no longer matches repository scope"
            )
        if len(assignment.secret_refs) != 1:
            raise TurnExecutionBindingError(
                "existing thread assignment does not have exactly one credential reference"
            )
        return TurnExecutionBinding(
            thread_id=thread_id,
            execution_id=assignment.execution_id,
            project_id=project.id,
            subject=subject,
            workspace_id=workspace.id,
            assignment_id=assignment.id,
            resource_ids=assignment.resource_ids,
            repository_resource_id=workspace.repository_resource_id,
            repository_target=assignment.repository_target or repository_target,
            repository_scope=assignment.repository_scope or repository_scope,
            base_revision=workspace.base_revision,
            sandbox=assignment.sandbox,
            approval_policy=assignment.approval_policy,
            secret_ref=assignment.secret_refs[0],
            deadline_at=assignment.deadline_at,
            runtime_binding=assignment.runtime_binding,
            execution_profile_id=assignment.execution_profile_id,
            execution_profile_definition=assignment.execution_profile_definition,
            agent_profile=assignment.agent_profile,
        )

    def _require_project_readiness(
        self,
        project: Project,
        execution_contract_version: str,
    ) -> None:
        if (
            execution_contract_version
            != THREAD_TURN_EXECUTION_CONTRACT_VERSION
            or self.project_readiness is None
        ):
            return
        readiness = self.project_readiness(
            project.id,
            self.control_actor,
        )
        if bool(readiness.get("execution_ready")):
            return
        blockers = [
            item
            for item in readiness.get("checks", [])
            if isinstance(item, dict)
            and item.get("status") == "blocked"
            and item.get("required", True)
        ]
        primary = blockers[0] if blockers else {}
        correlation_id = readiness.get("correlation_id")
        raise TurnExecutionBindingError(
            "Project is not execution-ready.",
            code="project_readiness_blocked",
            blocker={
                "code": "project_readiness_blocked",
                "message": (
                    str(primary.get("message"))
                    if primary.get("message")
                    else "Project semantic/execution readiness is blocked."
                ),
                "retryable": False,
                "target_type": "project",
                "target_id": project.id,
                "correlation_id": correlation_id,
                "readiness_url": (
                    f"/api/projects/{project.id}/readiness"
                ),
                "readiness_check_id": primary.get("id"),
                "readiness_code": primary.get("code"),
                "remediation": primary.get("remediation"),
                "remediation_route": (
                    primary.get("remediation_route")
                    or f"/api/projects/{project.id}/readiness"
                ),
            },
        )

    def _prepare_subject(
        self,
        *,
        subject: ExecutionSubject,
        thread_id: str | None,
        execution_id: str,
        project_id: str,
        sandbox: SandboxMode,
        approval_policy: ApprovalPolicy,
        execution_contract_version: str,
        session_seconds: int,
        max_session_seconds: int,
        limits: WorkerResourceLimits | None,
        runtime_binding: ExecutionRuntimeBinding | None = None,
        explicit_repository_id: str | None = None,
        writable_repository_ids: tuple[str, ...] = (),
        writable_repository_source: RepositoryTargetSource = RepositoryTargetSource.EXPLICIT,
        read_only_repository_ids: tuple[str, ...] = (),
        work_item_resource_ids: tuple[str, ...] = (),
        work_item_ref: str | None = None,
        thread_profile_repository_id: str | None = None,
        routing_repository_id: str | None = None,
        orchestration_only: bool = False,
        execution_profile_id: str | None = None,
        agent_profile: AgentProfileExecutionBinding | None = None,
    ) -> TurnExecutionBinding:
        normalized_execution_id = str(execution_id or "").strip()
        if not normalized_execution_id:
            raise TurnExecutionBindingError("thread execution requires an execution id")
        if session_seconds < 30 or session_seconds > max_session_seconds:
            raise TurnExecutionBindingError(
                "thread execution session lifetime must be between "
                f"30 and {max_session_seconds} seconds"
            )

        project = self._project(project_id)
        effective_runtime_binding = runtime_binding or self.runtime_binding
        execution_profile, execution_profile_definition = self._execution_profile(
            project,
            execution_profile_id,
        )
        lease_mode = self._validate_sandbox_compatibility(
            sandbox,
            runtime_binding=effective_runtime_binding,
            execution_profile=execution_profile,
        )
        profile_is_orchestration = bool(
            execution_profile is not None
            and execution_profile.workspace_mode == "scratch"
            and execution_profile.repository_access == "none"
        )
        if (
            execution_profile is not None
            and execution_profile.control_plane_operations
            and not self.control_plane_available()
        ):
            raise TurnExecutionBindingError(
                "brokered control-plane access is unavailable",
                code="control_plane_scope_missing",
                blocker={
                    "code": "control_plane_scope_missing",
                    "message": "brokered control-plane access is unavailable",
                    "retryable": False,
                    "target_type": "execution_profile",
                    "target_id": execution_profile.id,
                    "required_operations": list(
                        execution_profile.control_plane_operations
                    ),
                    "remediation": (
                        "Enable the assignment-bound control-plane broker or "
                        "select a profile that does not request brokered "
                        "control-plane operations."
                    ),
                    "remediation_route": "/api/control-plane-broker",
                },
            )
        if orchestration_only and execution_profile is not None and not profile_is_orchestration:
            raise TurnExecutionBindingError(
                "orchestration-only request conflicts with repository execution profile",
                code="execution_profile_incompatible",
                blocker={
                    "code": "execution_profile_incompatible",
                    "message": (
                        "orchestration-only request conflicts with repository "
                        "execution profile"
                    ),
                    "retryable": False,
                    "target_type": "execution_profile",
                    "target_id": execution_profile.id,
                    "remediation": (
                        "Use the orchestration-only profile or request "
                        "repository execution."
                    ),
                    "remediation_route": "/settings/execution-profiles",
                },
            )
        effective_orchestration_only = orchestration_only or profile_is_orchestration
        if effective_orchestration_only and execution_profile is None:
            raise TurnExecutionBindingError(
                "orchestration-only execution requires a canonical execution profile",
                code="execution_profile_incompatible",
                blocker={
                    "code": "execution_profile_incompatible",
                    "message": (
                        "orchestration-only execution requires a canonical "
                        "execution profile"
                    ),
                    "retryable": False,
                    "remediation": (
                        "Select the canonical orchestration-only execution "
                        "profile."
                    ),
                    "remediation_route": "/settings/execution-profiles",
                },
            )
        if (
            project.repository_selection_policy == "coordinated"
            and not writable_repository_ids
            and not effective_orchestration_only
        ):
            writable_repository_ids = tuple(
                item.id
                for item in self.resources.project_resources(
                    project,
                    actor=self.control_actor,
                )
                if item.resource_type == ResourceType.REPOSITORY
                and item.lifecycle == ResourceLifecycle.ACTIVE
            )
            writable_repository_source = RepositoryTargetSource.PROJECT_POLICY
        if writable_repository_ids:
            if effective_orchestration_only:
                raise TurnExecutionBindingError(
                    "orchestration-only execution cannot request writable repositories",
                    code="repository_scope_conflict",
                )
            normalized_writable = tuple(
                dict.fromkeys(
                    str(value).strip()
                    for value in writable_repository_ids
                    if str(value).strip()
                )
            )
            if (
                explicit_repository_id
                and explicit_repository_id not in normalized_writable
            ):
                raise TurnExecutionBindingError(
                    "explicit repository target conflicts with coordinated writable scope",
                    code="repository_target_conflict",
                    blocker={
                        "code": "repository_target_conflict",
                        "message": (
                            "explicit repository target conflicts with "
                            "coordinated writable scope"
                        ),
                        "retryable": False,
                        "target_type": "repository",
                        "target_id": explicit_repository_id,
                        "remediation_route": f"/api/projects/{project.id}/resources",
                    },
                )
            if len(normalized_writable) == 1:
                repository_target = self._repository_target(
                    project,
                    explicit_repository_id=(
                        normalized_writable[0]
                        if writable_repository_source == RepositoryTargetSource.EXPLICIT
                        else None
                    ),
                    read_only_repository_ids=read_only_repository_ids,
                    work_item_resource_ids=(
                        normalized_writable
                        if writable_repository_source == RepositoryTargetSource.WORK_ITEM
                        else ()
                    ),
                    work_item_ref=work_item_ref,
                )
                repository_scope = RepositoryExecutionScope.from_target(
                    repository_target
                )
            else:
                repository_target, repository_scope = self._coordinated_repository_scope(
                    project,
                    writable_repository_ids=normalized_writable,
                    read_only_repository_ids=read_only_repository_ids,
                    source=writable_repository_source,
                    source_ref=(
                        project.id
                        if writable_repository_source
                        == RepositoryTargetSource.PROJECT_POLICY
                        else work_item_ref or subject.ref
                    ),
                )
        else:
            repository_target = self._repository_target(
                project,
                explicit_repository_id=(
                    None if effective_orchestration_only else explicit_repository_id
                ),
                read_only_repository_ids=(
                    () if effective_orchestration_only else read_only_repository_ids
                ),
                work_item_resource_ids=(
                    () if effective_orchestration_only else work_item_resource_ids
                ),
                work_item_ref=work_item_ref,
                thread_profile_repository_id=(
                    None
                    if effective_orchestration_only
                    else thread_profile_repository_id
                ),
                routing_repository_id=(
                    None if effective_orchestration_only else routing_repository_id
                ),
                orchestration_only=effective_orchestration_only,
            )
            repository_scope = RepositoryExecutionScope.from_target(repository_target)
        self._require_project_readiness(
            project,
            execution_contract_version,
        )
        existing = self._existing_assignment(execution_id=normalized_execution_id)
        if (
            existing is not None
            and existing.runtime_binding is not None
            and effective_runtime_binding is not None
            and existing.runtime_binding.provider_id == effective_runtime_binding.provider_id
            and existing.runtime_binding.runtime_id == effective_runtime_binding.runtime_id
            and existing.runtime_binding.capability_revision
            == effective_runtime_binding.capability_revision
            and existing.runtime_binding.authentication_mode
        ):
            effective_runtime_binding = effective_runtime_binding.model_copy(
                update={
                    "authentication_mode": existing.runtime_binding.authentication_mode,
                }
            )
        effective_profile_id = (
            execution_profile.id if execution_profile is not None else None
        )
        if existing is not None:
            return self._binding_from_existing(
                existing,
                subject=subject,
                thread_id=thread_id,
                project=project,
                sandbox=sandbox,
                approval_policy=approval_policy,
                execution_contract_version=execution_contract_version,
                runtime_binding=effective_runtime_binding,
                repository_target=repository_target,
                repository_scope=repository_scope,
                execution_profile_id=effective_profile_id,
                execution_profile_definition=execution_profile_definition,
                agent_profile=agent_profile,
            )

        required_capabilities = (
            tuple(
                WorkerCapability(value)
                for value in execution_profile.required_worker_capabilities
            )
            if execution_profile is not None
            else (
                WorkerCapability.GIT,
                WorkerCapability.COMMAND_EXECUTION,
            )
        )
        if agent_profile is not None and agent_profile.skill_refs:
            if self.skill_worker_requirements is None:
                raise TurnExecutionBindingError(
                    "Agent Profile references Skills but Skill execution "
                    "requirements are unavailable",
                    code="skill_definition_unavailable",
                    blocker={
                        "code": "skill_definition_unavailable",
                        "message": (
                            "Agent Profile references Skills but Skill "
                            "execution requirements are unavailable"
                        ),
                        "retryable": False,
                        "target_type": "agent_profile",
                        "target_id": agent_profile.profile_id,
                        "remediation_route": "/api/skills",
                    },
                )
            try:
                skill_worker_capabilities = self.skill_worker_requirements(
                    agent_profile.skill_refs,
                    project,
                )
            except Exception as exc:
                raise TurnExecutionBindingError(
                    str(exc),
                    code="skill_definition_unavailable",
                    blocker={
                        "code": "skill_definition_unavailable",
                        "message": str(exc),
                        "retryable": False,
                        "target_type": "agent_profile",
                        "target_id": agent_profile.profile_id,
                        "remediation_route": "/api/skills",
                    },
                ) from exc
            required_capabilities = tuple(
                dict.fromkeys(
                    (*required_capabilities, *skill_worker_capabilities)
                )
            )
        worker_readiness = self.workers.execution_readiness(
            required_capabilities=required_capabilities,
            execution_contract_version=execution_contract_version,
            actor=self.control_actor,
            required_sandbox_profile=sandbox,
        )
        if not worker_readiness.ready:
            blocker = {
                "code": worker_readiness.code,
                "message": worker_readiness.reason,
                "retryable": False,
                "required_capabilities": [
                    value.value
                    for value in worker_readiness.required_capabilities
                ],
                "available_capabilities": [
                    value.value
                    for value in worker_readiness.available_capabilities
                ],
                "execution_contract_version": (
                    worker_readiness.execution_contract_version
                ),
                "active_worker_ids": list(
                    worker_readiness.active_worker_ids
                ),
                "remediation": worker_readiness.remediation,
            }
            if worker_readiness.code == "sandbox_profile_unsupported":
                blocker.update(
                    {
                        "target_type": "sandbox_profile",
                        "target_id": sandbox,
                        "incompatible_layer": "worker",
                        "requested_sandbox_profile": sandbox,
                        "remediation_route": "/api/execution-workers",
                    }
                )
            raise TurnExecutionBindingError(
                f"{worker_readiness.code}: {worker_readiness.reason}",
                code=worker_readiness.code,
                blocker=blocker,
            )

        authentication = self._authentication_preflight(
            project,
            subject,
            effective_runtime_binding,
        )
        requirement = authentication.requirement
        if (
            effective_runtime_binding is not None
            and requirement.codex_mode is not None
        ):
            effective_runtime_binding = effective_runtime_binding.model_copy(
                update={"authentication_mode": requirement.codex_mode}
            )
        secret_ref = authentication.secret_reference_id
        effective_limits = limits or WorkerResourceLimits(
            wall_seconds=session_seconds
        )
        deadline_at = self._clock() + session_seconds

        try:
            if effective_orchestration_only:
                workspace = self.workspaces.acquire(
                    ExecutionWorkspaceAcquire(
                        subject=subject,
                        execution_id=normalized_execution_id,
                        project_id=project.id,
                        resource_ids=(),
                        scratch=True,
                        lease_mode=lease_mode,
                        ttl_seconds=session_seconds,
                        requested_disk_bytes=effective_limits.disk_bytes,
                    ),
                    actor=self.control_actor,
                )
            else:
                if repository_target.mutable_repository_id is None:
                    raise TurnExecutionBindingError(
                        "repository execution profile requires a mutable repository target",
                        code="repository_target_missing",
                        blocker={
                            "code": "repository_target_missing",
                            "message": (
                                "repository execution profile requires a "
                                "mutable repository target"
                            ),
                            "retryable": False,
                            "target_type": "project",
                            "target_id": project.id,
                            "remediation_route": (
                                f"/api/projects/{project.id}/resources"
                            ),
                        },
                    )
                writable_ids = repository_scope.writable_repository_ids
                workspace = self.workspaces.acquire(
                    ExecutionWorkspaceAcquire(
                        subject=subject,
                        execution_id=normalized_execution_id,
                        project_id=project.id,
                        resource_ids=(
                            *writable_ids,
                            *repository_scope.read_only_repository_ids,
                        ),
                        repository_resource_id=repository_target.mutable_repository_id,
                        writable_repository_ids=writable_ids,
                        read_only_repository_ids=(
                            repository_scope.read_only_repository_ids
                        ),
                        lease_mode=lease_mode,
                        ttl_seconds=session_seconds,
                        requested_disk_bytes=effective_limits.disk_bytes,
                    ),
                    actor=self.control_actor,
                )
        except TurnExecutionBindingError:
            raise
        except ExecutionWorkspaceQuotaError as exc:
            raise TurnExecutionBindingError(
                str(exc),
                code="quota_or_capacity_blocked",
                blocker={
                    "code": "quota_or_capacity_blocked",
                    "message": str(exc),
                    "retryable": True,
                    "target_type": "project",
                    "target_id": project.id,
                    "remediation_route": "/api/execution-workspaces",
                },
            ) from exc
        except (ExecutionWorkspaceLeaseError, ExecutionWorkspaceConflictError) as exc:
            detail = str(exc)
            code = (
                "lease_conflict"
                if "lease" in detail.casefold()
                else "workspace_provisioning_blocked"
            )
            raise TurnExecutionBindingError(
                detail,
                code=code,
                blocker={
                    "code": code,
                    "message": detail,
                    "retryable": code == "lease_conflict",
                    "target_type": "repository",
                    "target_id": repository_target.mutable_repository_id,
                    "remediation_route": "/api/execution-workspaces",
                },
            ) from exc
        except (ExecutionWorkspaceError, ExecutionWorkspaceBackendError) as exc:
            raise TurnExecutionBindingError(
                str(exc),
                code="workspace_provisioning_blocked",
                blocker={
                    "code": "workspace_provisioning_blocked",
                    "message": str(exc),
                    "retryable": False,
                    "target_type": "project",
                    "target_id": project.id,
                    "remediation_route": "/api/execution-workspaces",
                },
            ) from exc

        try:
            assignment = self.workers.create_assignment(
                ExecutionAssignmentCreate(
                    subject=subject,
                    execution_id=normalized_execution_id,
                    project_id=project.id,
                    resource_ids=workspace.resource_ids,
                    base_revision=workspace.base_revision,
                    execution_contract_version=execution_contract_version,
                    required_capabilities=required_capabilities,
                    sandbox=sandbox,
                    approval_policy=approval_policy,
                    network=NetworkPolicy(),
                    limits=effective_limits,
                    secret_refs=((secret_ref,) if secret_ref else ()),
                    deadline_at=deadline_at,
                    execution_workspace_id=workspace.id,
                    runtime_binding=effective_runtime_binding,
                    repository_target=repository_target,
                    repository_scope=repository_scope,
                    execution_profile_id=effective_profile_id,
                    execution_profile_definition=execution_profile_definition,
                    agent_profile=agent_profile,
                ),
                actor=self.control_actor,
            )
        except WorkerConflictError as exc:
            raise TurnExecutionBindingError(
                str(exc),
                code="quota_or_capacity_blocked",
                blocker={
                    "code": "quota_or_capacity_blocked",
                    "message": str(exc),
                    "retryable": True,
                    "target_type": "execution_worker",
                    "remediation_route": "/api/execution-workers",
                },
            ) from exc
        except ExecutionWorkerError as exc:
            raise TurnExecutionBindingError(
                str(exc),
                code="worker_capability_missing",
                blocker={
                    "code": "worker_capability_missing",
                    "message": str(exc),
                    "retryable": False,
                    "target_type": "execution_worker",
                    "remediation_route": "/api/execution-workers/readiness",
                },
            ) from exc


        return TurnExecutionBinding(
            thread_id=thread_id,
            execution_id=normalized_execution_id,
            project_id=project.id,
            subject=subject,
            workspace_id=workspace.id,
            assignment_id=assignment.id,
            resource_ids=assignment.resource_ids,
            repository_resource_id=workspace.repository_resource_id,
            repository_target=repository_target,
            repository_scope=repository_scope,
            base_revision=workspace.base_revision,
            sandbox=assignment.sandbox,
            approval_policy=assignment.approval_policy,
            secret_ref=secret_ref,
            deadline_at=assignment.deadline_at,
            runtime_binding=assignment.runtime_binding,
            execution_profile_id=assignment.execution_profile_id,
            execution_profile_definition=assignment.execution_profile_definition,
            agent_profile=assignment.agent_profile,
        )

    def prepare(
        self,
        *,
        thread_id: str,
        execution_id: str,
        project_id: str,
        sandbox: SandboxMode,
        approval_policy: ApprovalPolicy,
        limits: WorkerResourceLimits | None = None,
        deadline_seconds: int = DEFAULT_TURN_DEADLINE_SECONDS,
        runtime_binding: ExecutionRuntimeBinding | None = None,
        explicit_repository_id: str | None = None,
        writable_repository_ids: tuple[str, ...] = (),
        writable_repository_source: RepositoryTargetSource = RepositoryTargetSource.EXPLICIT,
        read_only_repository_ids: tuple[str, ...] = (),
        work_item_resource_ids: tuple[str, ...] = (),
        work_item_ref: str | None = None,
        thread_profile_repository_id: str | None = None,
        routing_repository_id: str | None = None,
        orchestration_only: bool = False,
        execution_profile_id: str | None = None,
        agent_profile: AgentProfileExecutionBinding | None = None,
    ) -> TurnExecutionBinding:
        subject = self._subject(thread_id)
        return self._prepare_subject(
            subject=subject,
            thread_id=subject.ref,
            execution_id=execution_id,
            project_id=project_id,
            sandbox=sandbox,
            approval_policy=approval_policy,
            execution_contract_version=THREAD_TURN_EXECUTION_CONTRACT_VERSION,
            session_seconds=deadline_seconds,
            max_session_seconds=DEFAULT_TURN_DEADLINE_SECONDS,
            limits=limits,
            runtime_binding=runtime_binding,
            explicit_repository_id=explicit_repository_id,
            writable_repository_ids=writable_repository_ids,
            writable_repository_source=writable_repository_source,
            read_only_repository_ids=read_only_repository_ids,
            work_item_resource_ids=work_item_resource_ids,
            work_item_ref=work_item_ref,
            thread_profile_repository_id=thread_profile_repository_id,
            routing_repository_id=routing_repository_id,
            orchestration_only=orchestration_only,
            execution_profile_id=execution_profile_id,
            agent_profile=agent_profile,
        )

    def prepare_bootstrap(
        self,
        *,
        bootstrap_id: str,
        execution_id: str,
        project_id: str,
        sandbox: SandboxMode,
        approval_policy: ApprovalPolicy,
        limits: WorkerResourceLimits | None = None,
        session_seconds: int = THREAD_BOOTSTRAP_SESSION_SECONDS,
        runtime_binding: ExecutionRuntimeBinding | None = None,
        explicit_repository_id: str | None = None,
        writable_repository_ids: tuple[str, ...] = (),
        read_only_repository_ids: tuple[str, ...] = (),
        thread_profile_repository_id: str | None = None,
        routing_repository_id: str | None = None,
        execution_profile_id: str | None = None,
        agent_profile: AgentProfileExecutionBinding | None = None,
    ) -> TurnExecutionBinding:
        subject = self._bootstrap_subject(bootstrap_id)
        if (
            session_seconds < 30
            or session_seconds > THREAD_BOOTSTRAP_SESSION_SECONDS
        ):
            raise TurnExecutionBindingError(
                "thread execution session lifetime must be between "
                f"30 and {THREAD_BOOTSTRAP_SESSION_SECONDS} seconds"
            )
        effective_limits = limits or WorkerResourceLimits(
            cpu_seconds=session_seconds,
            wall_seconds=session_seconds,
        )
        return self._prepare_subject(
            subject=subject,
            thread_id=None,
            execution_id=execution_id,
            project_id=project_id,
            sandbox=sandbox,
            approval_policy=approval_policy,
            execution_contract_version=THREAD_BOOTSTRAP_EXECUTION_CONTRACT_VERSION,
            session_seconds=session_seconds,
            max_session_seconds=THREAD_BOOTSTRAP_SESSION_SECONDS,
            limits=effective_limits,
            runtime_binding=runtime_binding,
            explicit_repository_id=explicit_repository_id,
            writable_repository_ids=writable_repository_ids,
            read_only_repository_ids=read_only_repository_ids,
            thread_profile_repository_id=thread_profile_repository_id,
            routing_repository_id=routing_repository_id,
            execution_profile_id=execution_profile_id,
            agent_profile=agent_profile,
        )
