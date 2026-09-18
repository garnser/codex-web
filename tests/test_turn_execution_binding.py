from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.configuration import (
    ConfigurationDraftCreate,
    ConfigurationPublishRequest,
    ConfigurationScope,
    SecretReference,
)
from codex_web.execution_subjects import ExecutionSubjectKind
from codex_web.execution_workspace_backend import GitWorkspaceProvision
from codex_web.execution_workers import WorkerCapability
from codex_web.models import Project
from codex_web.resources import (
    ResourceCreate,
    ResourceLifecycle,
    ResourceType,
    ResourceUpdate,
)
from codex_web.services.codex_worker_configuration import (
    CODEX_WORKER_ACCESS_TOKEN_CONFIG,
    install_codex_worker_configuration,
)
from codex_web.services.configuration import ConfigurationService
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
from codex_web.storage.execution_workers import ExecutionWorkerStore
from codex_web.storage.execution_workspaces import ExecutionWorkspaceStateStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.sqlite_state import SQLiteStateStore


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
        self.clock = 1_800_000_000.0
        self.service = TurnExecutionBindingService(
            self.configuration,
            self.projects,
            self.resources,
            self.workspaces,
            self.workers,
            control_actor=self.actor,
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
    ) -> None:
        draft = self.configuration.create_draft(
            ConfigurationDraftCreate(
                key=CODEX_WORKER_ACCESS_TOKEN_CONFIG,
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
        self.assertEqual(set(binding.resource_ids), {self.repository.id, self.service_resource.id})
        self.assertEqual(assignment.resource_ids, workspace.resource_ids)
        self.assertEqual(binding.repository_resource_id, self.repository.id)
        self.assertEqual(assignment.base_revision, workspace.base_revision)
        self.assertEqual(workspace.base_revision, "base-revision")
        self.assertEqual(assignment.secret_refs, ("secret-codex-worker",))
        self.assertEqual(binding.secret_ref, "secret-codex-worker")
        self.assertFalse(assignment.network.enabled)
        self.assertEqual(binding.deadline_at, self.clock + 900)
        self.assertEqual(len(self.backend.provisioned), 1)

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
            lease.expires_at,
            self.clock + THREAD_BOOTSTRAP_SESSION_SECONDS,
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
        with self.assertRaisesRegex(
            TurnExecutionBindingError,
            "credential reference configuration is unavailable",
        ):
            self._prepare()

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

        with self.assertRaisesRegex(
            TurnExecutionBindingError,
            "multiple active canonical repository resources",
        ):
            self._prepare()

        self.assertEqual(self.workspaces.list(self.actor), [])
        self.assertEqual(self.workers.list_assignments(self.actor), [])

    def test_deprecated_only_repository_is_not_eligible(self) -> None:
        self._publish_secret()
        self.resources.update(
            self.repository.id,
            ResourceUpdate(lifecycle=ResourceLifecycle.DEPRECATED),
            actor=self.actor,
        )

        with self.assertRaisesRegex(
            TurnExecutionBindingError,
            "no active canonical repository resource",
        ):
            self._prepare()

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
