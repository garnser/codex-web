from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.bootstrap_engine import (
    BootstrapExecutionStatus,
    ProjectBootstrapExecution,
)
from codex_web.canonical_materialization import (
    CanonicalMaterializationOperation,
    CanonicalMaterializationPlan,
    MaterializationDisposition,
)
from codex_web.execution_workers import WorkerCapability
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.legacy_project_migration import LegacyMigrationPlan
from codex_web.models import Project, TaskSourceConfiguration
from codex_web.project_bootstrap import parse_project_bootstrap_manifest
from codex_web.services.identity import AuthorizationError
from codex_web.services.project_bootstrap import (
    ProjectBootstrapApprovalRequired,
    ProjectBootstrapBlocked,
    ProjectBootstrapConcurrentApply,
    ProjectBootstrapError,
    ProjectBootstrapPlanStale,
    ProjectBootstrapService,
)
from codex_web.storage.project_bootstrap import ProjectBootstrapStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.workspaces import WorkspaceMapper


class _ProjectRepository:
    def __init__(self, project: Project, root: Path) -> None:
        self.values = [project]
        self.workspace_mapper = WorkspaceMapper(root=root)
        self.save_calls = 0

    def load(self):
        return list(self.values)

    def save(self, values):
        self.values = list(values)
        self.save_calls += 1


class _Projects:
    def __init__(self, repository: _ProjectRepository) -> None:
        self.repository = repository


class _Workers:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready

    def execution_readiness(
        self,
        *,
        required_capabilities,
        execution_contract_version,
        actor,
    ):
        del execution_contract_version, actor
        required = tuple(required_capabilities)
        available = (
            required
            if self.ready
            else tuple(
                item
                for item in required
                if item != WorkerCapability.COMMAND_EXECUTION
            )
        )
        return SimpleNamespace(
            ready=self.ready,
            code=(
                "ready"
                if self.ready
                else "worker_capability_missing"
            ),
            reason=(
                "eligible execution worker is available"
                if self.ready
                else "command_execution capability is unavailable"
            ),
            required_capabilities=required,
            available_capabilities=available,
            eligible_worker_ids=("worker-a",) if self.ready else (),
        )


class _SecretReference:
    def __init__(self, secret_id: str) -> None:
        self.id = secret_id

    @staticmethod
    def status():
        return SimpleNamespace(value="active")


class _Secrets:
    def __init__(self, *, secret_id: str = "gitlab-primary") -> None:
        self.secret_id = secret_id
        self.raw_value = "RAW_SECRET_MUST_NEVER_APPEAR"

    def metadata(self, secret_id, *, actor, require_use=False):
        del actor, require_use
        if secret_id != self.secret_id:
            raise LookupError("secret reference not found")
        return _SecretReference(secret_id)


class _Canonical:
    def __init__(
        self,
        *,
        project_path: Path,
        repository_path: Path,
        repository_apply: bool = False,
        credential_blocked: bool = False,
        candidate: TaskSourceConfiguration | None = None,
    ) -> None:
        self.project_path = project_path
        self.repository_path = repository_path
        self.repository_apply = repository_apply
        self.credential_blocked = credential_blocked
        self.candidate = candidate
        self.apply_calls = 0
        self.last_applied_plan = None

    def derived_gitlab_task_source(self, project_id):
        del project_id
        if self.candidate is None:
            return None, "task_source_not_configured"
        return self.candidate, None

    def plan(
        self,
        project_id,
        *,
        actor,
        confirm_generic_target=False,
        preferred_gitlab_secret_id=None,
    ):
        del confirm_generic_target
        operations = [
            CanonicalMaterializationOperation(
                id="project:scope",
                domain="project_scope",
                record_ref=project_id,
                disposition=MaterializationDisposition.UNCHANGED,
                reason_code="project_scope_canonical",
                message="Project scope is canonical.",
            ),
            CanonicalMaterializationOperation(
                id="repository:root",
                domain="repository_resource",
                record_ref=str(self.repository_path),
                disposition=(
                    MaterializationDisposition.MIGRATED
                    if self.repository_apply
                    else MaterializationDisposition.UNCHANGED
                ),
                reason_code=(
                    "repository_resource_missing"
                    if self.repository_apply
                    else "repository_resource_exists"
                ),
                message=(
                    "Repository will be materialized."
                    if self.repository_apply
                    else "Repository is canonical."
                ),
                apply_kind=(
                    "repository_create"
                    if self.repository_apply
                    else None
                ),
                metadata={
                    "filesystem_path": str(self.repository_path),
                },
            ),
        ]
        if self.credential_blocked:
            if preferred_gitlab_secret_id:
                operations.extend(
                    [
                        CanonicalMaterializationOperation(
                            id="secret:gitlab",
                            domain="secret_reference",
                            record_ref=project_id,
                            disposition=MaterializationDisposition.UNCHANGED,
                            reason_code="gitlab_secret_reference_exists",
                            message="Canonical GitLab SecretReference exists.",
                            metadata={
                                "secret_reference_id": preferred_gitlab_secret_id,
                            },
                        ),
                        CanonicalMaterializationOperation(
                            id="task-source:gitlab",
                            domain="task_source",
                            record_ref=project_id,
                            disposition=MaterializationDisposition.MIGRATED,
                            reason_code="task_source_binding_missing",
                            message="TaskSource will be materialized.",
                            apply_kind="gitlab_task_source",
                            dependencies=("secret:gitlab",),
                            metadata={
                                "source_type": "gitlab",
                                "source_instance": "https://gitlab.example/api/v4",
                                "scope": "group",
                                "secret_reference_id": preferred_gitlab_secret_id,
                            },
                        ),
                    ]
                )
            else:
                operations.extend(
                    [
                        CanonicalMaterializationOperation(
                            id="secret:gitlab",
                            domain="secret_reference",
                            record_ref=project_id,
                            disposition=MaterializationDisposition.UNRESOLVED,
                            reason_code="gitlab_credential_missing",
                            message="GitLab credential is unresolved.",
                        ),
                        CanonicalMaterializationOperation(
                            id="task-source:gitlab",
                            domain="task_source",
                            record_ref=project_id,
                            disposition=MaterializationDisposition.UNRESOLVED,
                            reason_code="task_source_credential_unresolved",
                            message="TaskSource credential is unresolved.",
                        ),
                    ]
                )
        return CanonicalMaterializationPlan(
            id=(
                "materialization-plan-"
                + ("apply" if self.repository_apply else "ready")
                + ("-credential" if self.credential_blocked else "")
            ),
            project_id=project_id,
            source_organization_id=actor.organization_id,
            source_workspace_id=actor.workspace_id,
            target_organization_id=actor.organization_id,
            target_workspace_id=actor.workspace_id,
            project_path=str(self.project_path),
            operations=tuple(operations),
            generated_at=1.0,
        )

    def apply(self, plan, *, actor):
        del actor
        self.apply_calls += 1
        self.last_applied_plan = plan
        return SimpleNamespace(id="canonical-execution-a")


class _Legacy:
    def __init__(self, project_path: Path) -> None:
        self.project_path = project_path
        self.apply_calls = 0

    def plan(self, project_id, *, actor):
        return LegacyMigrationPlan(
            id="migration-plan-ready",
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            project_id=project_id,
            project_path=str(self.project_path),
            repositories=(),
            threads=(),
            blockers=(),
            generated_at=1.0,
        )

    def apply(
        self,
        plan,
        *,
        actor,
        approve_material_authority_changes=False,
    ):
        del plan, actor, approve_material_authority_changes
        self.apply_calls += 1
        return SimpleNamespace(id="legacy-execution-a")


def _actor(
    organization_id: str = "local",
    workspace_id: str = "default",
) -> AuthenticationActor:
    return AuthenticationActor(
        identity_id="admin-a",
        principal_kind=PrincipalKind.HUMAN,
        organization_id=organization_id,
        workspace_id=workspace_id,
        roles=(MembershipRole.OWNER,),
        assurance=AuthenticationAssurance.MFA,
    )


class ProjectBootstrapEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "workspace"
        self.repo = self.workspace / "repo"
        self.repo.mkdir(parents=True)
        (self.repo / ".git").mkdir()

        self.project = Project(
            id="project-a",
            organization_id="local",
            workspace_id="default",
            name="Demo",
            path=str(self.repo),
            sandbox="workspace-write",
        )
        self.repository = _ProjectRepository(
            self.project,
            self.workspace,
        )
        self.projects = _Projects(self.repository)
        self.sqlite = SQLiteStateStore(
            Path(self.temp.name) / "state.sqlite3"
        )
        self.store = ProjectBootstrapStore(self.sqlite)
        self.secrets = _Secrets()
        self.canonical = _Canonical(
            project_path=self.repo,
            repository_path=self.repo,
        )
        self.legacy = _Legacy(self.repo)
        self.clock_value = 1000.0

    def manifest(
        self,
        *,
        name: str = "Demo",
        sandbox: str = "workspace-write",
        task_source: bool = False,
        repository_selection: str = "single",
    ):
        payload = {
            "apiVersion": "codex-web/v1",
            "kind": "ProjectBootstrap",
            "project": {
                "name": name,
                "organization": "local",
                "workspace": "default",
            },
            "repositories": [
                {
                    "id": "repo",
                    "path": str(self.repo),
                    "default": True,
                }
            ],
            "execution": {
                "repositorySelection": repository_selection,
                "requiredCapabilities": ["command_execution"],
                "sandbox": sandbox,
            },
        }
        if task_source:
            payload["taskSource"] = {
                "type": "gitlab",
                "authoritative": True,
                "secretRef": "gitlab-primary",
            }
        return parse_project_bootstrap_manifest(payload)

    def service(
        self,
        *,
        workers_ready: bool = True,
        task_source_health=None,
        readiness_probe=None,
        authorization_check=None,
    ) -> ProjectBootstrapService:
        return ProjectBootstrapService(
            projects=self.projects,
            resources=SimpleNamespace(),
            secrets=self.secrets,
            workers=_Workers(ready=workers_ready),
            state_store=self.sqlite,
            canonical_materialization=self.canonical,
            legacy_migration=self.legacy,
            store=self.store,
            task_source_health=task_source_health,
            environment_health=lambda *_args: {
                "available": True,
                "code": "environment_ready",
                "reason": "environment ready",
            },
            readiness_probe=readiness_probe,
            authorization_check=authorization_check,
            clock=lambda: self.clock_value,
        )

    def test_preflight_is_read_only(self):
        service = self.service()
        manifest = self.manifest()
        state_before = self.sqlite.documents()
        projects_before = self.repository.load()
        saves_before = self.repository.save_calls

        value = service.preflight(
            self.project.id,
            manifest,
            actor=_actor(),
            migrate_legacy=False,
        )

        self.assertFalse(value.blocked)
        self.assertEqual(self.sqlite.documents(), state_before)
        self.assertEqual(self.repository.load(), projects_before)
        self.assertEqual(self.repository.save_calls, saves_before)
        self.assertEqual(self.canonical.apply_calls, 0)
        self.assertEqual(self.legacy.apply_calls, 0)

    def test_equivalent_snapshot_produces_same_plan_id(self):
        service = self.service()
        manifest = self.manifest()
        first = service.plan(
            self.project.id,
            manifest,
            actor=_actor(),
            migrate_legacy=False,
        )
        self.clock_value += 500.0
        second = service.plan(
            self.project.id,
            manifest,
            actor=_actor(),
            migrate_legacy=False,
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.snapshot_digest, second.snapshot_digest)

    def test_stale_plan_is_rejected_before_apply(self):
        service = self.service()
        plan = service.plan(
            self.project.id,
            self.manifest(),
            actor=_actor(),
            migrate_legacy=False,
        )
        self.repository.values[0] = self.project.model_copy(
            update={"name": "Changed elsewhere"}
        )
        with self.assertRaises(ProjectBootstrapPlanStale):
            service.apply(plan, actor=_actor())

    def test_crash_after_each_operation_boundary_resumes_idempotently(self):
        self.canonical.repository_apply = True
        manifest = self.manifest(
            name="Updated",
            sandbox="read-only",
        )

        for fail_after in (1, 2, 3):
            with self.subTest(fail_after=fail_after):
                self.repository.values = [self.project]
                self.repository.save_calls = 0
                self.sqlite.delete(ProjectBootstrapStore.NAMESPACE)
                self.canonical.apply_calls = 0
                service = self.service(
                    readiness_probe=lambda *_args: {
                        "ready": False,
                        "code": "semantic_readiness_pending",
                    }
                )
                plan = service.plan(
                    self.project.id,
                    manifest,
                    actor=_actor(),
                    migrate_legacy=False,
                )
                self.assertEqual(
                    len(plan.applicable_operations),
                    3,
                )

                with self.assertRaises(ProjectBootstrapError):
                    service.apply(
                        plan,
                        actor=_actor(),
                        fail_after_operations=fail_after,
                    )

                partial = service.status(
                    self.project.id,
                    actor=_actor(),
                )[0]
                self.assertEqual(
                    partial.status,
                    BootstrapExecutionStatus.PARTIAL,
                )
                self.assertEqual(
                    len(partial.completed_operation_ids),
                    fail_after,
                )
                self.assertEqual(
                    len(partial.audit_events),
                    fail_after,
                )

                resumed = service.apply(plan, actor=_actor())
                repeated = service.apply(plan, actor=_actor())
                self.assertEqual(
                    resumed.status,
                    BootstrapExecutionStatus.APPLIED,
                )
                self.assertEqual(repeated.id, resumed.id)
                self.assertEqual(self.canonical.apply_calls, 1)
                self.assertEqual(len(resumed.audit_events), 3)
                self.assertEqual(
                    self.repository.load()[0].name,
                    "Updated",
                )
                self.assertEqual(
                    self.repository.load()[0].sandbox,
                    "read-only",
                )
                self.assertFalse(resumed.readiness["ready"])

    def test_concurrent_apply_is_rejected(self):
        service = self.service()
        plan = service.plan(
            self.project.id,
            self.manifest(name="Updated"),
            actor=_actor(),
            migrate_legacy=False,
        )
        active = ProjectBootstrapExecution(
            id="bootstrap-execution-other",
            plan_id="bootstrap-plan-other",
            project_id=self.project.id,
            organization_id="local",
            workspace_id="default",
            manifest_digest=plan.manifest_digest,
            plan=plan,
            status=BootstrapExecutionStatus.APPLYING,
            lease_owner="other-executor",
            lease_expires_at=self.clock_value + 100,
            created_at=self.clock_value,
            updated_at=self.clock_value,
        )
        self.store.update(
            lambda state: state.model_copy(
                update={"executions": [active]}
            )
        )

        with self.assertRaises(ProjectBootstrapConcurrentApply):
            service.apply(plan, actor=_actor())

    def test_authorization_loss_mid_run_stops_checkpoint(self):
        self.canonical.repository_apply = True
        service = self.service()
        plan = service.plan(
            self.project.id,
            self.manifest(),
            actor=_actor(),
            migrate_legacy=False,
        )
        partial = ProjectBootstrapExecution(
            id="bootstrap-execution-resume",
            plan_id=plan.id,
            project_id=self.project.id,
            organization_id="local",
            workspace_id="default",
            manifest_digest=plan.manifest_digest,
            plan=plan,
            status=BootstrapExecutionStatus.PARTIAL,
            created_at=self.clock_value,
            updated_at=self.clock_value,
        )
        self.store.update(
            lambda state: state.model_copy(
                update={"executions": [partial]}
            )
        )
        calls = {"count": 0}

        def authorization(actor):
            calls["count"] += 1
            if calls["count"] >= 3:
                raise AuthorizationError("membership revoked")
            self.assertEqual(actor.identity_id, "admin-a")

        service.authorization_check = authorization
        with self.assertRaises(AuthorizationError):
            service.apply(plan, actor=_actor())

        current = service.store.executions_for_project(
            self.project.id,
            organization_id="local",
            workspace_id="default",
        )[0]
        self.assertEqual(
            current.status,
            BootstrapExecutionStatus.PARTIAL,
        )
        self.assertEqual(current.audit_events, ())
        self.assertEqual(self.canonical.apply_calls, 1)

    def test_tenant_mismatch_is_blocked(self):
        service = self.service()
        with self.assertRaises(ProjectBootstrapBlocked):
            service.plan(
                self.project.id,
                self.manifest(),
                actor=_actor("other-org", "other-workspace"),
                migrate_legacy=False,
            )

    def test_worker_capability_gap_blocks_preflight(self):
        service = self.service(workers_ready=False)
        value = service.preflight(
            self.project.id,
            self.manifest(),
            actor=_actor(),
            migrate_legacy=False,
        )
        worker = next(
            item
            for item in value.checks
            if item.id == "execution:worker"
        )
        self.assertEqual(worker.disposition.value, "blocked")
        self.assertEqual(
            worker.reason_code,
            "worker_capability_missing",
        )

    def test_explicit_secret_reference_supersedes_legacy_credential_gap(self):
        self.canonical.credential_blocked = True
        self.canonical.repository_apply = True
        self.canonical.candidate = TaskSourceConfiguration(
            source_type="gitlab",
            source_instance="https://gitlab.example/api/v4",
            scope="group",
        )
        service = self.service(
            task_source_health=lambda *_args: {
                "available": False,
                "code": "gitlab_temporarily_unreachable",
            },
            readiness_probe=lambda *_args: {
                "ready": False,
                "code": "project_readiness_blocked",
            },
        )
        manifest = self.manifest(task_source=True)
        plan = service.plan(
            self.project.id,
            manifest,
            actor=_actor(),
            migrate_legacy=False,
        )

        self.assertFalse(plan.blockers)
        warning = next(
            item
            for item in plan.operations
            if item.reason_code == "gitlab_temporarily_unreachable"
        )
        self.assertEqual(warning.disposition.value, "warning")
        direct = next(
            item
            for item in plan.operations
            if item.id == "task-source:manifest"
        )
        self.assertEqual(direct.disposition.value, "ready")
        delegated_task_source = next(
            item
            for item in plan.operations
            if item.id == "canonical:task-source:gitlab"
        )
        self.assertEqual(
            delegated_task_source.disposition.value,
            "migrate",
        )

        execution = service.apply(plan, actor=_actor())
        delegated = self.canonical.last_applied_plan
        self.assertIsNotNone(delegated)
        delegated_by_id = {
            item.id: item for item in delegated.operations
        }
        self.assertIsNone(
            delegated_by_id["secret:gitlab"].apply_kind
        )
        self.assertEqual(
            delegated_by_id["secret:gitlab"].metadata[
                "secret_reference_id"
            ],
            "gitlab-primary",
        )
        self.assertEqual(
            delegated_by_id["task-source:gitlab"].apply_kind,
            "gitlab_task_source",
        )
        self.assertEqual(
            delegated_by_id["task-source:gitlab"].metadata[
                "secret_reference_id"
            ],
            "gitlab-primary",
        )
        self.assertIn(
            "gitlab_temporarily_unreachable",
            execution.warnings,
        )
        self.assertFalse(execution.readiness["ready"])
        serialized = json.dumps(
            execution.model_dump(mode="json"),
            sort_keys=True,
        )
        self.assertNotIn(self.secrets.raw_value, serialized)

    def test_multi_repository_policies_round_trip_through_bootstrap(self):
        service = self.service()
        for mode in ("explicit", "coordinated"):
            with self.subTest(mode=mode):
                manifest = self.manifest(repository_selection=mode)
                plan = service.plan(
                    self.project.id,
                    manifest,
                    actor=_actor(),
                    migrate_legacy=False,
                )

                policy = next(
                    item
                    for item in plan.operations
                    if item.id == "project:settings:repository-selection"
                )
                self.assertEqual(policy.disposition.value, "update")
                self.assertEqual(
                    policy.desired["repository_selection_policy"],
                    mode,
                )

                execution = service.apply(plan, actor=_actor())
                self.assertEqual(
                    execution.status,
                    BootstrapExecutionStatus.APPLIED,
                )
                project = self.repository.load()[0]
                self.assertEqual(
                    project.repository_selection_policy,
                    mode,
                )

                repeat = service.plan(
                    self.project.id,
                    manifest,
                    actor=_actor(),
                    migrate_legacy=False,
                )
                policy = next(
                    item
                    for item in repeat.operations
                    if item.id == "project:settings:repository-selection"
                )
                self.assertEqual(policy.disposition.value, "ready")

    def test_single_and_default_manifest_modes_persist_deterministic_policy(self):
        self.repository.values[0] = self.project.model_copy(
            update={"repository_selection_policy": "explicit"}
        )
        service = self.service()

        for mode in ("single", "default"):
            with self.subTest(mode=mode):
                plan = service.plan(
                    self.project.id,
                    self.manifest(repository_selection=mode),
                    actor=_actor(),
                    migrate_legacy=False,
                )
                service.apply(plan, actor=_actor())
                self.assertEqual(
                    self.repository.load()[0].repository_selection_policy,
                    "deterministic",
                )
                self.repository.values[0] = (
                    self.repository.values[0].model_copy(
                        update={
                            "repository_selection_policy": "explicit"
                        }
                    )
                )

    def test_danger_full_access_requires_explicit_approval(self):
        service = self.service()
        plan = service.plan(
            self.project.id,
            self.manifest(sandbox="danger-full-access"),
            actor=_actor(),
            migrate_legacy=False,
        )
        sandbox = next(
            item
            for item in plan.operations
            if item.id == "project:settings:sandbox"
        )
        self.assertTrue(sandbox.operator_action_required)
        with self.assertRaises(ProjectBootstrapApprovalRequired):
            service.apply(plan, actor=_actor())

        applied = service.apply(
            plan,
            actor=_actor(),
            approve_authority_changes=True,
        )
        self.assertEqual(
            applied.status,
            BootstrapExecutionStatus.APPLIED,
        )
        self.assertEqual(
            self.repository.load()[0].sandbox,
            "danger-full-access",
        )


if __name__ == "__main__":
    unittest.main()
