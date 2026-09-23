from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.configuration import (
    ConfigurationDraftCreate,
    ConfigurationPublishRequest,
    ConfigurationScope,
    SecretReference,
)
from codex_web.definitions import DefinitionReference
from codex_web.execution_subjects import ExecutionSubjectKind
from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.execution_workspace_backend import GitWorkspaceProvision
from codex_web.execution_workspaces import LeaseMode, WorkspaceQuota
from codex_web.execution_workers import (
    CodexExecutionAuthenticationMode,
    ExecutionRuntimeBinding,
    ExecutionWorkerRegister,
    WorkerCapability,
)
from codex_web.models import Project
from codex_web.resources import (
    RepositoryTargetSource,
    RepositoryWriteMode,
    ResourceAlias,
    ResourceCreate,
    ResourceLifecycle,
    ResourceType,
    ResourceUpdate,
)
from codex_web.services.anthropic_worker_configuration import (
    ANTHROPIC_WORKER_API_KEY_CONFIG,
    install_anthropic_worker_configuration,
)
from codex_web.services.codex_worker_configuration import (
    CODEX_WORKER_ACCESS_TOKEN_CONFIG,
    CODEX_WORKER_API_KEY_CONFIG,
    install_codex_worker_configuration,
)
from codex_web.services.configuration import ConfigurationService
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.execution_profile_definitions import (
    install_execution_profile_definitions,
)
from codex_web.services.execution_workers import ExecutionWorkerService
from codex_web.services.execution_workspaces import ExecutionWorkspaceService
from codex_web.services.identity import IdentityService
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.turn_execution_binding import (
    THREAD_BOOTSTRAP_EXECUTION_CONTRACT_VERSION,
    THREAD_BOOTSTRAP_SESSION_SECONDS,
    THREAD_TURN_EXECUTION_CONTRACT_VERSION,
    TurnExecutionBindingError,
    TurnExecutionBindingService,
)
from codex_web.storage.configuration_registry import ConfigurationRegistryStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.execution_workers import ExecutionWorkerStore
from codex_web.storage.execution_workspaces import ExecutionWorkspaceStateStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _AuthenticationSecrets:
    def __init__(self, status: str = "active", *, available: bool = True) -> None:
        self.status = status
        self.available = available

    def metadata(self, secret_id, *, actor, require_use=False):
        del secret_id, actor, require_use
        if not self.available:
            raise LookupError("secret unavailable")
        status = self.status
        return SimpleNamespace(
            status=lambda: SimpleNamespace(value=status)
        )


class _Projects:
    def __init__(self, project: Project) -> None:
        self.project = project

    def get(self, project_id: str, scope=None) -> Project:
        if project_id != self.project.id:
            raise LookupError("project not found")
        if scope is not None and (
            scope.organization_id != self.project.organization_id
            or scope.workspace_id != self.project.workspace_id
        ):
            raise LookupError("project not found")
        return self.project


class _FakeGitBackend:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.provisioned: list[tuple[str, str, str | None]] = []

    def provision_git(self, repository_path, workspace_id, branch_name, base_revision):
        path = self.root / workspace_id
        path.mkdir(parents=True, exist_ok=False)
        base = base_revision or "base-revision"
        self.provisioned.append((workspace_id, branch_name, base_revision))
        return GitWorkspaceProvision(
            path=path,
            branch_name=branch_name,
            base_revision=base,
            head_revision=base,
        )

    def provision_git_readonly(self, repository_path, workspace_id, base_revision):
        path = self.root / workspace_id
        path.mkdir(parents=True, exist_ok=False)
        base = base_revision or "readonly-base"
        self.provisioned.append((workspace_id, "", base_revision))
        return GitWorkspaceProvision(
            path=path,
            branch_name="",
            base_revision=base,
            head_revision=base,
        )

    def provision_scratch(self, workspace_id):
        path = self.root / workspace_id
        path.mkdir(parents=True, exist_ok=False)
        self.provisioned.append((workspace_id, "scratch", None))
        return path

    def cleanup_scratch(self, workspace_path):
        return None

    def cleanup_git(self, repository_path, workspace_path, branch_name, *, discard_branch):
        return None

    def head_revision(self, workspace_path):
        return "head-revision"

    def disk_usage(self, workspace_path):
        return 256


class TurnExecutionBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.sqlite = SQLiteStateStore(root / "state.sqlite3")
        self.identity = IdentityService(IdentityStateStore(self.sqlite))
        self.identity.bootstrap_local()
        self.actor = self.identity.local_trusted_actor()

        project_path = root / "repo"
        project_path.mkdir()
        self.project = Project(
            id="home",
            organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,
            name="Home",
            path=str(project_path),
        )
        self.projects = _Projects(self.project)

        self.resources = ResourceCatalogService(ResourceCatalogStore(self.sqlite))
        self.repository = self.resources.create(
            ResourceCreate(resource_type=ResourceType.REPOSITORY, name="Repository"),
            actor=self.actor,
        )
        self.service_resource = self.resources.create(
            ResourceCreate(resource_type=ResourceType.SERVICE, name="Build service"),
            actor=self.actor,
        )
        for resource in (self.repository, self.service_resource):
            self.resources.bind_project(
                project=self.project,
                resource_id=resource.id,
                actor=self.actor,
            )

        self.configuration = ConfigurationService(
            ConfigurationRegistryStore(self.sqlite)
        )
        install_codex_worker_configuration(self.configuration)
        install_anthropic_worker_configuration(self.configuration)

        self.backend = _FakeGitBackend(root / "workspaces")
        self.workspaces = ExecutionWorkspaceService(
            ExecutionWorkspaceStateStore(self.sqlite),
            self.backend,
            self.resources,
            lambda project_id: self.projects.get(project_id),
        )
        self.workers = ExecutionWorkerService(
            ExecutionWorkerStore(self.sqlite),
            workspaces=self.workspaces,
        )
        self.worker = self.workers.register(
            ExecutionWorkerRegister(
                service_identity_id="test-local-worker",
                pool="local",
                version="test-v1",
                capabilities=(
                    WorkerCapability.GIT,
                    WorkerCapability.COMMAND_EXECUTION,
                    WorkerCapability.ARTIFACT_UPLOAD,
                ),
                supported_execution_contract_versions=(
                    "1.0",
                    THREAD_TURN_EXECUTION_CONTRACT_VERSION,
                    THREAD_BOOTSTRAP_EXECUTION_CONTRACT_VERSION,
                ),
            ),
            actor=self.actor,
        )
        self.execution_profiles = install_execution_profile_definitions(
            DefinitionRegistryService(DefinitionRegistryStore(self.sqlite))
        )
        self.clock = 1_800_000_000.0
        self.service = TurnExecutionBindingService(
            self.configuration,
            self.projects,
            self.resources,
            self.workspaces,
            self.workers,
            control_actor=self.actor,
            runtime_binding=ExecutionRuntimeBinding(
                provider_id="openai",
                runtime_id="codex",
                capability_revision=1,
            ),
            runtime_credential_configs={
                ("openai", "codex"): CODEX_WORKER_ACCESS_TOKEN_CONFIG,
                ("anthropic", "claude-code"): ANTHROPIC_WORKER_API_KEY_CONFIG,
            },
            execution_profiles=self.execution_profiles,
            clock=lambda: self.clock,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _publish_secret(
        self,
        secret_id: str = "secret-codex-worker",
        *,
        scope_type: ConfigurationScope = ConfigurationScope.PROJECT,
        scope_id: str | None = "home",
        config_key: str = CODEX_WORKER_ACCESS_TOKEN_CONFIG,
    ) -> None:
        draft = self.configuration.create_draft(
            ConfigurationDraftCreate(
                key=config_key,
                scope_type=scope_type,
                scope_id=scope_id,
                value=SecretReference(secret_id=secret_id),
                actor=self.actor.identity_id,
            )
        )
        self.configuration.publish(
            draft.id,
            ConfigurationPublishRequest(actor=self.actor.identity_id),
        )

    def _prepare(
        self,
        *,
        execution_id: str = "turn-exec-1",
        sandbox: str = "workspace-write",
    ):
        return self.service.prepare(
            thread_id="thread-123",
            execution_id=execution_id,
            project_id=self.project.id,
            sandbox=sandbox,
            approval_policy="on-request",
        )

    def test_turn_preflight_distinguishes_expired_and_revoked_authentication(self) -> None:
        self._publish_secret()
        for status, expected in (
            ("expired", "authentication_expired"),
            ("revoked", "authentication_revoked"),
        ):
            with self.subTest(status=status):
                self.service.secrets = _AuthenticationSecrets(status)
                with self.assertRaises(TurnExecutionBindingError) as raised:
                    self._prepare(
                        execution_id=f"turn-auth-{status}",
                    )
                self.assertEqual(raised.exception.code, expected)
                blocker = raised.exception.public()
                self.assertEqual(
                    blocker["authentication"]["authentication_status"],
                    status,
                )
                self.assertEqual(self.workspaces.list(self.actor), [])
                self.assertEqual(self.workers.list_assignments(self.actor), [])

    def test_turn_preflight_reports_configured_but_unavailable_authentication(self) -> None:
        self._publish_secret()
        self.service.secrets = _AuthenticationSecrets(available=False)

        with self.assertRaises(TurnExecutionBindingError) as raised:
            self._prepare(execution_id="turn-auth-unavailable")

        self.assertEqual(raised.exception.code, "authentication_unavailable")
        self.assertNotEqual(
            raised.exception.code,
            "credential_reference_missing",
        )

    def test_turn_preflight_api_key_uses_distinct_secret_reference(self) -> None:
        self.service.runtime_binding = ExecutionRuntimeBinding(
            provider_id="openai",
            runtime_id="codex",
            capability_revision=1,
            authentication_mode="api_key",
        )
        self.service.secrets = _AuthenticationSecrets()
        self._publish_secret(
            secret_id="secret-codex-api-key",
            config_key=CODEX_WORKER_API_KEY_CONFIG,
        )

        binding = self._prepare(execution_id="turn-auth-api-key")
        assignment = next(
            item
            for item in self.workers.list_assignments(self.actor)
            if item.id == binding.assignment_id
        )
        self.assertEqual(
            assignment.runtime_binding.authentication_mode,
            "api_key",
        )
        self.assertEqual(
            assignment.secret_refs,
            ("secret-codex-api-key",),
        )

    def test_turn_preflight_policy_denial_is_explicit(self) -> None:
        self.service.runtime_binding = ExecutionRuntimeBinding(
            provider_id="openai",
            runtime_id="codex",
            capability_revision=1,
            authentication_mode="api_key",
        )
        self.service.permitted_codex_authentication_modes = (
            CodexExecutionAuthenticationMode.DELEGATED_WORKER_TOKEN,
        )

        with self.assertRaises(TurnExecutionBindingError) as raised:
            self._prepare(execution_id="turn-auth-policy-denied")

        self.assertEqual(raised.exception.code, "authentication_mode_denied")
        self.assertEqual(
            raised.exception.public()["authentication"]["authentication_status"],
            "denied",
        )

    def test_turn_preflight_local_session_availability_is_explicit(self) -> None:
        self.service.runtime_binding = ExecutionRuntimeBinding(
            provider_id="openai",
            runtime_id="codex",
            capability_revision=1,
            authentication_mode="trusted_local_session",
        )

        with self.assertRaises(TurnExecutionBindingError) as unsupported:
            self._prepare(execution_id="turn-local-unsupported")
        self.assertEqual(
            unsupported.exception.code,
            "authentication_mode_unsupported",
        )

        self.service.local_session_probe = lambda: False
        with self.assertRaises(TurnExecutionBindingError) as unavailable:
            self._prepare(execution_id="turn-local-unavailable")
        self.assertEqual(
            unavailable.exception.code,
            "local_session_unavailable",
        )

    def test_runtime_sandbox_mismatch_blocks_before_credential_preflight(self) -> None:
        self.service.runtime_binding = ExecutionRuntimeBinding(
            provider_id="openai",
            runtime_id="codex",
            capability_revision=1,
            sandbox_profiles=("read-only", "workspace-write"),
        )

        with self.assertRaises(TurnExecutionBindingError) as raised:
            self._prepare(
                execution_id="turn-runtime-sandbox-mismatch",
                sandbox="danger-full-access",
            )

        self.assertEqual(raised.exception.code, "sandbox_profile_unsupported")
        blocker = raised.exception.public()
        self.assertEqual(blocker["incompatible_layer"], "runtime")
        self.assertEqual(
            blocker["requested_sandbox_profile"],
            "danger-full-access",
        )
        self.assertNotEqual(blocker["code"], "credential_reference_missing")
        self.assertEqual(self.workspaces.list(self.actor), [])
        self.assertEqual(self.workers.list_assignments(self.actor), [])

    def test_worker_sandbox_mismatch_blocks_before_credential_preflight(self) -> None:
        self.workers.ensure_local_worker(
            service_identity_id="test-local-worker",
            version="test-v1",
            capabilities=(
                WorkerCapability.GIT,
                WorkerCapability.COMMAND_EXECUTION,
                WorkerCapability.ARTIFACT_UPLOAD,
            ),
            supported_execution_contract_versions=(
                "1.0",
                THREAD_TURN_EXECUTION_CONTRACT_VERSION,
                THREAD_BOOTSTRAP_EXECUTION_CONTRACT_VERSION,
            ),
            supported_sandbox_profiles=("read-only",),
            actor=self.actor,
        )

        with self.assertRaises(TurnExecutionBindingError) as raised:
            self._prepare(
                execution_id="turn-worker-sandbox-mismatch",
                sandbox="workspace-write",
            )

        self.assertEqual(raised.exception.code, "sandbox_profile_unsupported")
        blocker = raised.exception.public()
        self.assertEqual(blocker["incompatible_layer"], "worker")
        self.assertEqual(
            blocker["requested_sandbox_profile"],
            "workspace-write",
        )
        self.assertNotEqual(blocker["code"], "credential_reference_missing")
        self.assertEqual(self.workspaces.list(self.actor), [])
        self.assertEqual(self.workers.list_assignments(self.actor), [])

    def test_project_readiness_blocks_before_workspace_creation(self) -> None:
        self._publish_secret()
        self.service.project_readiness = lambda project_id, actor: {
            "execution_ready": False,
            "correlation_id": "readiness-corr-a",
            "checks": [
                {
                    "id": "repository:resources",
                    "domain": "repository",
                    "status": "blocked",
                    "code": "repository_resource_missing",
                    "message": "Project repository migration is incomplete.",
                    "required": True,
                    "remediation": "Bind the intended repository Resource.",
                    "remediation_route": "/api/projects/home/resources",
                }
            ],
        }

        with self.assertRaises(TurnExecutionBindingError) as raised:
            self._prepare(execution_id="turn-readiness-blocked")

        self.assertEqual(
            raised.exception.code,
            "project_readiness_blocked",
        )
        blocker = raised.exception.public()
        self.assertEqual(blocker["correlation_id"], "readiness-corr-a")
        self.assertEqual(
            blocker["readiness_check_id"],
            "repository:resources",
        )
        self.assertEqual(
            blocker["readiness_code"],
            "repository_resource_missing",
        )
        self.assertEqual(
            blocker["readiness_url"],
            "/api/projects/home/readiness",
        )
        self.assertEqual(self.workspaces.list(self.actor), [])
        self.assertEqual(self.workers.list_assignments(self.actor), [])

    def test_thread_bootstrap_bypasses_project_readiness_gate(self) -> None:
        self._publish_secret()
        self.service.project_readiness = lambda project_id, actor: {
            "execution_ready": False,
            "correlation_id": "readiness-blocked",
            "checks": [
                {
                    "id": "repository:resources",
                    "status": "blocked",
                    "required": True,
                    "code": "repository_resource_missing",
                    "message": "Project is not ready.",
                }
            ],
        }

        binding = self.service.prepare_bootstrap(
            bootstrap_id="bootstrap-a",
            execution_id="bootstrap-exec-a",
            project_id=self.project.id,
            sandbox="workspace-write",
            approval_policy="on-request",
            session_seconds=60,
        )

        self.assertEqual(binding.project_id, self.project.id)
        self.assertEqual(
            binding.subject.kind,
            ExecutionSubjectKind.THREAD_BOOTSTRAP,
        )

    def test_thread_bootstrap_persists_exact_agent_profile_binding(self) -> None:
        self._publish_secret()
        profile = AgentProfileExecutionBinding(
            profile_id="coordinator",
            profile_revision=4,
            profile_record_id="agent-profile-rev-4",
        )

        binding = self.service.prepare_bootstrap(
            bootstrap_id="bootstrap-profile",
            execution_id="bootstrap-profile-exec",
            project_id=self.project.id,
            sandbox="workspace-write",
            approval_policy="on-request",
            agent_profile=profile,
        )

        assignment = next(
            item
            for item in self.workers.list_assignments(self.actor)
            if item.id == binding.assignment_id
        )
        self.assertEqual(binding.agent_profile, profile)
        self.assertEqual(assignment.agent_profile, profile)
        self.assertEqual(
            assignment.agent_profile.profile_revision,
            4,
        )

    def test_skill_worker_requirement_tightens_assignment_eligibility(self) -> None:
        self._publish_secret()
        self.service.skill_worker_requirements = (
            lambda _refs, _project: (WorkerCapability.CONTAINER,)
        )
        profile = AgentProfileExecutionBinding(
            profile_id="coder",
            profile_revision=1,
            profile_record_id="agent-profile-rev-1",
            skill_refs=(
                DefinitionReference(
                    definition_id="container-helper",
                    kind="agent.skill",
                    revision=1,
                    record_id="skill-rev-1",
                    checksum="a" * 64,
                    definition_schema_version="1.0",
                ),
            ),
        )

        with self.assertRaises(TurnExecutionBindingError) as raised:
            self.service.prepare(
                thread_id="thread-skill-worker",
                execution_id="turn-skill-worker",
                project_id=self.project.id,
                sandbox="workspace-write",
                approval_policy="on-request",
                agent_profile=profile,
            )

        self.assertEqual(
            raised.exception.code,
            "worker_capability_missing",
        )
        blocker = raised.exception.public()
        self.assertIn(
            "container",
            blocker["required_capabilities"],
        )
        self.assertNotIn(
            "container",
            blocker["available_capabilities"],
        )
        self.assertEqual(self.workspaces.list(self.actor), [])
        self.assertEqual(self.workers.list_assignments(self.actor), [])

    def test_missing_command_execution_blocks_before_workspace_creation(self) -> None:
        self._publish_secret()
        self.workers.ensure_local_worker(
            service_identity_id="test-local-worker",
            version="test-v1",
            capabilities=(
                WorkerCapability.GIT,
                WorkerCapability.ARTIFACT_UPLOAD,
            ),
            supported_execution_contract_versions=(
                "1.0",
                THREAD_TURN_EXECUTION_CONTRACT_VERSION,
                THREAD_BOOTSTRAP_EXECUTION_CONTRACT_VERSION,
            ),
            actor=self.actor,
        )

        with self.assertRaises(TurnExecutionBindingError) as raised:
            self._prepare(execution_id="turn-no-command")

        self.assertEqual(
            raised.exception.code,
            "worker_capability_missing",
        )
        blocker = raised.exception.public()
        self.assertEqual(blocker["code"], "worker_capability_missing")
        self.assertIn(
            "command_execution",
            blocker["required_capabilities"],
        )
        self.assertNotIn(
            "command_execution",
            blocker["available_capabilities"],
        )
        self.assertIn("remediation", blocker)
        self.assertEqual(self.workspaces.list(self.actor), [])
        self.assertEqual(self.workers.list_assignments(self.actor), [])

    def test_prepares_thread_workspace_and_assignment_from_canonical_state(self) -> None:
        self._publish_secret()

        binding = self._prepare()
        workspace = self.workspaces.get(binding.workspace_id, self.actor)
        assignment = self.workers.list_assignments(self.actor)[0]

        self.assertEqual(binding.subject.kind, ExecutionSubjectKind.THREAD)
        self.assertEqual(binding.subject.ref, "thread-123")
        self.assertEqual(binding.execution_id, "turn-exec-1")
        self.assertEqual(binding.assignment_id, assignment.id)
        self.assertEqual(assignment.execution_workspace_id, workspace.id)
        self.assertEqual(assignment.execution_contract_version, THREAD_TURN_EXECUTION_CONTRACT_VERSION)
        self.assertEqual(
            set(assignment.required_capabilities),
            {WorkerCapability.GIT, WorkerCapability.COMMAND_EXECUTION},
        )
        self.assertEqual(set(binding.resource_ids), {self.repository.id})
        self.assertEqual(assignment.resource_ids, workspace.resource_ids)
        self.assertEqual(binding.repository_resource_id, self.repository.id)
        self.assertEqual(assignment.base_revision, workspace.base_revision)
        self.assertEqual(workspace.base_revision, "base-revision")
        self.assertEqual(assignment.secret_refs, ("secret-codex-worker",))
        self.assertEqual(binding.secret_ref, "secret-codex-worker")
        self.assertFalse(assignment.network.enabled)
        self.assertEqual(binding.deadline_at, self.clock + 900)
        self.assertEqual(len(self.backend.provisioned), 1)

    def test_orchestration_profile_uses_scratch_without_repository_or_git_authority(self) -> None:
        self._publish_secret()

        binding = self.service.prepare_bootstrap(
            bootstrap_id="bootstrap-orchestration",
            execution_id="bootstrap-orchestration-exec",
            project_id=self.project.id,
            sandbox="workspace-write",
            approval_policy="on-request",
            execution_profile_id="orchestration-only",
        )
        workspace = self.workspaces.get(binding.workspace_id, self.actor)
        assignment = next(
            item
            for item in self.workers.list_assignments(self.actor)
            if item.id == binding.assignment_id
        )

        self.assertEqual(binding.execution_profile_id, "orchestration-only")
        self.assertIsNotNone(binding.execution_profile_definition)
        self.assertEqual(binding.repository_target.source.value, "orchestration_only")
        self.assertIsNone(binding.repository_resource_id)
        self.assertEqual(binding.resource_ids, ())
        self.assertEqual(workspace.kind.value, "scratch")
        self.assertEqual(workspace.resource_ids, ())
        self.assertIsNone(workspace.repository_resource_id)
        self.assertIsNotNone(workspace.path)
        self.assertEqual(
            set(assignment.required_capabilities),
            {WorkerCapability.COMMAND_EXECUTION},
        )
        self.assertNotIn(WorkerCapability.GIT, assignment.required_capabilities)
        self.assertFalse(assignment.network.enabled)
        self.assertEqual(assignment.execution_profile_id, "orchestration-only")
        self.assertEqual(
            assignment.execution_profile_definition,
            binding.execution_profile_definition,
        )

    def test_danger_full_access_uses_write_workspace_and_canonical_assignment(self) -> None:
        self._publish_secret()

        binding = self._prepare(
            execution_id="turn-exec-danger",
            sandbox="danger-full-access",
        )
        inspection = next(
            item
            for item in self.workspaces.inspect(self.actor)
            if item.workspace.id == binding.workspace_id
        )
        assignment = self.workers.list_assignments(self.actor)[0]

        self.assertEqual(binding.sandbox, "danger-full-access")
        self.assertEqual(assignment.sandbox, "danger-full-access")
        self.assertIsNotNone(inspection.lease)
        self.assertEqual(inspection.lease.mode, LeaseMode.WRITE)
        self.assertFalse(assignment.network.enabled)
        self.assertEqual(assignment.execution_workspace_id, binding.workspace_id)

    def test_prepares_long_lived_thread_bootstrap_without_fake_thread_or_work_item(self) -> None:
        self._publish_secret()

        binding = self.service.prepare_bootstrap(
            bootstrap_id="bootstrap-123",
            execution_id="bootstrap-exec-1",
            project_id=self.project.id,
            sandbox="workspace-write",
            approval_policy="on-request",
        )
        workspace = self.workspaces.get(binding.workspace_id, self.actor)
        assignment = self.workers.list_assignments(self.actor)[0]

        self.assertIsNone(binding.thread_id)
        self.assertEqual(
            binding.subject.kind,
            ExecutionSubjectKind.THREAD_BOOTSTRAP,
        )
        self.assertEqual(binding.subject.ref, "bootstrap-123")
        self.assertIsNone(assignment.work_item_ref)
        self.assertIsNone(workspace.work_item_ref)
        self.assertEqual(
            assignment.execution_contract_version,
            THREAD_BOOTSTRAP_EXECUTION_CONTRACT_VERSION,
        )
        self.assertEqual(
            assignment.limits.cpu_seconds,
            THREAD_BOOTSTRAP_SESSION_SECONDS,
        )
        self.assertEqual(
            assignment.limits.wall_seconds,
            THREAD_BOOTSTRAP_SESSION_SECONDS,
        )
        self.assertEqual(
            binding.deadline_at,
            self.clock + THREAD_BOOTSTRAP_SESSION_SECONDS,
        )
        lease = next(
            item
            for item in self.workspaces.store.load().leases
            if item.execution_workspace_id == workspace.id
        )
        self.assertEqual(
            lease.expires_at - lease.acquired_at,
            THREAD_BOOTSTRAP_SESSION_SECONDS,
        )

    def test_bootstrap_lifetime_cannot_exceed_bounded_worker_workspace_contract(self) -> None:
        self._publish_secret()

        with self.assertRaisesRegex(
            TurnExecutionBindingError,
            "session lifetime",
        ):
            self.service.prepare_bootstrap(
                bootstrap_id="bootstrap-123",
                execution_id="bootstrap-exec-1",
                project_id=self.project.id,
                sandbox="workspace-write",
                approval_policy="on-request",
                session_seconds=THREAD_BOOTSTRAP_SESSION_SECONDS + 1,
            )

        self.assertEqual(self.workspaces.list(self.actor), [])
        self.assertEqual(self.workers.list_assignments(self.actor), [])

    def test_project_secret_overrides_workspace_secret_by_scope_precedence(self) -> None:
        self._publish_secret(
            "secret-workspace",
            scope_type=ConfigurationScope.WORKSPACE,
            scope_id=self.actor.workspace_id,
        )
        self._publish_secret("secret-project")

        binding = self._prepare()

        self.assertEqual(binding.secret_ref, "secret-project")
        assignment = self.workers.list_assignments(self.actor)[0]
        self.assertEqual(assignment.secret_refs, ("secret-project",))

    def test_same_execution_id_is_idempotent(self) -> None:
        self._publish_secret()

        first = self._prepare()
        second = self._prepare()

        self.assertEqual(second, first)
        self.assertEqual(len(self.workspaces.list(self.actor)), 1)
        self.assertEqual(len(self.workers.list_assignments(self.actor)), 1)
        self.assertEqual(len(self.backend.provisioned), 1)

    def test_missing_credential_configuration_fails_before_workspace_creation(self) -> None:
        with self.assertRaises(TurnExecutionBindingError) as caught:
            self._prepare()

        self.assertEqual(
            caught.exception.code,
            "credential_reference_missing",
        )
        self.assertIn(
            "credential reference configuration is unavailable",
            str(caught.exception),
        )
        self.assertEqual(self.workspaces.list(self.actor), [])
        self.assertEqual(self.workers.list_assignments(self.actor), [])

    def test_multiple_active_repositories_fail_closed_before_workspace_creation(self) -> None:
        self._publish_secret()
        second = self.resources.create(
            ResourceCreate(resource_type=ResourceType.REPOSITORY, name="Repository 2"),
            actor=self.actor,
        )
        self.resources.bind_project(
            project=self.project,
            resource_id=second.id,
            actor=self.actor,
        )

        with self.assertRaises(TurnExecutionBindingError) as caught:
            self._prepare()

        self.assertEqual(
            caught.exception.code,
            "repository_target_ambiguous",
        )
        self.assertEqual(
            caught.exception.public()["code"],
            "repository_target_ambiguous",
        )
        self.assertEqual(self.workspaces.list(self.actor), [])
        self.assertEqual(self.workers.list_assignments(self.actor), [])

    def test_unbound_explicit_repository_is_typed_as_unauthorized(self) -> None:
        self._publish_secret()
        unbound = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Unbound repository",
            ),
            actor=self.actor,
        )

        with self.assertRaises(TurnExecutionBindingError) as caught:
            self.service.prepare(
                thread_id="thread-unbound",
                execution_id="exec-unbound",
                project_id=self.project.id,
                sandbox="workspace-write",
                approval_policy="on-request",
                explicit_repository_id=unbound.id,
            )

        self.assertEqual(
            caught.exception.code,
            "repository_target_unauthorized",
        )
        self.assertEqual(self.workspaces.list(self.actor), [])
        self.assertEqual(self.workers.list_assignments(self.actor), [])

    def test_workspace_quota_is_typed_before_workspace_creation(self) -> None:
        self._publish_secret()
        limited_workspaces = ExecutionWorkspaceService(
            ExecutionWorkspaceStateStore(self.sqlite),
            self.backend,
            self.resources,
            lambda project_id: self.projects.get(project_id),
            quota=WorkspaceQuota(max_requested_disk_bytes=1),
        )
        service = TurnExecutionBindingService(
            self.configuration,
            self.projects,
            self.resources,
            limited_workspaces,
            self.workers,
            control_actor=self.actor,
            runtime_binding=ExecutionRuntimeBinding(
                provider_id="openai",
                runtime_id="codex",
                capability_revision=1,
            ),
            runtime_credential_configs={
                ("openai", "codex"): CODEX_WORKER_ACCESS_TOKEN_CONFIG,
            },
            execution_profiles=self.execution_profiles,
            clock=lambda: self.clock,
        )

        with self.assertRaises(TurnExecutionBindingError) as caught:
            service.prepare(
                thread_id="thread-quota",
                execution_id="exec-quota",
                project_id=self.project.id,
                sandbox="workspace-write",
                approval_policy="on-request",
            )

        self.assertEqual(
            caught.exception.code,
            "quota_or_capacity_blocked",
        )
        self.assertEqual(limited_workspaces.list(self.actor), [])
        self.assertEqual(self.workers.list_assignments(self.actor), [])

    def test_workspace_backend_failure_is_typed_before_assignment_creation(self) -> None:
        self._publish_secret()
        original = self.backend.provision_git

        def fail_provision(*_args, **_kwargs):
            raise RuntimeError("fixture workspace provisioning failed")

        self.backend.provision_git = fail_provision
        try:
            with self.assertRaises(TurnExecutionBindingError) as caught:
                self.service.prepare(
                    thread_id="thread-workspace-failure",
                    execution_id="exec-workspace-failure",
                    project_id=self.project.id,
                    sandbox="workspace-write",
                    approval_policy="on-request",
                )
        finally:
            self.backend.provision_git = original

        self.assertEqual(
            caught.exception.code,
            "workspace_provisioning_blocked",
        )
        self.assertEqual(self.workers.list_assignments(self.actor), [])
        failed = self.workspaces.list(self.actor)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].status.value, "error")

    def test_missing_control_plane_broker_is_typed_before_workspace_creation(self) -> None:
        self._publish_secret()
        service = TurnExecutionBindingService(
            self.configuration,
            self.projects,
            self.resources,
            self.workspaces,
            self.workers,
            control_actor=self.actor,
            runtime_binding=ExecutionRuntimeBinding(
                provider_id="openai",
                runtime_id="codex",
                capability_revision=1,
            ),
            runtime_credential_configs={
                ("openai", "codex"): CODEX_WORKER_ACCESS_TOKEN_CONFIG,
            },
            execution_profiles=self.execution_profiles,
            control_plane_available=lambda: False,
            clock=lambda: self.clock,
        )

        with self.assertRaises(TurnExecutionBindingError) as caught:
            service.prepare(
                thread_id="thread-orchestration",
                execution_id="exec-orchestration",
                project_id=self.project.id,
                sandbox="workspace-write",
                approval_policy="on-request",
                execution_profile_id="orchestration-only",
            )

        self.assertEqual(
            caught.exception.code,
            "control_plane_scope_missing",
        )
        self.assertEqual(self.workspaces.list(self.actor), [])
        self.assertEqual(self.workers.list_assignments(self.actor), [])

    def test_unknown_execution_profile_is_typed_before_workspace_creation(self) -> None:
        self._publish_secret()

        with self.assertRaises(TurnExecutionBindingError) as caught:
            self.service.prepare(
                thread_id="thread-profile",
                execution_id="exec-profile",
                project_id=self.project.id,
                sandbox="workspace-write",
                approval_policy="on-request",
                execution_profile_id="does-not-exist",
            )

        self.assertEqual(
            caught.exception.code,
            "execution_profile_incompatible",
        )
        self.assertEqual(self.workspaces.list(self.actor), [])
        self.assertEqual(self.workers.list_assignments(self.actor), [])

    def test_unsupported_sandbox_is_typed_before_workspace_creation(self) -> None:
        self._publish_secret()

        with self.assertRaises(TurnExecutionBindingError) as caught:
            self.service.prepare(
                thread_id="thread-sandbox",
                execution_id="exec-sandbox",
                project_id=self.project.id,
                sandbox="unsupported",
                approval_policy="on-request",
            )

        self.assertEqual(
            caught.exception.code,
            "sandbox_profile_unsupported",
        )
        self.assertEqual(self.workspaces.list(self.actor), [])
        self.assertEqual(self.workers.list_assignments(self.actor), [])

    def test_conflicting_write_lease_is_typed_and_creates_no_second_assignment(self) -> None:
        self._publish_secret()
        first = self.service.prepare(
            thread_id="thread-first",
            execution_id="exec-first",
            project_id=self.project.id,
            sandbox="workspace-write",
            approval_policy="on-request",
        )

        with self.assertRaises(TurnExecutionBindingError) as caught:
            self.service.prepare(
                thread_id="thread-second",
                execution_id="exec-second",
                project_id=self.project.id,
                sandbox="workspace-write",
                approval_policy="on-request",
            )

        self.assertEqual(caught.exception.code, "lease_conflict")
        self.assertEqual(len(self.workspaces.list(self.actor)), 1)
        self.assertEqual(
            self.workspaces.list(self.actor)[0].id,
            first.workspace_id,
        )
        self.assertEqual(len(self.workers.list_assignments(self.actor)), 1)

    def test_explicit_repository_target_is_persisted(self) -> None:
        self._publish_secret()
        second = self.resources.create(
            ResourceCreate(resource_type=ResourceType.REPOSITORY, name="Repository 2"),
            actor=self.actor,
        )
        self.resources.bind_project(
            project=self.project,
            resource_id=second.id,
            actor=self.actor,
        )

        explicit = self.service.prepare(
            thread_id="thread-explicit",
            execution_id="exec-explicit",
            project_id=self.project.id,
            sandbox="workspace-write",
            approval_policy="on-request",
            explicit_repository_id=second.id,
        )
        explicit_assignment = next(
            item
            for item in self.workers.list_assignments(self.actor)
            if item.id == explicit.assignment_id
        )
        self.assertEqual(explicit.repository_resource_id, second.id)
        self.assertEqual(explicit.resource_ids, (second.id,))
        self.assertEqual(
            explicit.repository_target.source,
            RepositoryTargetSource.EXPLICIT,
        )
        self.assertEqual(
            explicit_assignment.repository_target,
            explicit.repository_target,
        )

    def test_conflicting_work_item_and_explicit_targets_fail_before_mutation(self) -> None:
        self._publish_secret()
        second = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Repository 2",
            ),
            actor=self.actor,
        )
        self.resources.bind_project(
            project=self.project,
            resource_id=second.id,
            actor=self.actor,
        )

        with self.assertRaises(TurnExecutionBindingError) as caught:
            self.service.prepare(
                thread_id="thread-work-item-conflict",
                execution_id="exec-work-item-conflict",
                project_id=self.project.id,
                sandbox="workspace-write",
                approval_policy="on-request",
                work_item_resource_ids=(self.repository.id,),
                work_item_ref="group/app#42",
                explicit_repository_id=second.id,
            )

        self.assertEqual(
            caught.exception.code,
            "repository_target_conflict",
        )
        self.assertEqual(
            caught.exception.public()["code"],
            "repository_target_conflict",
        )
        self.assertEqual(self.workspaces.list(self.actor), [])
        self.assertEqual(
            self.workers.list_assignments(self.actor),
            [],
        )
        self.assertEqual(self.backend.provisioned, [])

    def test_converging_work_item_and_explicit_targets_persist_provenance(self) -> None:
        self._publish_secret()
        second = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Repository 2",
            ),
            actor=self.actor,
        )
        self.resources.bind_project(
            project=self.project,
            resource_id=second.id,
            actor=self.actor,
        )

        binding = self.service.prepare(
            thread_id="thread-work-item-converged",
            execution_id="exec-work-item-converged",
            project_id=self.project.id,
            sandbox="workspace-write",
            approval_policy="on-request",
            work_item_resource_ids=(second.id,),
            work_item_ref="group/app#42",
            explicit_repository_id=second.id,
        )
        assignment = next(
            item
            for item in self.workers.list_assignments(self.actor)
            if item.id == binding.assignment_id
        )

        self.assertEqual(
            binding.repository_resource_id,
            second.id,
        )
        self.assertEqual(
            binding.repository_target.source,
            RepositoryTargetSource.WORK_ITEM,
        )
        self.assertEqual(
            [
                item.source
                for item in binding.repository_target.selection_evidence
            ],
            [
                RepositoryTargetSource.WORK_ITEM,
                RepositoryTargetSource.EXPLICIT,
            ],
        )
        self.assertEqual(
            assignment.repository_target,
            binding.repository_target,
        )

    def test_explicit_policy_requires_contextual_target_before_mutation(self) -> None:
        self._publish_secret()
        second = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Repository 2",
            ),
            actor=self.actor,
        )
        self.resources.bind_project(
            project=self.project,
            resource_id=second.id,
            actor=self.actor,
        )
        self.project = self.project.model_copy(
            update={"repository_selection_policy": "explicit"}
        )
        self.projects.project = self.project

        with self.assertRaises(TurnExecutionBindingError) as caught:
            self.service.prepare(
                thread_id="thread-explicit-missing",
                execution_id="exec-explicit-missing",
                project_id=self.project.id,
                sandbox="workspace-write",
                approval_policy="on-request",
            )

        self.assertEqual(
            caught.exception.code,
            "repository_target_missing",
        )
        self.assertEqual(self.workspaces.list(self.actor), [])
        self.assertEqual(
            self.workers.list_assignments(self.actor),
            [],
        )
        self.assertEqual(self.backend.provisioned, [])

        selected = self.service.prepare(
            thread_id="thread-explicit-selected",
            execution_id="exec-explicit-selected",
            project_id=self.project.id,
            sandbox="workspace-write",
            approval_policy="on-request",
            explicit_repository_id=second.id,
        )
        self.assertEqual(
            selected.repository_resource_id,
            second.id,
        )

    def test_explicit_target_resolves_before_non_repository_readiness_blocker(self) -> None:
        self._publish_secret()
        second = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Repository 2",
            ),
            actor=self.actor,
        )
        self.resources.bind_project(
            project=self.project,
            resource_id=second.id,
            actor=self.actor,
        )
        self.project = self.project.model_copy(
            update={"repository_selection_policy": "explicit"}
        )
        self.projects.project = self.project
        self.service.project_readiness = lambda project_id, actor: {
            "execution_ready": False,
            "correlation_id": "readiness-worker-blocked",
            "checks": [
                {
                    "id": "execution:worker",
                    "domain": "execution_worker",
                    "status": "blocked",
                    "code": "worker_capability_missing",
                    "message": "command_execution is unavailable",
                    "required": True,
                    "remediation": "Start a qualified worker.",
                }
            ],
        }

        with self.assertRaises(TurnExecutionBindingError) as caught:
            self.service.prepare(
                thread_id="thread-explicit-worker-blocked",
                execution_id="exec-explicit-worker-blocked",
                project_id=self.project.id,
                sandbox="workspace-write",
                approval_policy="on-request",
                explicit_repository_id=second.id,
            )

        self.assertEqual(
            caught.exception.code,
            "project_readiness_blocked",
        )
        self.assertEqual(
            caught.exception.public()["readiness_code"],
            "worker_capability_missing",
        )
        self.assertEqual(self.workspaces.list(self.actor), [])
        self.assertEqual(
            self.workers.list_assignments(self.actor),
            [],
        )

    def test_explicit_policy_readiness_required_per_turn_allows_selected_target(self) -> None:
        self._publish_secret()
        second = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Repository 2",
            ),
            actor=self.actor,
        )
        self.resources.bind_project(
            project=self.project,
            resource_id=second.id,
            actor=self.actor,
        )
        self.project = self.project.model_copy(
            update={"repository_selection_policy": "explicit"}
        )
        self.projects.project = self.project
        self.service.project_readiness = lambda project_id, actor: {
            "execution_ready": True,
            "correlation_id": "readiness-explicit-ready",
            "checks": [
                {
                    "id": "repository:execution-target",
                    "domain": "execution_target",
                    "status": "ready",
                    "code": "repository_target_required_per_turn",
                    "required": True,
                }
            ],
        }

        binding = self.service.prepare(
            thread_id="thread-explicit-ready",
            execution_id="exec-explicit-ready",
            project_id=self.project.id,
            sandbox="workspace-write",
            approval_policy="on-request",
            explicit_repository_id=second.id,
        )

        self.assertEqual(
            binding.repository_resource_id,
            second.id,
        )
        self.assertEqual(
            binding.repository_target.source,
            RepositoryTargetSource.EXPLICIT,
        )

    def test_read_only_context_is_recorded_without_changing_mutable_repository(self) -> None:
        self._publish_secret()
        second_path = Path(self.temp.name) / "docs-repository"
        second_path.mkdir()
        second = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Docs",
                aliases=[
                    ResourceAlias(
                        namespace="filesystem",
                        value=str(second_path),
                    )
                ],
            ),
            actor=self.actor,
        )
        self.resources.bind_project(
            project=self.project,
            resource_id=second.id,
            actor=self.actor,
        )

        binding = self.service.prepare(
            thread_id="thread-context",
            execution_id="exec-context",
            project_id=self.project.id,
            sandbox="workspace-write",
            approval_policy="on-request",
            explicit_repository_id=self.repository.id,
            read_only_repository_ids=(second.id,),
        )

        self.assertEqual(binding.repository_resource_id, self.repository.id)
        self.assertEqual(
            binding.repository_target.read_only_repository_ids,
            (second.id,),
        )

    def test_single_writable_collection_preserves_single_repository_semantics(self) -> None:
        self._publish_secret()

        binding = self.service.prepare(
            thread_id="thread-single-collection",
            execution_id="exec-single-collection",
            project_id=self.project.id,
            sandbox="workspace-write",
            approval_policy="on-request",
            writable_repository_ids=(self.repository.id,),
        )

        workspace = self.workspaces.get(binding.workspace_id, self.actor)
        assignment = next(
            item
            for item in self.workers.list_assignments(self.actor)
            if item.id == binding.assignment_id
        )
        self.assertEqual(
            binding.repository_scope.write_mode,
            RepositoryWriteMode.SINGLE,
        )
        self.assertEqual(
            binding.repository_scope.writable_repository_ids,
            (self.repository.id,),
        )
        self.assertEqual(binding.repository_resource_id, self.repository.id)
        self.assertEqual(
            workspace.writable_repository_ids,
            (self.repository.id,),
        )
        self.assertEqual(
            assignment.repository_scope,
            binding.repository_scope,
        )

    def test_work_item_coordinated_scope_keeps_selection_provenance(self) -> None:
        self._publish_secret()
        second_path = Path(self.project.path) / "repo-work-item-2"
        second_path.mkdir()
        second = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Work Item repository 2",
                aliases=[
                    ResourceAlias(
                        namespace="filesystem",
                        value=str(second_path),
                    )
                ],
            ),
            actor=self.actor,
        )
        self.resources.bind_project(
            project=self.project,
            resource_id=second.id,
            actor=self.actor,
        )

        binding = self.service.prepare(
            thread_id="thread-work-item-coordinated",
            execution_id="exec-work-item-coordinated",
            project_id=self.project.id,
            sandbox="workspace-write",
            approval_policy="on-request",
            writable_repository_ids=(self.repository.id, second.id),
            writable_repository_source=RepositoryTargetSource.WORK_ITEM,
            work_item_ref="group/app#613",
        )

        self.assertEqual(
            binding.repository_scope.source,
            RepositoryTargetSource.WORK_ITEM,
        )
        self.assertEqual(
            binding.repository_scope.source_ref,
            "group/app#613",
        )
        self.assertTrue(
            all(
                evidence.source == RepositoryTargetSource.WORK_ITEM
                for evidence in binding.repository_scope.selection_evidence
            )
        )

    def test_coordinated_writable_scope_is_persisted_atomically(self) -> None:
        self._publish_secret()
        second_path = Path(self.project.path) / "repo-2"
        second_path.mkdir()
        second = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Repository 2",
                aliases=[
                    ResourceAlias(
                        namespace="filesystem",
                        value=str(second_path),
                    )
                ],
            ),
            actor=self.actor,
        )
        self.resources.bind_project(
            project=self.project,
            resource_id=second.id,
            actor=self.actor,
        )

        binding = self.service.prepare(
            thread_id="thread-coordinated",
            execution_id="exec-coordinated",
            project_id=self.project.id,
            sandbox="workspace-write",
            approval_policy="on-request",
            writable_repository_ids=(self.repository.id, second.id),
        )

        workspace = self.workspaces.get(binding.workspace_id, self.actor)
        assignment = next(
            item
            for item in self.workers.list_assignments(self.actor)
            if item.id == binding.assignment_id
        )
        self.assertEqual(
            binding.repository_scope.write_mode,
            RepositoryWriteMode.COORDINATED,
        )
        self.assertEqual(
            binding.repository_scope.writable_repository_ids,
            (self.repository.id, second.id),
        )
        self.assertEqual(
            workspace.writable_repository_ids,
            (self.repository.id, second.id),
        )
        self.assertEqual(
            assignment.repository_scope,
            binding.repository_scope,
        )
        self.assertEqual(
            assignment.repository_target,
            binding.repository_target,
        )
        self.assertEqual(
            {member.resource_id for member in workspace.repository_members},
            {self.repository.id, second.id},
        )
        self.assertTrue(
            all(
                member.access_mode == LeaseMode.WRITE
                for member in workspace.repository_members
            )
        )

    def test_project_coordinated_policy_supplies_writable_scope(self) -> None:
        self._publish_secret()
        second_path = Path(self.project.path) / "repo-policy-2"
        second_path.mkdir()
        second = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Repository policy 2",
                aliases=[
                    ResourceAlias(
                        namespace="filesystem",
                        value=str(second_path),
                    )
                ],
            ),
            actor=self.actor,
        )
        self.resources.bind_project(
            project=self.project,
            resource_id=second.id,
            actor=self.actor,
        )
        self.project = self.project.model_copy(
            update={"repository_selection_policy": "coordinated"}
        )
        self.projects.project = self.project

        binding = self.service.prepare(
            thread_id="thread-project-coordinated",
            execution_id="exec-project-coordinated",
            project_id=self.project.id,
            sandbox="workspace-write",
            approval_policy="on-request",
        )

        self.assertEqual(
            set(binding.repository_scope.writable_repository_ids),
            {self.repository.id, second.id},
        )
        self.assertEqual(
            binding.repository_scope.write_mode,
            RepositoryWriteMode.COORDINATED,
        )
        self.assertEqual(
            binding.repository_scope.source,
            RepositoryTargetSource.PROJECT_POLICY,
        )
        self.assertEqual(
            binding.repository_scope.source_ref,
            self.project.id,
        )

    def test_invalid_coordinated_scope_fails_before_workspace_or_assignment(self) -> None:
        self._publish_secret()
        unbound_path = Path(self.project.path) / "unbound"
        unbound_path.mkdir()
        unbound = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Unbound repository",
                aliases=[
                    ResourceAlias(
                        namespace="filesystem",
                        value=str(unbound_path),
                    )
                ],
            ),
            actor=self.actor,
        )

        with self.assertRaises(TurnExecutionBindingError) as caught:
            self.service.prepare(
                thread_id="thread-coordinated-invalid",
                execution_id="exec-coordinated-invalid",
                project_id=self.project.id,
                sandbox="workspace-write",
                approval_policy="on-request",
                writable_repository_ids=(self.repository.id, unbound.id),
            )

        self.assertEqual(caught.exception.code, "repository_target_unauthorized")
        self.assertEqual(self.workspaces.list(self.actor), [])
        self.assertEqual(self.workers.list_assignments(self.actor), [])

    def test_deprecated_only_repository_is_not_eligible(self) -> None:
        self._publish_secret()
        self.resources.update(
            self.repository.id,
            ResourceUpdate(lifecycle=ResourceLifecycle.DEPRECATED),
            actor=self.actor,
        )

        with self.assertRaises(TurnExecutionBindingError) as caught:
            self._prepare()

        self.assertEqual(caught.exception.code, "repository_target_missing")
        self.assertIn(
            "no active canonical repository resource",
            str(caught.exception),
        )
        self.assertEqual(self.workspaces.list(self.actor), [])

    def test_existing_execution_with_different_controls_fails_closed(self) -> None:
        self._publish_secret()
        self._prepare(sandbox="workspace-write")

        with self.assertRaisesRegex(
            TurnExecutionBindingError,
            "different execution controls",
        ):
            self._prepare(sandbox="read-only")

        self.assertEqual(len(self.workspaces.list(self.actor)), 1)
        self.assertEqual(len(self.workers.list_assignments(self.actor)), 1)


    def test_per_execution_runtime_binding_is_persisted_and_reuse_is_fail_closed(self) -> None:
        self._publish_secret(
            "secret-anthropic-worker",
            config_key=ANTHROPIC_WORKER_API_KEY_CONFIG,
        )
        selected = ExecutionRuntimeBinding(
            provider_id="anthropic",
            runtime_id="claude-code",
            capability_revision=1,
        )

        first = self.service.prepare(
            thread_id="thread-123",
            execution_id="routed-exec-1",
            project_id=self.project.id,
            sandbox="workspace-write",
            approval_policy="on-request",
            runtime_binding=selected,
        )
        assignment = self.workers.list_assignments(self.actor)[0]

        self.assertEqual(first.runtime_binding, selected)
        self.assertEqual(assignment.runtime_binding, selected)

        with self.assertRaisesRegex(
            TurnExecutionBindingError,
            "different agent runtime",
        ):
            self.service.prepare(
                thread_id="thread-123",
                execution_id="routed-exec-1",
                project_id=self.project.id,
                sandbox="workspace-write",
                approval_policy="on-request",
                runtime_binding=ExecutionRuntimeBinding(
                    provider_id="openai",
                    runtime_id="codex",
                    capability_revision=1,
                ),
            )


    def test_selected_runtime_uses_its_own_credential_reference(self) -> None:
        self._publish_secret(
            "secret-anthropic-worker",
            config_key=ANTHROPIC_WORKER_API_KEY_CONFIG,
        )
        selected = ExecutionRuntimeBinding(
            provider_id="anthropic",
            runtime_id="claude-code",
            capability_revision=1,
        )

        binding = self.service.prepare(
            thread_id="thread-123",
            execution_id="claude-exec-1",
            project_id=self.project.id,
            sandbox="workspace-write",
            approval_policy="on-request",
            runtime_binding=selected,
        )

        assignment = self.workers.list_assignments(self.actor)[0]
        self.assertEqual(binding.secret_ref, "secret-anthropic-worker")
        self.assertEqual(assignment.secret_refs, ("secret-anthropic-worker",))
        self.assertEqual(assignment.runtime_binding, selected)

    def test_binding_contains_reference_metadata_only(self) -> None:
        self._publish_secret("secret-codex-worker")

        binding = self._prepare()
        payload = binding.public()

        self.assertEqual(payload["secret_ref"], "secret-codex-worker")
        self.assertNotIn("secret_value", payload)
        self.assertNotIn("access_token", payload)
        self.assertNotIn("auth_json", payload)
        self.assertNotIn("CODEX_HOME", payload)


if __name__ == "__main__":
    unittest.main()
