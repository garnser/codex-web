from __future__ import annotations

import unittest

from codex_web.execution_workspaces import LeaseMode
from codex_web.resources import (
    RepositoryTargetSource,
    ResourceCreate,
    ResourceLifecycle,
    ResourceType,
    ResourceUpdate,
)
from codex_web.services.turn_execution_binding import TurnExecutionBindingError
from tests.test_turn_execution_binding import TurnExecutionBindingTests


class ExplicitRepositoryTargetQualificationTests(unittest.TestCase):
    """End-to-end qualification for explicit per-turn multi-repository targeting."""

    def setUp(self) -> None:
        self.fx = TurnExecutionBindingTests(
            "test_explicit_repository_target_is_persisted"
        )
        self.fx.setUp()
        self.fx._publish_secret()

        self.repo_a = self.fx.repository
        self.repo_b = self._repository("Application")
        self.repo_c = self._repository("Platform")
        self.fx.project = self.fx.project.model_copy(
            update={"repository_selection_policy": "explicit"}
        )
        self.fx.projects.project = self.fx.project
        self.fx.service.project_readiness = lambda project_id, actor: {
            "execution_ready": True,
            "correlation_id": "explicit-target-qualification",
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

    def tearDown(self) -> None:
        self.fx.tearDown()

    def _repository(self, name: str):
        repository = self.fx.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name=name,
            ),
            actor=self.fx.actor,
        )
        self.fx.resources.bind_project(
            project=self.fx.project,
            resource_id=repository.id,
            actor=self.fx.actor,
        )
        return repository

    def _prepare(self, execution_id: str, **kwargs):
        return self.fx.service.prepare(
            thread_id=f"thread-{execution_id}",
            execution_id=execution_id,
            project_id=self.fx.project.id,
            sandbox="workspace-write",
            approval_policy="on-request",
            **kwargs,
        )

    def _assert_no_mutation(self) -> None:
        self.assertEqual(self.fx.workspaces.list(self.fx.actor), [])
        self.assertEqual(self.fx.workers.list_assignments(self.fx.actor), [])
        self.assertEqual(self.fx.backend.provisioned, [])

    def _assert_isolated(
        self,
        binding,
        mutable_repository_id: str,
        *,
        read_only_repository_ids: tuple[str, ...] = (),
    ) -> None:
        workspace = self.fx.workspaces.get(binding.workspace_id, self.fx.actor)
        assignment = next(
            item
            for item in self.fx.workers.list_assignments(self.fx.actor)
            if item.id == binding.assignment_id
        )
        self.assertEqual(workspace.repository_resource_id, mutable_repository_id)
        self.assertEqual(
            workspace.resource_ids,
            (mutable_repository_id, *read_only_repository_ids),
        )
        self.assertEqual(assignment.resource_ids, workspace.resource_ids)
        self.assertEqual(
            binding.repository_target.read_only_repository_ids,
            read_only_repository_ids,
        )
        members = {
            item.resource_id: item
            for item in workspace.repository_members
        }
        self.assertEqual(
            members[mutable_repository_id].access_mode,
            LeaseMode.WRITE,
        )
        for repository_id in read_only_repository_ids:
            self.assertEqual(
                members[repository_id].access_mode,
                LeaseMode.READ,
            )
        writable = [
            item.resource_id
            for item in workspace.repository_members
            if item.access_mode == LeaseMode.WRITE
        ]
        self.assertEqual(writable, [mutable_repository_id])
        inspection = next(
            item
            for item in self.fx.workspaces.inspect(self.fx.actor)
            if item.workspace.id == workspace.id
        )
        self.assertEqual(inspection.lease.mode, LeaseMode.WRITE)
        self.assertEqual(
            assignment.repository_target,
            binding.repository_target,
        )

    def test_explicit_target_uses_exactly_one_mutable_repository(self) -> None:
        binding = self._prepare(
            "explicit",
            explicit_repository_id=self.repo_b.id,
            read_only_repository_ids=(self.repo_c.id,),
        )

        self.assertEqual(
            binding.repository_target.source,
            RepositoryTargetSource.EXPLICIT,
        )
        self._assert_isolated(
            binding,
            self.repo_b.id,
            read_only_repository_ids=(self.repo_c.id,),
        )

    def test_work_item_and_converging_selectors_preserve_provenance_and_retry(self) -> None:
        binding = self._prepare(
            "work-item",
            work_item_resource_ids=(self.repo_b.id,),
            work_item_ref="group/application#42",
            explicit_repository_id=self.repo_b.id,
        )
        self.assertEqual(
            binding.repository_target.source,
            RepositoryTargetSource.WORK_ITEM,
        )
        self.assertEqual(
            [item.source for item in binding.repository_target.selection_evidence],
            [
                RepositoryTargetSource.WORK_ITEM,
                RepositoryTargetSource.EXPLICIT,
            ],
        )
        self._assert_isolated(binding, self.repo_b.id)

        retried = self._prepare(
            "work-item",
            work_item_resource_ids=(self.repo_b.id,),
            work_item_ref="group/application#42",
            explicit_repository_id=self.repo_b.id,
        )
        self.assertEqual(retried.workspace_id, binding.workspace_id)
        self.assertEqual(retried.assignment_id, binding.assignment_id)
        self.assertEqual(len(self.fx.workspaces.list(self.fx.actor)), 1)
        self.assertEqual(len(self.fx.workers.list_assignments(self.fx.actor)), 1)
        self.assertEqual(len(self.fx.backend.provisioned), 1)

    def test_negative_target_matrix_fails_before_any_mutation(self) -> None:
        cases = []

        cases.append(("missing", {}, "repository_target_missing"))
        cases.append((
            "conflict",
            {
                "work_item_resource_ids": (self.repo_a.id,),
                "work_item_ref": "group/application#43",
                "explicit_repository_id": self.repo_b.id,
            },
            "repository_target_conflict",
        ))
        cases.append((
            "ambiguous-work-item",
            {
                "work_item_resource_ids": (
                    self.repo_a.id,
                    self.repo_b.id,
                ),
                "work_item_ref": "group/application#44",
            },
            "repository_target_ambiguous",
        ))

        unbound = self.fx.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Unbound",
            ),
            actor=self.fx.actor,
        )
        cases.append((
            "unbound",
            {"explicit_repository_id": unbound.id},
            "repository_target_unauthorized",
        ))
        cases.append((
            "not-repository",
            {"explicit_repository_id": self.fx.service_resource.id},
            "repository_target_unauthorized",
        ))

        foreign_actor = self.fx.actor.model_copy(
            update={
                "identity_id": "foreign-admin",
                "organization_id": "foreign-org",
                "workspace_id": "foreign-workspace",
            }
        )
        foreign = self.fx.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Foreign",
            ),
            actor=foreign_actor,
        )
        cases.append((
            "cross-tenant",
            {"explicit_repository_id": foreign.id},
            "repository_target_unauthorized",
        ))

        inactive = self._repository("Inactive")
        self.fx.resources.update(
            inactive.id,
            ResourceUpdate(lifecycle=ResourceLifecycle.DISABLED),
            actor=self.fx.actor,
        )
        cases.append((
            "inactive",
            {"explicit_repository_id": inactive.id},
            "repository_target_unauthorized",
        ))

        cached_stale_id = self.repo_c.id
        self.fx.resources.update(
            cached_stale_id,
            ResourceUpdate(lifecycle=ResourceLifecycle.DISABLED),
            actor=self.fx.actor,
        )
        cases.append((
            "stale-selector",
            {"explicit_repository_id": cached_stale_id},
            "repository_target_unauthorized",
        ))

        for name, kwargs, code in cases:
            with self.subTest(name=name):
                with self.assertRaises(TurnExecutionBindingError) as caught:
                    self._prepare(f"negative-{name}", **kwargs)
                self.assertEqual(caught.exception.code, code)
                public = caught.exception.public()
                self.assertEqual(public["code"], code)
                self.assertNotIn("secret-codex-worker", str(public))
                self._assert_no_mutation()

    def test_project_readiness_and_turn_eligibility_are_independent(self) -> None:
        with self.assertRaises(TurnExecutionBindingError) as missing:
            self._prepare("ready-project-missing-turn-target")
        self.assertEqual(
            missing.exception.code,
            "repository_target_missing",
        )
        self._assert_no_mutation()

        self.fx.service.project_readiness = lambda project_id, actor: {
            "execution_ready": False,
            "correlation_id": "rootless-podman-blocked",
            "checks": [
                {
                    "id": "execution:worker",
                    "domain": "execution_worker",
                    "status": "blocked",
                    "code": "worker_capability_missing",
                    "message": (
                        "rootless Podman worker is missing "
                        "command_execution"
                    ),
                    "required": True,
                    "remediation": "Restore command_execution capability.",
                }
            ],
        }

        with self.assertRaises(TurnExecutionBindingError) as blocked:
            self._prepare(
                "valid-target-worker-blocked",
                explicit_repository_id=self.repo_b.id,
            )
        self.assertEqual(
            blocked.exception.code,
            "project_readiness_blocked",
        )
        self.assertEqual(
            blocked.exception.public()["readiness_code"],
            "worker_capability_missing",
        )
        self._assert_no_mutation()


if __name__ == "__main__":
    unittest.main()
