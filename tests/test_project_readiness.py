from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.bootstrap_engine import BootstrapExecutionStatus
from codex_web.execution_workers import (
    CodexExecutionAuthenticationMode,
    ExecutionRuntimeBinding,
    WorkerCapability,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.models import Project, TaskSourceConfiguration, WorkItemState
from codex_web.project_readiness import ReadinessCheckStatus
from codex_web.resources import ResourceLifecycle, ResourceType
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
        self.resource_type = ResourceType.REPOSITORY
        self.lifecycle = ResourceLifecycle.ACTIVE


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
    def __init__(self, status="active") -> None:
        self._status = status

    def status(self):
        return SimpleNamespace(value=self._status)


class _Secrets:
    def __init__(
        self,
        *,
        available=True,
        status="active",
        raw="DO_NOT_LEAK",
    ) -> None:
        self.available = available
        self.status = status
        self.raw = raw

    def metadata(self, secret_id, *, actor, require_use=False):
        del actor, require_use
        if not self.available:
            raise LookupError("not found")
        self.last_id = secret_id
        return _Reference(self.status)


class _Configuration:
    def __init__(self, *, value=None, available=True) -> None:
        self.value = value
        self.available = available
        self.calls = []

    def resolve(self, key, context):
        self.calls.append((key, context))
        if not self.available:
            raise LookupError("configuration not found")
        return SimpleNamespace(value=self.value)


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
        configuration=None,
        runtime_binding=None,
        runtime_credential_configs=None,
        permitted_codex_authentication_modes=None,
        local_session_probe=None,
    ):
        return ProjectReadinessService(
            projects=_Projects(self.project),
            resources=resources or _Resources(),
            secrets=secrets or _Secrets(),
            workers=workers or _Workers(),
            bootstrap=bootstrap or _BootstrapStore(),
            store=self.readiness_store,
            load_work_items=lambda: dict(self.work_items),
            configuration=configuration,
            runtime_binding=runtime_binding,
            runtime_credential_configs=runtime_credential_configs,
            permitted_codex_authentication_modes=(
                permitted_codex_authentication_modes
            ),
            local_session_probe=local_session_probe,
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

    def test_required_codex_credential_is_visible_before_execution(self):
        runtime = ExecutionRuntimeBinding(
            provider_id="openai",
            runtime_id="codex",
            capability_revision=1,
        )
        value = self.service(
            configuration=_Configuration(available=False),
            runtime_binding=runtime,
        ).evaluate(self.project.id, actor=_actor())

        self.assertFalse(value.execution_ready)
        blocker = next(
            item for item in value.blockers
            if item.id == "runtime:credential-reference"
        )
        self.assertEqual(blocker.code, "credential_reference_missing")
        self.assertEqual(
            blocker.affected_id,
            "codex.worker.access_token_secret",
        )
        self.assertEqual(blocker.remediation_route, "/api/configuration")
        self.assertNotIn("DO_NOT_LEAK", json.dumps(blocker.model_dump(mode="json")))

    def test_configured_codex_credential_clears_readiness_blocker(self):
        runtime = ExecutionRuntimeBinding(
            provider_id="openai",
            runtime_id="codex",
            capability_revision=1,
        )
        value = self.service(
            configuration=_Configuration(
                value={"kind": "secret", "secret_id": "secret-codex"}
            ),
            runtime_binding=runtime,
        ).evaluate(self.project.id, actor=_actor())

        check = next(
            item for item in value.checks
            if item.id == "runtime:credential-reference"
        )
        self.assertEqual(check.status, ReadinessCheckStatus.READY)
        self.assertEqual(check.code, "credential_reference_ready")
        self.assertTrue(value.execution_ready)

    def test_expired_and_revoked_runtime_authentication_are_distinct(self):
        for status, expected in (
            ("expired", "authentication_expired"),
            ("revoked", "authentication_revoked"),
        ):
            with self.subTest(status=status):
                runtime = ExecutionRuntimeBinding(
                    provider_id="openai",
                    runtime_id="codex",
                    capability_revision=1,
                    authentication_mode="delegated_worker_token",
                )
                value = self.service(
                    secrets=_Secrets(status=status),
                    configuration=_Configuration(
                        value={"kind": "secret", "secret_id": "secret-codex"}
                    ),
                    runtime_binding=runtime,
                ).evaluate(self.project.id, actor=_actor())

                blocker = next(
                    item
                    for item in value.blockers
                    if item.id == "runtime:credential-reference"
                )
                self.assertEqual(blocker.code, expected)
                self.assertEqual(
                    blocker.details["authentication_status"],
                    status,
                )
                self.assertFalse(value.execution_ready)

    def test_api_key_readiness_uses_api_key_configuration_contract(self):
        runtime = ExecutionRuntimeBinding(
            provider_id="openai",
            runtime_id="codex",
            capability_revision=1,
            authentication_mode="api_key",
        )
        configuration = _Configuration(
            value={"kind": "secret", "secret_id": "secret-api-key"}
        )
        value = self.service(
            configuration=configuration,
            runtime_binding=runtime,
        ).evaluate(self.project.id, actor=_actor())

        check = next(
            item for item in value.checks
            if item.id == "runtime:credential-reference"
        )
        self.assertEqual(check.code, "credential_reference_ready")
        self.assertEqual(
            check.details["credential_configuration_key"],
            "codex.worker.api_key_secret",
        )
        self.assertEqual(
            configuration.calls[-1][0],
            "codex.worker.api_key_secret",
        )

    def test_trusted_local_session_readiness_distinguishes_support_and_availability(self):
        runtime = ExecutionRuntimeBinding(
            provider_id="openai",
            runtime_id="codex",
            capability_revision=1,
            authentication_mode="trusted_local_session",
        )
        unsupported = self.service(
            runtime_binding=runtime,
        ).evaluate(self.project.id, actor=_actor())
        supported = self.service(
            runtime_binding=runtime,
            local_session_probe=lambda: True,
        ).evaluate(self.project.id, actor=_actor())

        unsupported_check = next(
            item for item in unsupported.checks
            if item.id == "runtime:credential-reference"
        )
        supported_check = next(
            item for item in supported.checks
            if item.id == "runtime:credential-reference"
        )
        self.assertEqual(
            unsupported_check.code,
            "authentication_mode_unsupported",
        )
        self.assertEqual(supported_check.code, "local_session_available")
        self.assertTrue(supported.execution_ready)

    def test_authentication_policy_denial_is_explicit(self):
        runtime = ExecutionRuntimeBinding(
            provider_id="openai",
            runtime_id="codex",
            capability_revision=1,
            authentication_mode="api_key",
        )
        value = self.service(
            runtime_binding=runtime,
            permitted_codex_authentication_modes=(
                CodexExecutionAuthenticationMode.DELEGATED_WORKER_TOKEN,
            ),
        ).evaluate(self.project.id, actor=_actor())

        blocker = next(
            item for item in value.blockers
            if item.id == "runtime:credential-reference"
        )
        self.assertEqual(blocker.code, "authentication_mode_denied")
        self.assertEqual(
            blocker.details["authentication_status"],
            "denied",
        )

    def test_runtime_without_credential_requirement_is_not_globally_blocked(self):
        runtime = ExecutionRuntimeBinding(
            provider_id="local",
            runtime_id="credentialless",
            capability_revision=1,
        )
        value = self.service(
            configuration=_Configuration(available=False),
            runtime_binding=runtime,
            runtime_credential_configs={},
        ).evaluate(self.project.id, actor=_actor())

        check = next(
            item for item in value.checks
            if item.id == "runtime:credential-reference"
        )
        self.assertEqual(check.status, ReadinessCheckStatus.NOT_APPLICABLE)
        self.assertEqual(check.code, "credential_reference_not_required")
        self.assertTrue(value.execution_ready)

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

    def test_explicit_multi_repository_policy_is_ready_without_default(self):
        self.project = self.project.model_copy(
            update={"repository_selection_policy": "explicit"}
        )
        resources = _Resources(
            repository_ids=("repo-a", "repo-b"),
            target_error=RepositoryTargetAmbiguousError(
                "must not be called for explicit policy"
            ),
        )

        value = self.service(resources=resources).evaluate(
            self.project.id,
            actor=_actor(),
        )

        self.assertTrue(value.semantic_ready)
        self.assertTrue(value.execution_ready)
        check = next(
            item for item in value.checks
            if item.id == "repository:execution-target"
        )
        self.assertEqual(
            check.code,
            "repository_target_required_per_turn",
        )
        self.assertEqual(check.status, ReadinessCheckStatus.READY)
        self.assertEqual(check.details["policy"], "explicit")
        self.assertEqual(
            check.details["target_resolution"],
            "required_per_turn",
        )
        self.assertEqual(check.details["repository_count"], 2)
        self.assertNotIn("mutable_repository_id", check.details)

    def test_explicit_policy_still_blocks_when_no_repository_exists(self):
        self.project = self.project.model_copy(
            update={"repository_selection_policy": "explicit"}
        )
        value = self.service(
            resources=_Resources(repository_ids=())
        ).evaluate(self.project.id, actor=_actor())

        self.assertFalse(value.semantic_ready)
        self.assertFalse(value.execution_ready)
        target = next(
            item for item in value.checks
            if item.id == "repository:execution-target"
        )
        self.assertEqual(target.code, "repository_target_missing")
        self.assertEqual(target.status, ReadinessCheckStatus.BLOCKED)
        self.assertEqual(target.details["policy"], "explicit")

    def test_coordinated_policy_uses_complete_project_repository_set(self):
        self.project = self.project.model_copy(
            update={"repository_selection_policy": "coordinated"}
        )
        resources = _Resources(
            repository_ids=("repo-a", "repo-b"),
            target_error=RepositoryTargetAmbiguousError(
                "must not resolve a singular target for coordinated policy"
            ),
        )

        value = self.service(resources=resources).evaluate(
            self.project.id,
            actor=_actor(),
        )

        self.assertTrue(value.semantic_ready)
        self.assertTrue(value.execution_ready)
        check = next(
            item for item in value.checks
            if item.id == "repository:execution-target"
        )
        self.assertEqual(check.code, "repository_coordinated_scope_ready")
        self.assertEqual(check.status, ReadinessCheckStatus.READY)
        self.assertEqual(check.details["policy"], "coordinated")
        self.assertEqual(
            check.details["target_resolution"],
            "project_repository_set",
        )
        self.assertEqual(check.details["repository_ids"], ["repo-a", "repo-b"])

    def test_single_repository_default_policy_remains_deterministic(self):
        value = self.service().evaluate(
            self.project.id,
            actor=_actor(),
        )
        check = next(
            item for item in value.checks
            if item.id == "repository:execution-target"
        )
        self.assertEqual(
            self.project.repository_selection_policy,
            "deterministic",
        )
        self.assertEqual(
            check.code,
            "repository_execution_target_ready",
        )
        self.assertEqual(check.details["policy"], "deterministic")
        self.assertEqual(
            check.details["mutable_repository_id"],
            "repo-a",
        )

    def test_tenant_isolation_precedes_repository_policy(self):
        self.project = self.project.model_copy(
            update={"repository_selection_policy": "explicit"}
        )
        foreign = _actor(
        ).model_copy(
            update={
                "organization_id": "other",
                "workspace_id": "other",
            }
        )
        with self.assertRaises(LookupError):
            self.service(
                resources=_Resources(
                    repository_ids=("repo-a", "repo-b")
                )
            ).evaluate(self.project.id, actor=foreign)

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
