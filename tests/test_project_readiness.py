from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.bootstrap_engine import BootstrapExecutionStatus
from codex_web.execution_workers import WorkerCapability
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.models import Project, TaskSourceConfiguration, WorkItemState
from codex_web.project_readiness import ReadinessCheckStatus
from codex_web.services.project_readiness import ProjectReadinessService
from codex_web.services.resources import (
    RepositoryTargetAmbiguousError,
    RepositoryTargetMissingError,
)
from codex_web.storage.project_readiness import ProjectReadinessStore
from codex_web.storage.sqlite_state import SQLiteStateStore


def _actor() -> AuthenticationActor:
    return AuthenticationActor(
        identity_id="admin-a",
        principal_kind=PrincipalKind.HUMAN,
        organization_id="local",
        workspace_id="default",
        roles=(MembershipRole.OWNER,),
        assurance=AuthenticationAssurance.MFA,
    )


class _Projects:
    def __init__(self, project: Project) -> None:
        self.project = project

    def get(self, project_id, scope):
        if (
            project_id != self.project.id
            or scope.organization_id != self.project.organization_id
            or scope.workspace_id != self.project.workspace_id
        ):
            raise LookupError("Project not found")
        return self.project


class _Resource:
    def __init__(self, resource_id: str) -> None:
        self.id = resource_id
        self.resource_type = SimpleNamespace(value="repository")
        self.lifecycle = SimpleNamespace(value="active")


class _Resources:
    def __init__(
        self,
        *,
        repository_ids=("repo-a",),
        target_error=None,
    ) -> None:
        self.repository_ids = tuple(repository_ids)
        self.target_error = target_error

    def project_resources(self, project, *, actor):
        del project, actor
        return [_Resource(value) for value in self.repository_ids]

    def resolve_repository_target(self, project, *, actor):
        del actor
        if self.target_error is not None:
            raise self.target_error
        if not self.repository_ids:
            raise RepositoryTargetMissingError(
                "project has no active canonical repository resource"
            )
        return SimpleNamespace(
            mutable_repository_id=self.repository_ids[0],
            source=SimpleNamespace(value="single_repository"),
        )


class _Reference:
    @staticmethod
    def status():
        return SimpleNamespace(value="active")


class _Secrets:
    def __init__(self, *, available=True, raw="DO_NOT_LEAK") -> None:
        self.available = available
        self.raw = raw

    def metadata(self, secret_id, *, actor, require_use=False):
        del actor, require_use
        if not self.available:
            raise LookupError("not found")
        self.last_id = secret_id
        return _Reference()


class _Workers:
    def __init__(self, *, ready=True) -> None:
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
            else (WorkerCapability.GIT,)
        )
        return SimpleNamespace(
            ready=self.ready,
            code="ready" if self.ready else "worker_capability_missing",
            reason=(
                "eligible execution worker is available"
                if self.ready
                else "command_execution capability is unavailable"
            ),
            remediation=(
                None
                if self.ready
                else "Start a qualified execution worker."
            ),
            required_capabilities=required,
            available_capabilities=available,
            eligible_worker_ids=("worker-a",) if self.ready else (),
        )


class _BootstrapStore:
    def __init__(self, rows=()) -> None:
        self.rows = tuple(rows)

    def executions_for_project(
        self,
        project_id,
        *,
        organization_id,
        workspace_id,
    ):
        del project_id, organization_id, workspace_id
        return self.rows


class ProjectReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sqlite = SQLiteStateStore(
            Path(self.temp.name) / "state.sqlite3"
        )
        self.readiness_store = ProjectReadinessStore(self.sqlite)
        self.project = Project(
            id="project-a",
            organization_id="local",
            workspace_id="default",
            name="Project A",
            path=str(Path(self.temp.name) / "repo"),
        )
        self.work_items = {}

    def service(
        self,
        *,
        resources=None,
        secrets=None,
        workers=None,
        bootstrap=None,
        environment_ready=True,
    ):
        return ProjectReadinessService(
            projects=_Projects(self.project),
            resources=resources or _Resources(),
            secrets=secrets or _Secrets(),
            workers=workers or _Workers(),
            bootstrap=bootstrap or _BootstrapStore(),
            store=self.readiness_store,
            load_work_items=lambda: dict(self.work_items),
            environment_probe=lambda *_args: {
                "available": environment_ready,
                "code": (
                    "sandbox_profile_supported"
                    if environment_ready
                    else "sandbox_profile_unsupported"
                ),
                "reason": (
                    "sandbox supported"
                    if environment_ready
                    else "sandbox cannot be enforced"
                ),
                "remediation": "repair isolation",
            },
            clock=lambda: 1_800_000_000.0,
        )

    def test_fully_canonical_project_without_optional_domains_is_ready(self):
        value = self.service().evaluate(
            self.project.id,
            actor=_actor(),
            record=True,
        )
        self.assertTrue(value.semantic_ready)
        self.assertTrue(value.execution_ready)
        task = next(
            item for item in value.checks
            if item.id == "task-source:binding"
        )
        work_items = next(
            item for item in value.checks
            if item.id == "work-items:resource-associations"
        )
        self.assertEqual(task.status, ReadinessCheckStatus.NOT_APPLICABLE)
        self.assertEqual(
            work_items.status,
            ReadinessCheckStatus.NOT_APPLICABLE,
        )
        self.assertEqual(
            value.last_successful_verification_at,
            value.generated_at,
        )

    def test_missing_repository_blocks_semantic_and_execution_readiness(self):
        value = self.service(
            resources=_Resources(repository_ids=())
        ).evaluate(self.project.id, actor=_actor())
        self.assertFalse(value.semantic_ready)
        self.assertFalse(value.execution_ready)
        codes = {item.code for item in value.blockers}
        self.assertIn("repository_resource_missing", codes)
        self.assertIn("repository_target_missing", codes)

    def test_ambiguous_multi_repository_target_blocks_execution(self):
        value = self.service(
            resources=_Resources(
                repository_ids=("repo-a", "repo-b"),
                target_error=RepositoryTargetAmbiguousError(
                    "multiple repositories require explicit selection"
                ),
            )
        ).evaluate(self.project.id, actor=_actor())
        self.assertFalse(value.execution_ready)
        check = next(
            item for item in value.checks
            if item.id == "repository:execution-target"
        )
        self.assertEqual(check.code, "repository_target_ambiguous")

    def test_required_task_source_missing_is_blocked(self):
        manifest = SimpleNamespace(task_source=SimpleNamespace(type="gitlab"))
        plan = SimpleNamespace(
            version="1.0",
            manifest=manifest,
            operations=(),
        )
        execution = SimpleNamespace(
            id="bootstrap-a",
            status=BootstrapExecutionStatus.APPLIED,
            plan=plan,
            last_error_code=None,
        )
        value = self.service(
            bootstrap=_BootstrapStore((execution,))
        ).evaluate(self.project.id, actor=_actor())
        self.assertIn("task_source_missing", {x.code for x in value.blockers})

    def test_missing_task_source_secret_reference_is_blocked(self):
        self.project = self.project.model_copy(
            update={
                "authoritative_task_source": TaskSourceConfiguration(
                    source_type="gitlab",
                    source_instance="https://gitlab.example/api/v4",
                    scope="group",
                )
            }
        )
        value = self.service().evaluate(
            self.project.id,
            actor=_actor(),
        )
        self.assertIn(
            "task_source_secret_reference_missing",
            {x.code for x in value.blockers},
        )

    def test_unavailable_secret_is_blocked_and_value_never_serializes(self):
        self.project = self.project.model_copy(
            update={
                "authoritative_task_source": TaskSourceConfiguration(
                    source_type="gitlab",
                    source_instance="https://gitlab.example/api/v4",
                    scope="group",
                    credential_secret_id="secret-gitlab",
                )
            }
        )
        secrets = _Secrets(
            available=False,
            raw="RAW_SECRET_DO_NOT_REPORT",
        )
        value = self.service(secrets=secrets).evaluate(
            self.project.id,
            actor=_actor(),
        )
        self.assertIn(
            "secret_reference_missing_or_unauthorized",
            {x.code for x in value.blockers},
        )
        serialized = json.dumps(value.model_dump(mode="json"))
        self.assertNotIn(secrets.raw, serialized)

    def test_missing_command_execution_worker_blocks_execution(self):
        value = self.service(
            workers=_Workers(ready=False)
        ).evaluate(self.project.id, actor=_actor())
        self.assertFalse(value.execution_ready)
        self.assertIn(
            "worker_capability_missing",
            {x.code for x in value.blockers},
        )

    def test_unsupported_execution_environment_blocks_execution(self):
        value = self.service(
            environment_ready=False
        ).evaluate(self.project.id, actor=_actor())
        self.assertFalse(value.execution_ready)
        self.assertIn(
            "sandbox_profile_unsupported",
            {x.code for x in value.blockers},
        )

    def test_unresolved_open_work_item_association_blocks_semantic_readiness(self):
        self.work_items["group/repo#1"] = WorkItemState(
            ref="group/repo#1",
            organization_id="local",
            workspace_id="default",
            project_id=self.project.id,
            resource_ids=[],
            last_meaningful_update_at=1.0,
            created_at=1.0,
            updated_at=1.0,
        )
        value = self.service().evaluate(
            self.project.id,
            actor=_actor(),
        )
        self.assertFalse(value.semantic_ready)
        self.assertIn(
            "work_item_resource_association_unresolved",
            {x.code for x in value.blockers},
        )

    def test_partial_bootstrap_blocks_readiness(self):
        plan = SimpleNamespace(
            version="1.0",
            manifest=SimpleNamespace(task_source=None),
            operations=(),
        )
        execution = SimpleNamespace(
            id="bootstrap-partial",
            status=BootstrapExecutionStatus.PARTIAL,
            plan=plan,
            last_error_code="InjectedFailure",
        )
        value = self.service(
            bootstrap=_BootstrapStore((execution,))
        ).evaluate(self.project.id, actor=_actor())
        self.assertIn(
            "bootstrap_incomplete",
            {x.code for x in value.blockers},
        )


if __name__ == "__main__":
    unittest.main()
