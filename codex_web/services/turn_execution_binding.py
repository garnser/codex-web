from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Mapping

from codex_web.configuration import ConfigurationContext, SecretReference as ConfigurationSecretReference
from codex_web.execution_subjects import ExecutionSubject, ExecutionSubjectKind
from codex_web.execution_workers import (
    ExecutionAssignment,
    ExecutionAssignmentCreate,
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
from codex_web.identity import AuthenticationActor
from codex_web.models import ApprovalPolicy, Project, SandboxMode
from codex_web.resources import Resource, ResourceLifecycle, ResourceType
from codex_web.services.codex_worker_configuration import (
    CODEX_WORKER_ACCESS_TOKEN_CONFIG,
)
from codex_web.services.configuration import (
    ConfigurationError,
    ConfigurationNotFoundError,
    ConfigurationService,
)
from codex_web.services.execution_workers import ExecutionWorkerService
from codex_web.services.execution_workspaces import ExecutionWorkspaceService
from codex_web.services.projects import ProjectService
from codex_web.services.resources import ResourceCatalogService


THREAD_TURN_EXECUTION_CONTRACT_VERSION = "thread-turn/1.0"
THREAD_BOOTSTRAP_EXECUTION_CONTRACT_VERSION = "thread-bootstrap/1.0"
DEFAULT_TURN_DEADLINE_SECONDS = 15 * 60
THREAD_BOOTSTRAP_SESSION_SECONDS = 24 * 60 * 60


class TurnExecutionBindingError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TurnExecutionBinding:
    thread_id: str | None
    execution_id: str
    project_id: str
    subject: ExecutionSubject
    workspace_id: str
    assignment_id: str
    resource_ids: tuple[str, ...]
    repository_resource_id: str
    base_revision: str | None
    sandbox: SandboxMode
    approval_policy: ApprovalPolicy
    secret_ref: str
    deadline_at: float | None
    runtime_binding: ExecutionRuntimeBinding | None = None

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
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.configuration = configuration
        self.projects = projects
        self.resources = resources
        self.workspaces = workspaces
        self.workers = workers
        self.control_actor = control_actor
        self.runtime_binding = runtime_binding
        self.runtime_credential_configs = dict(
            runtime_credential_configs
            or {("openai", "codex"): CODEX_WORKER_ACCESS_TOKEN_CONFIG}
        )
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
    ) -> tuple[tuple[Resource, ...], Resource]:
        resource_ids = self.resources.resource_ids_for_project(project)
        items = tuple(
            self.resources.get(resource_id, self.control_actor)
            for resource_id in resource_ids
        )
        eligible = tuple(
            item
            for item in items
            if item.lifecycle == ResourceLifecycle.ACTIVE
        )
        repositories = tuple(
            item
            for item in eligible
            if item.resource_type == ResourceType.REPOSITORY
        )
        if not repositories:
            raise TurnExecutionBindingError(
                "project has no active canonical repository resource for isolated Codex execution"
            )
        if len(repositories) != 1:
            raise TurnExecutionBindingError(
                "project has multiple active canonical repository resources; explicit target selection is required"
            )
        return eligible, repositories[0]

    def _secret_ref(
        self,
        project: Project,
        subject: ExecutionSubject,
        runtime_binding: ExecutionRuntimeBinding | None,
    ) -> str:
        if runtime_binding is None:
            raise TurnExecutionBindingError(
                "agent runtime binding is required for worker credential selection"
            )
        config_key = self.runtime_credential_configs.get(
            (runtime_binding.provider_id, runtime_binding.runtime_id)
        )
        if not config_key:
            raise TurnExecutionBindingError(
                "agent runtime has no worker credential reference configuration"
            )
        try:
            effective = self.configuration.resolve(
                config_key,
                ConfigurationContext(
                    organization_id=self.control_actor.organization_id,
                    workspace_id=self.control_actor.workspace_id,
                    project_id=project.id,
                    subject_id=subject.key,
                ),
            )
            reference = ConfigurationSecretReference.model_validate(effective.value)
        except (ConfigurationError, ConfigurationNotFoundError, ValueError) as exc:
            raise TurnExecutionBindingError(
                "worker credential reference configuration is unavailable "
                f"for {runtime_binding.provider_id}/{runtime_binding.runtime_id}"
            ) from exc
        return reference.secret_id

    @staticmethod
    def _lease_mode(sandbox: SandboxMode) -> LeaseMode:
        if sandbox == "read-only":
            return LeaseMode.READ
        if sandbox == "workspace-write":
            return LeaseMode.WRITE
        raise TurnExecutionBindingError(
            "danger-full-access cannot be prepared for isolated local Codex execution"
        )

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
        if workspace.repository_resource_id is None:
            raise TurnExecutionBindingError(
                "existing thread workspace has no canonical repository resource"
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
            base_revision=workspace.base_revision,
            sandbox=assignment.sandbox,
            approval_policy=assignment.approval_policy,
            secret_ref=assignment.secret_refs[0],
            deadline_at=assignment.deadline_at,
            runtime_binding=assignment.runtime_binding,
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
        existing = self._existing_assignment(execution_id=normalized_execution_id)
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
            )

        project_resources, repository = self._project_resources(project)
        secret_ref = self._secret_ref(project, subject, effective_runtime_binding)
        lease_mode = self._lease_mode(sandbox)
        effective_limits = limits or WorkerResourceLimits(
            wall_seconds=session_seconds
        )
        deadline_at = self._clock() + session_seconds

        workspace = self.workspaces.acquire(
            ExecutionWorkspaceAcquire(
                subject=subject,
                execution_id=normalized_execution_id,
                project_id=project.id,
                resource_ids=tuple(item.id for item in project_resources),
                repository_resource_id=repository.id,
                lease_mode=lease_mode,
                ttl_seconds=session_seconds,
                requested_disk_bytes=effective_limits.disk_bytes,
            ),
            actor=self.control_actor,
        )

        assignment = self.workers.create_assignment(
            ExecutionAssignmentCreate(
                subject=subject,
                execution_id=normalized_execution_id,
                project_id=project.id,
                resource_ids=workspace.resource_ids,
                base_revision=workspace.base_revision,
                execution_contract_version=execution_contract_version,
                required_capabilities=(
                    WorkerCapability.GIT,
                    WorkerCapability.COMMAND_EXECUTION,
                ),
                sandbox=sandbox,
                approval_policy=approval_policy,
                network=NetworkPolicy(),
                limits=effective_limits,
                secret_refs=(secret_ref,),
                deadline_at=deadline_at,
                execution_workspace_id=workspace.id,
                runtime_binding=effective_runtime_binding,
            ),
            actor=self.control_actor,
        )

        return TurnExecutionBinding(
            thread_id=thread_id,
            execution_id=normalized_execution_id,
            project_id=project.id,
            subject=subject,
            workspace_id=workspace.id,
            assignment_id=assignment.id,
            resource_ids=assignment.resource_ids,
            repository_resource_id=repository.id,
            base_revision=workspace.base_revision,
            sandbox=assignment.sandbox,
            approval_policy=assignment.approval_policy,
            secret_ref=secret_ref,
            deadline_at=assignment.deadline_at,
            runtime_binding=assignment.runtime_binding,
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
        )
