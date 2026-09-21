from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.models import GitLabRoutingSettings, Project
from codex_web.secret_backends import LocalFileSecretBackend
from codex_web.services.canonical_materialization import (
    CanonicalMaterializationService,
)
from codex_web.services.fresh_project_bootstrap import (
    FreshProjectBootstrapService,
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
            updated = project.model_copy(
                update={"authoritative_task_source": source}
            )
            self.repository.values[index] = updated
            return updated
        raise LookupError("Project not found")


class _WorkItems:
    def __init__(self) -> None:
        self.values = {}

    def load(self):
        return dict(self.values)

    def get(self, ref):
        return self.values.get(ref)

    def put(self, ref, value):
        self.values[ref] = value
        return value


class _Readiness:
    def __init__(
        self,
        resources: ResourceCatalogService,
        projects: _Projects,
        actor: AuthenticationActor,
        *,
        worker_ready: bool = True,
    ) -> None:
        self.resources = resources
        self.projects = projects
        self.actor = actor
        self.worker_ready = worker_ready

    def evaluate(self, project_id, *, actor, record=False):
        del record
        project = next(
            item
            for item in self.projects.repository.load()
            if item.id == project_id
        )
        repositories = [
            item
            for item in self.resources.project_resources(
                project,
                actor=actor,
            )
            if item.resource_type.value == "repository"
        ]
        execution_ready = (
            self.worker_ready
            and len(repositories) == 1
        )
        return SimpleNamespace(
            execution_ready=execution_ready,
            model_dump=lambda mode="json": {
                "project_id": project_id,
                "execution_ready": execution_ready,
                "semantic_ready": bool(repositories),
                "status": (
                    "ready"
                    if execution_ready
                    else "blocked"
                ),
                "repository_count": len(repositories),
                "worker_ready": self.worker_ready,
            },
        )


def _actor() -> AuthenticationActor:
    return AuthenticationActor(
        identity_id="admin-a",
        principal_kind=PrincipalKind.HUMAN,
        organization_id="local",
        workspace_id="default",
        roles=(MembershipRole.OWNER,),
        assurance=AuthenticationAssurance.MFA,
    )


class FreshProjectBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project_root = root / "project"
        self.project_root.mkdir()
        self.project = Project(
            id="project-a",
            organization_id="local",
            workspace_id="default",
            name="Project A",
            path=str(self.project_root),
        )
        self.projects = _Projects(self.project)
        self.actor = _actor()
        self.sqlite = SQLiteStateStore(root / "state.sqlite3")
        self.resources = ResourceCatalogService(
            ResourceCatalogStore(self.sqlite)
        )
        self.secrets = SecretBroker(
            SecretStateStore(self.sqlite),
            {
                "local": LocalFileSecretBackend(
                    root / "secrets"
                )
            },
        )
        self.work_items = _WorkItems()
        self.materialization = CanonicalMaterializationService(
            projects=self.projects,
            resources=self.resources,
            work_items=self.work_items,
            secrets=self.secrets,
            load_gitlab_routing=lambda: GitLabRoutingSettings(),
            legacy_gitlab_token=lambda _project_id: None,
            gitlab_api_base="https://gitlab.example/api/v4",
            store=CanonicalMaterializationStore(self.sqlite),
            clock=lambda: 1_800_000_000.0,
        )

    def _service(self, *, worker_ready=True):
        return FreshProjectBootstrapService(
            materialization=self.materialization,
            readiness=_Readiness(
                self.resources,
                self.projects,
                self.actor,
                worker_ready=worker_ready,
            ),
        )

    def _repository_resources(self):
        project = self.projects.repository.load()[0]
        return [
            item
            for item in self.resources.project_resources(
                project,
                actor=self.actor,
            )
            if item.resource_type.value == "repository"
        ]

    def test_single_repository_materializes_once_and_becomes_ready(self):
        (self.project_root / ".git").mkdir()

        first = self._service().bootstrap(
            self.project.id,
            actor=self.actor,
        )
        second = self._service().bootstrap(
            self.project.id,
            actor=self.actor,
        )

        self.assertEqual(first["status"], "ready")
        self.assertEqual(second["status"], "ready")
        self.assertEqual(len(self._repository_resources()), 1)
        self.assertEqual(
            len(self.resources.store.load().project_bindings),
            1,
        )

    def test_missing_worker_blocks_readiness_but_not_topology_materialization(self):
        (self.project_root / ".git").mkdir()

        result = self._service(worker_ready=False).bootstrap(
            self.project.id,
            actor=self.actor,
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(len(self._repository_resources()), 1)
        self.assertFalse(result["readiness"]["execution_ready"])

    def test_multi_repository_topology_is_preserved_without_arbitrary_selection(self):
        for name in ("app", "platform"):
            repository = self.project_root / name
            repository.mkdir()
            (repository / ".git").mkdir()

        result = self._service().bootstrap(
            self.project.id,
            actor=self.actor,
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(
            {item.name for item in self._repository_resources()},
            {"app", "platform"},
        )
        self.assertEqual(
            result["readiness"]["repository_count"],
            2,
        )

    def test_non_git_directory_remains_configured_and_explicitly_blocked(self):
        result = self._service().bootstrap(
            self.project.id,
            actor=self.actor,
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self._repository_resources(), [])
        codes = {
            item["code"]
            for item in result["materializationBlockers"]
        }
        self.assertIn("repository_missing", codes)


if __name__ == "__main__":
    unittest.main()
