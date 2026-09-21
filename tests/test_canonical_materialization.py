from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_web.canonical_materialization import (
    MaterializationDisposition,
    MaterializationExecutionStatus,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
    TenantScope,
)
from codex_web.models import (
    GitLabProjectRoutingSettings,
    GitLabRoutingSettings,
    Project,
    WorkItemState,
)
from codex_web.secret_backends import LocalFileSecretBackend
from codex_web.services.canonical_materialization import (
    CanonicalMaterializationError,
    CanonicalMaterializationService,
)
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.secrets import SecretBroker
from codex_web.storage.canonical_materialization import (
    CanonicalMaterializationStore,
)
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _ProjectRepository:
    def __init__(self, project: Project) -> None:
        self.values = [project]

    def load(self):
        return list(self.values)

    def save(self, values):
        self.values = list(values)


class _Projects:
    def __init__(self, project: Project) -> None:
        self.repository = _ProjectRepository(project)

    def set_authoritative_task_source(
        self,
        project_id,
        source,
        scope=None,
    ):
        for index, project in enumerate(self.repository.values):
            if project.id != project_id:
                continue
            if scope is not None and (
                project.organization_id != scope.organization_id
                or project.workspace_id != scope.workspace_id
            ):
                raise LookupError("Project not found")
            updated = project.model_copy(
                update={"authoritative_task_source": source}
            )
            self.repository.values[index] = updated
            return updated
        raise LookupError("Project not found")


class _WorkItems:
    def __init__(self, values=()):
        self.values = {item.ref: item for item in values}
        self.put_calls = []

    def load(self):
        return dict(self.values)

    def get(self, ref):
        return self.values.get(ref)

    def put(self, ref, value):
        self.values[ref] = value
        self.put_calls.append(ref)
        return value


def _actor(
    *,
    organization_id="org-a",
    workspace_id="ws-a",
) -> AuthenticationActor:
    return AuthenticationActor(
        identity_id="admin-a",
        principal_kind=PrincipalKind.HUMAN,
        organization_id=organization_id,
        workspace_id=workspace_id,
        roles=(MembershipRole.OWNER,),
        assurance=AuthenticationAssurance.MFA,
    )


class CanonicalMaterializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.repo = self.root / "app"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()

        self.project = Project(
            id="project-a",
            organization_id="local",
            workspace_id="default",
            name="Legacy project",
            path=str(self.root),
        )
        self.projects = _Projects(self.project)
        self.actor = _actor()
        self.sqlite = SQLiteStateStore(
            Path(self.temp.name) / "state.sqlite3"
        )
        self.resources = ResourceCatalogService(
            ResourceCatalogStore(self.sqlite)
        )
        self.secrets = SecretBroker(
            SecretStateStore(self.sqlite),
            {
                "local": LocalFileSecretBackend(
                    Path(self.temp.name) / "secrets"
                )
            },
        )
        self.work_item = WorkItemState(
            ref="group/app#1",
            project_id=self.project.id,
            organization_id="local",
            workspace_id="default",
            resource_ids=[],
            project_path=str(self.repo),
            last_meaningful_update_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )
        self.work_items = _WorkItems((self.work_item,))
        self.routing = GitLabRoutingSettings(
            projects={
                self.project.id: GitLabProjectRoutingSettings(
                    project_paths=["group/app"]
                )
            }
        )
        self.token = "LEGACY_TOKEN_DO_NOT_REPORT"
        self.service = CanonicalMaterializationService(
            projects=self.projects,
            resources=self.resources,
            work_items=self.work_items,
            secrets=self.secrets,
            load_gitlab_routing=lambda: self.routing,
            legacy_gitlab_token=lambda _project_id: self.token,
            gitlab_api_base="https://gitlab.example/api/v4",
            store=CanonicalMaterializationStore(self.sqlite),
            clock=lambda: 1_800_000_000.0,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_dry_run_is_secret_free_and_has_zero_canonical_mutation(self):
        projects_before = self.projects.repository.load()
        resources_before = self.resources.store.load()
        secrets_before = self.secrets.store.load()
        work_before = self.work_items.load()

        plan = self.service.plan(
            self.project.id,
            actor=self.actor,
        )
        serialized = plan.model_dump_json()

        self.assertNotIn(self.token, serialized)
        self.assertEqual(
            self.projects.repository.load(),
            projects_before,
        )
        self.assertEqual(
            self.resources.store.load(),
            resources_before,
        )
        self.assertEqual(
            self.secrets.store.load(),
            secrets_before,
        )
        self.assertEqual(self.work_items.load(), work_before)
        self.assertEqual(
            self.service.status(
                self.project.id,
                actor=self.actor,
            ),
            (),
        )

    def test_apply_materializes_domains_and_repeat_is_idempotent(self):
        plan = self.service.plan(
            self.project.id,
            actor=self.actor,
        )

        first = self.service.apply(plan, actor=self.actor)
        second = self.service.apply(plan, actor=self.actor)

        self.assertEqual(
            first.status,
            MaterializationExecutionStatus.APPLIED,
        )
        self.assertEqual(second.id, first.id)
        project = self.projects.repository.load()[0]
        self.assertEqual(project.organization_id, "org-a")
        self.assertEqual(project.workspace_id, "ws-a")
        self.assertIsNotNone(project.authoritative_task_source)
        self.assertEqual(
            project.authoritative_task_source.source_type,
            "gitlab",
        )
        self.assertTrue(
            project.authoritative_task_source.credential_secret_id
        )

        resources = self.resources.list(
            self.actor,
            resource_type=None,
        )
        repositories = [
            item
            for item in resources
            if item.resource_type.value == "repository"
        ]
        self.assertEqual(len(repositories), 1)
        self.assertEqual(
            self.resources.resource_ids_for_project(project),
            [repositories[0].id],
        )

        work_item = self.work_items.get(self.work_item.ref)
        self.assertEqual(work_item.organization_id, "org-a")
        self.assertEqual(work_item.workspace_id, "ws-a")
        self.assertEqual(work_item.resource_ids, [repositories[0].id])

        references = self.secrets.list(self.actor)
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0].provider, "gitlab")
        self.assertEqual(
            len(self.resources.store.load().project_bindings),
            1,
        )
        report = json.dumps(
            second.model_dump(mode="json"),
            sort_keys=True,
        )
        self.assertNotIn(self.token, report)

    def test_fresh_plan_after_convergence_has_no_duplicate_writes(self):
        first_plan = self.service.plan(
            self.project.id,
            actor=self.actor,
        )
        self.service.apply(first_plan, actor=self.actor)

        bindings_before = len(
            self.resources.store.load().project_bindings
        )
        secrets_before = len(self.secrets.list(self.actor))
        puts_before = len(self.work_items.put_calls)

        converged = self.service.plan(
            self.project.id,
            actor=self.actor,
        )
        applied = self.service.apply(
            converged,
            actor=self.actor,
        )

        self.assertEqual(
            applied.status,
            MaterializationExecutionStatus.APPLIED,
        )
        self.assertFalse(
            any(
                item.apply_kind is not None
                for item in converged.operations
            )
        )
        self.assertEqual(
            len(self.resources.store.load().project_bindings),
            bindings_before,
        )
        self.assertEqual(
            len(self.secrets.list(self.actor)),
            secrets_before,
        )
        self.assertEqual(
            len(self.work_items.put_calls),
            puts_before,
        )

    def test_interrupted_apply_resumes_without_duplicate_state(self):
        plan = self.service.plan(
            self.project.id,
            actor=self.actor,
        )

        with self.assertRaises(CanonicalMaterializationError):
            self.service.apply(
                plan,
                actor=self.actor,
                fail_after_operations=2,
            )

        partial = self.service.status(
            self.project.id,
            actor=self.actor,
        )[0]
        self.assertEqual(
            partial.status,
            MaterializationExecutionStatus.PARTIAL,
        )

        resumed = self.service.apply(plan, actor=self.actor)
        self.assertEqual(
            resumed.status,
            MaterializationExecutionStatus.APPLIED,
        )
        self.assertEqual(len(self.secrets.list(self.actor)), 1)
        self.assertEqual(
            len(self.resources.store.load().project_bindings),
            1,
        )
        self.assertEqual(
            len(
                [
                    item
                    for item in self.resources.list(self.actor)
                    if item.resource_type.value == "repository"
                ]
            ),
            1,
        )

    def test_multi_repo_ambiguous_work_item_is_explicitly_unresolved(self):
        platform = self.root / "platform"
        platform.mkdir()
        (platform / ".git").mkdir()
        self.routing = GitLabRoutingSettings(
            projects={
                self.project.id: GitLabProjectRoutingSettings(
                    project_paths=[
                        "group/app",
                        "group/platform",
                    ]
                )
            }
        )
        self.work_items = _WorkItems(
            (
                self.work_item.model_copy(
                    update={
                        "ref": "group/unknown#9",
                        "project_path": None,
                    }
                ),
            )
        )
        self.service.work_items = self.work_items

        plan = self.service.plan(
            self.project.id,
            actor=self.actor,
        )
        item = next(
            value
            for value in plan.operations
            if value.domain == "work_item"
        )

        self.assertEqual(
            item.disposition,
            MaterializationDisposition.UNRESOLVED,
        )
        self.assertEqual(
            item.reason_code,
            "work_item_repository_ambiguous",
        )

    def test_missing_gitlab_credential_is_unresolved_not_guessed(self):
        self.service.legacy_gitlab_token = lambda _project_id: None

        plan = self.service.plan(
            self.project.id,
            actor=self.actor,
        )
        secret = next(
            value
            for value in plan.operations
            if value.id == "secret:gitlab"
        )
        task_source = next(
            value
            for value in plan.operations
            if value.id == "task-source:gitlab"
        )

        self.assertEqual(
            secret.reason_code,
            "gitlab_credential_missing",
        )
        self.assertEqual(
            secret.disposition,
            MaterializationDisposition.UNRESOLVED,
        )
        self.assertEqual(
            task_source.reason_code,
            "task_source_credential_unresolved",
        )

    def test_non_generic_cross_tenant_project_is_never_rebound(self):
        self.projects.repository.values[0] = self.project.model_copy(
            update={
                "organization_id": "org-old",
                "workspace_id": "ws-old",
            }
        )

        plan = self.service.plan(
            self.project.id,
            actor=self.actor,
        )
        scope = next(
            value
            for value in plan.operations
            if value.id == "project:scope"
        )

        self.assertEqual(
            scope.disposition,
            MaterializationDisposition.OPERATOR_ACTION_REQUIRED,
        )
        self.assertEqual(
            scope.reason_code,
            "project_scope_conflict",
        )
        self.assertFalse(
            any(
                value.apply_kind == "project_scope"
                for value in plan.operations
            )
        )

    def test_local_default_requires_explicit_confirmation(self):
        local_actor = _actor(
            organization_id="local",
            workspace_id="default",
        )

        blocked = self.service.plan(
            self.project.id,
            actor=local_actor,
        )
        confirmed = self.service.plan(
            self.project.id,
            actor=local_actor,
            confirm_generic_target=True,
        )

        blocked_scope = next(
            value
            for value in blocked.operations
            if value.id == "project:scope"
        )
        confirmed_scope = next(
            value
            for value in confirmed.operations
            if value.id == "project:scope"
        )
        self.assertEqual(
            blocked_scope.reason_code,
            "generic_scope_requires_confirmation",
        )
        self.assertEqual(
            confirmed_scope.reason_code,
            "project_scope_canonical",
        )
        self.assertNotEqual(blocked.id, confirmed.id)

    def test_historical_attention_and_approvals_are_never_fabricated(self):
        plan = self.service.plan(
            self.project.id,
            actor=self.actor,
        )
        skipped = {
            value.domain: value
            for value in plan.operations
            if value.domain in {"attention", "approval"}
        }

        self.assertEqual(
            skipped["attention"].disposition,
            MaterializationDisposition.SKIPPED,
        )
        self.assertEqual(
            skipped["approval"].disposition,
            MaterializationDisposition.SKIPPED,
        )
        self.assertIn(
            "not_safely_derivable",
            skipped["approval"].reason_code,
        )


if __name__ == "__main__":
    unittest.main()
