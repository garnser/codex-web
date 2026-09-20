from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from codex_web.execution_contract_schema import execution_contract_for_work_item
from codex_web.execution_subjects import ExecutionSubject, ExecutionSubjectKind
from codex_web.execution_contract_seed import execution_role_catalog_seed_payload
from codex_web.execution_role_models import ExecutionRoleCatalogDefinition
from codex_web.execution_workspace_backend import GitWorkspaceProvision
from codex_web.execution_workspaces import (
    ExecutionWorkspaceAcquire,
    ExecutionWorkspaceKind,
    ExecutionWorkspaceRelease,
    ExecutionWorkspaceStatus,
    IntegrationOutcome,
    IntegrationStrategy,
    LeaseMode,
    WorkspaceIntegrationRecord,
    WorkspaceQuota,
)
from codex_web.models import Project, WorkItemState
from codex_web.resources import ResourceAlias, ResourceCreate, ResourceType
from codex_web.services.execution_workspaces import (
    ExecutionWorkspaceConflictError,
    ExecutionWorkspaceQuotaError,
    ExecutionWorkspaceService,
)
from codex_web.services.identity import IdentityService
from codex_web.services.resources import ResourceCatalogService
from codex_web.storage.execution_workspaces import ExecutionWorkspaceStateStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.sqlite_state import SQLiteStateStore


ROLE_CONTRACTS = ExecutionRoleCatalogDefinition.model_validate(
    execution_role_catalog_seed_payload()
).role_map


class _FakeBackend:
    def __init__(self, root: Path, *, fail_provision: bool = False) -> None:
        self.root = root
        self.fail_provision = fail_provision
        self.provisioned: list[tuple[str, str, str | None]] = []
        self.cleaned: list[tuple[str, str, bool]] = []

    def provision_git(self, repository_path, workspace_id, branch_name, base_revision):
        if self.fail_provision:
            raise RuntimeError("provision failed")
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
        if self.fail_provision:
            raise RuntimeError("provision failed")
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
        if self.fail_provision:
            raise RuntimeError("provision failed")
        path = self.root / workspace_id
        path.mkdir(parents=True, exist_ok=False)
        self.provisioned.append((workspace_id, "scratch", None))
        return path

    def cleanup_scratch(self, workspace_path):
        self.cleaned.append((str(workspace_path), "scratch", False))
        Path(workspace_path).rmdir()

    def cleanup_git(self, repository_path, workspace_path, branch_name, *, discard_branch):
        self.cleaned.append((str(workspace_path), branch_name, discard_branch))

    def head_revision(self, workspace_path):
        return "head-revision"

    def disk_usage(self, workspace_path):
        return 128


class _WorkItemHost:
    def __init__(self, state: WorkItemState) -> None:
        self.states = {state.ref: state}
        self.events = []

    def _load_work_item_states(self):
        return {key: value.model_copy(deep=True) for key, value in self.states.items()}

    def _save_work_item_states(self, states):
        self.states = {key: value.model_copy(deep=True) for key, value in states.items()}

    @staticmethod
    def _work_item_event(ref, event_type, **kwargs):
        return {"ref": ref, "event_type": event_type, **kwargs}

    def _append_work_item_event(self, event):
        self.events.append(event)


class ExecutionWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.sqlite = SQLiteStateStore(root / "state.sqlite3")
        self.identity = IdentityService(IdentityStateStore(self.sqlite))
        self.identity.bootstrap_local()
        self.actor = self.identity.local_trusted_actor()
        self.resources = ResourceCatalogService(ResourceCatalogStore(self.sqlite))
        self.repo = self.resources.create(
            ResourceCreate(resource_type=ResourceType.REPOSITORY, name="Repo"),
            actor=self.actor,
        )
        self.repo2_source = root / "repo2-source"
        self.repo2_source.mkdir()
        self.repo2 = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Repo 2",
                aliases=[
                    ResourceAlias(
                        namespace="filesystem",
                        value=str(self.repo2_source),
                    )
                ],
            ),
            actor=self.actor,
        )
        self.database = self.resources.create(
            ResourceCreate(resource_type=ResourceType.DATABASE, name="Database"),
            actor=self.actor,
        )
        self.project = Project(
            id="home",
            organization_id="local",
            workspace_id="default",
            name="Home",
            path=str(root / "repository"),
        )
        Path(self.project.path).mkdir()
        self.work_item = WorkItemState(
            ref="group/app#42",
            organization_id="local",
            workspace_id="default",
            project_id="home",
            project_path="group/app",
            resource_ids=[self.repo.id],
            current_owner="james",
            current_stage="implementation_active",
            artifact_state="branch",
            last_meaningful_update_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )
        self.host = _WorkItemHost(self.work_item)
        self.backend = _FakeBackend(root / "workspaces")
        self.service = ExecutionWorkspaceService(
            ExecutionWorkspaceStateStore(self.sqlite),
            self.backend,
            self.resources,
            lambda project_id: self.project,
            work_item_host=self.host,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _acquire(
        self,
        execution_id: str,
        resource_id: str | None = None,
        *,
        repository_resource_id: str | None = None,
        lease_mode: LeaseMode = LeaseMode.WRITE,
    ):
        resource_id = resource_id or self.repo.id
        if repository_resource_id is None and resource_id in {self.repo.id, self.repo2.id}:
            repository_resource_id = resource_id
        return self.service.acquire(
            ExecutionWorkspaceAcquire(
                work_item_ref=self.work_item.ref,
                execution_id=execution_id,
                project_id="home",
                resource_ids=(resource_id,),
                repository_resource_id=repository_resource_id,
                lease_mode=lease_mode,
                ttl_seconds=30,
            ),
            actor=self.actor,
        )

    def test_thread_subject_workspace_is_canonical_without_fake_work_item_sync(self) -> None:
        workspace = self.service.acquire(
            ExecutionWorkspaceAcquire(
                subject=ExecutionSubject(
                    kind=ExecutionSubjectKind.THREAD,
                    ref="thread-abc",
                ),
                execution_id="thread-exec",
                project_id="home",
                resource_ids=(self.repo.id,),
                repository_resource_id=self.repo.id,
                ttl_seconds=30,
            ),
            actor=self.actor,
        )

        self.assertEqual(workspace.subject.kind, ExecutionSubjectKind.THREAD)
        self.assertEqual(workspace.subject.ref, "thread-abc")
        self.assertIsNone(workspace.work_item_ref)
        self.assertEqual(set(self.host.states), {self.work_item.ref})
        self.assertEqual(self.host.events, [])

    def test_thread_bootstrap_workspace_is_canonical_without_work_item_sync(self) -> None:
        workspace = self.service.acquire(
            ExecutionWorkspaceAcquire(
                subject=ExecutionSubject(
                    kind=ExecutionSubjectKind.THREAD_BOOTSTRAP,
                    ref="bootstrap-abc",
                ),
                execution_id="bootstrap-exec",
                project_id="home",
                resource_ids=(self.repo.id,),
                repository_resource_id=self.repo.id,
                ttl_seconds=30,
            ),
            actor=self.actor,
        )

        self.assertEqual(
            workspace.subject.kind,
            ExecutionSubjectKind.THREAD_BOOTSTRAP,
        )
        self.assertEqual(workspace.subject.ref, "bootstrap-abc")
        self.assertIsNone(workspace.work_item_ref)
        self.assertEqual(set(self.host.states), {self.work_item.ref})
        self.assertEqual(self.host.events, [])

    def test_v1_workspace_state_migrates_work_item_subject_on_workspace_and_lease(self) -> None:
        workspace = self._acquire("migrate-exec", self.repo.id)
        raw = self.service.store.store.get(self.service.store.namespace)
        raw["schema_version"] = "1.0"
        for item in raw["workspaces"]:
            if item["id"] == workspace.id:
                item.pop("subject", None)
        for item in raw["leases"]:
            if item["execution_workspace_id"] == workspace.id:
                item.pop("subject", None)
        self.service.store.store.put(self.service.store.namespace, raw)

        state = self.service.store.load()
        migrated_workspace = next(item for item in state.workspaces if item.id == workspace.id)
        migrated_lease = next(
            item for item in state.leases if item.execution_workspace_id == workspace.id
        )
        self.assertEqual(state.schema_version, "1.3")
        self.assertEqual(migrated_workspace.subject.kind, ExecutionSubjectKind.WORK_ITEM)
        self.assertEqual(migrated_workspace.subject.ref, self.work_item.ref)
        self.assertEqual(migrated_lease.subject, migrated_workspace.subject)

    def test_parallel_non_conflicting_executions_get_distinct_worktrees_and_branches(self) -> None:
        first = self._acquire("exec-1", self.repo.id)
        second = self._acquire("exec-2", self.repo2.id)

        self.assertEqual(first.status, ExecutionWorkspaceStatus.ACTIVE)
        self.assertEqual(second.status, ExecutionWorkspaceStatus.ACTIVE)
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.path, second.path)
        self.assertNotEqual(first.branch_name, second.branch_name)
        self.assertEqual(len(self.backend.provisioned), 2)

    def test_multi_repository_workspace_provisions_mutable_and_read_only_members(self) -> None:
        workspace = self.service.acquire(
            ExecutionWorkspaceAcquire(
                work_item_ref=self.work_item.ref,
                execution_id="multi-exec",
                project_id="home",
                resource_ids=(self.repo.id, self.repo2.id),
                repository_resource_id=self.repo.id,
                read_only_repository_ids=(self.repo2.id,),
                lease_mode=LeaseMode.WRITE,
                ttl_seconds=30,
            ),
            actor=self.actor,
        )
        lease = next(
            item
            for item in self.service.store.load().leases
            if item.id == workspace.lease_id
        )

        self.assertEqual(workspace.status, ExecutionWorkspaceStatus.ACTIVE)
        self.assertEqual(len(workspace.repository_members), 2)
        mutable = next(
            item
            for item in workspace.repository_members
            if item.resource_id == self.repo.id
        )
        read_only = next(
            item
            for item in workspace.repository_members
            if item.resource_id == self.repo2.id
        )
        self.assertEqual(mutable.access_mode, LeaseMode.WRITE)
        self.assertEqual(mutable.workspace_path, workspace.path)
        self.assertEqual(read_only.access_mode, LeaseMode.READ)
        self.assertEqual(read_only.source_path, str(self.repo2_source.resolve()))
        self.assertTrue(read_only.sandbox_path.startswith("/mnt/codex-context/"))
        self.assertEqual(lease.resource_modes[self.repo.id], LeaseMode.WRITE)
        self.assertEqual(lease.resource_modes[self.repo2.id], LeaseMode.READ)
        self.assertEqual(workspace.actual_disk_bytes, 256)
        self.assertEqual(len(self.backend.provisioned), 2)

    def test_read_only_member_lease_can_coexist_but_write_conflicts(self) -> None:
        self.service.acquire(
            ExecutionWorkspaceAcquire(
                work_item_ref=self.work_item.ref,
                execution_id="multi-read-holder",
                project_id="home",
                resource_ids=(self.repo.id, self.repo2.id),
                repository_resource_id=self.repo.id,
                read_only_repository_ids=(self.repo2.id,),
                lease_mode=LeaseMode.WRITE,
                ttl_seconds=30,
            ),
            actor=self.actor,
        )

        second = self.service.acquire(
            ExecutionWorkspaceAcquire(
                work_item_ref=self.work_item.ref,
                execution_id="repo2-read",
                project_id="home",
                resource_ids=(self.repo2.id,),
                repository_resource_id=self.repo2.id,
                lease_mode=LeaseMode.READ,
                ttl_seconds=30,
            ),
            actor=self.actor,
        )
        self.assertEqual(second.status, ExecutionWorkspaceStatus.ACTIVE)

        with self.assertRaises(ExecutionWorkspaceConflictError):
            self.service.acquire(
                ExecutionWorkspaceAcquire(
                    work_item_ref=self.work_item.ref,
                    execution_id="repo2-write",
                    project_id="home",
                    resource_ids=(self.repo2.id,),
                    repository_resource_id=self.repo2.id,
                    lease_mode=LeaseMode.WRITE,
                    ttl_seconds=30,
                ),
                actor=self.actor,
            )

    def test_multi_repository_release_cleans_every_member_without_deleting_read_only_branch(self) -> None:
        workspace = self.service.acquire(
            ExecutionWorkspaceAcquire(
                work_item_ref=self.work_item.ref,
                execution_id="multi-cleanup",
                project_id="home",
                resource_ids=(self.repo.id, self.repo2.id),
                repository_resource_id=self.repo.id,
                read_only_repository_ids=(self.repo2.id,),
                lease_mode=LeaseMode.WRITE,
                ttl_seconds=30,
            ),
            actor=self.actor,
        )

        self.service.release(
            workspace.id,
            ExecutionWorkspaceRelease(discard=True, reason="cleanup test"),
            actor=self.actor,
        )

        self.assertEqual(len(self.backend.cleaned), 2)
        discarded = [item for item in self.backend.cleaned if item[2]]
        self.assertEqual(len(discarded), 1)
        self.assertEqual(
            discarded[0][0],
            next(
                item.workspace_path
                for item in workspace.repository_members
                if item.resource_id == self.repo.id
            ),
        )

    def test_relative_repository_alias_cannot_escape_project_root_through_symlink(self) -> None:
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        escape = Path(self.project.path) / "escape"
        escape.symlink_to(outside, target_is_directory=True)
        escaped_resource = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Escaped",
                aliases=[ResourceAlias(namespace="path", value="escape")],
            ),
            actor=self.actor,
        )

        with self.assertRaisesRegex(
            ExecutionWorkspaceConflictError,
            "escapes canonical project root",
        ):
            self.service.acquire(
                ExecutionWorkspaceAcquire(
                    work_item_ref=self.work_item.ref,
                    execution_id="escape-exec",
                    project_id="home",
                    resource_ids=(self.repo.id, escaped_resource.id),
                    repository_resource_id=self.repo.id,
                    read_only_repository_ids=(escaped_resource.id,),
                    ttl_seconds=30,
                ),
                actor=self.actor,
            )

        self.assertFalse(
            any(
                item.execution_id == "escape-exec"
                for item in self.service.store.load().workspaces
            )
        )

    def test_multi_repository_expiry_recovers_and_cleans_all_members(self) -> None:
        workspace = self.service.acquire(
            ExecutionWorkspaceAcquire(
                work_item_ref=self.work_item.ref,
                execution_id="multi-expire",
                project_id="home",
                resource_ids=(self.repo.id, self.repo2.id),
                repository_resource_id=self.repo.id,
                read_only_repository_ids=(self.repo2.id,),
                lease_mode=LeaseMode.WRITE,
                ttl_seconds=30,
            ),
            actor=self.actor,
        )

        recovered = self.service.recover_expired(
            now=time.time() + 31,
            scope=self.actor.tenant,
        )

        self.assertEqual([item.id for item in recovered], [workspace.id])
        current = self.service.get(workspace.id, self.actor)
        self.assertEqual(current.status, ExecutionWorkspaceStatus.ABANDONED)
        self.assertIsNotNone(current.cleaned_at)
        self.assertEqual(len(self.backend.cleaned), 2)
        self.assertTrue(all(not item[2] for item in self.backend.cleaned))

    def test_integration_result_updates_only_mutable_repository_member(self) -> None:
        workspace = self.service.acquire(
            ExecutionWorkspaceAcquire(
                work_item_ref=self.work_item.ref,
                execution_id="multi-integrate",
                project_id="home",
                resource_ids=(self.repo.id, self.repo2.id),
                repository_resource_id=self.repo.id,
                read_only_repository_ids=(self.repo2.id,),
                lease_mode=LeaseMode.WRITE,
                ttl_seconds=30,
            ),
            actor=self.actor,
        )
        before_read_only = next(
            item
            for item in workspace.repository_members
            if item.resource_id == self.repo2.id
        ).head_revision

        updated = self.service.record_integration(
            workspace.id,
            WorkspaceIntegrationRecord(
                strategy=IntegrationStrategy.MERGE,
                outcome=IntegrationOutcome.MERGED,
                target_revision="main@abc",
                resulting_revision="result-123",
            ),
            actor=self.actor,
        )

        mutable = next(
            item
            for item in updated.repository_members
            if item.resource_id == self.repo.id
        )
        read_only = next(
            item
            for item in updated.repository_members
            if item.resource_id == self.repo2.id
        )
        self.assertEqual(updated.head_revision, "result-123")
        self.assertEqual(mutable.head_revision, "result-123")
        self.assertEqual(read_only.head_revision, before_read_only)

    def test_same_execution_acquisition_is_idempotent(self) -> None:
        first = self._acquire("same-exec", self.repo.id)
        second = self._acquire("same-exec", self.repo.id)

        self.assertEqual(second.id, first.id)
        self.assertEqual(second.lease_id, first.lease_id)
        self.assertEqual(len(self.backend.provisioned), 1)

    def test_conflicting_write_lease_fails_closed(self) -> None:
        self._acquire("exec-1", self.repo.id)
        with self.assertRaises(ExecutionWorkspaceConflictError):
            self._acquire("exec-2", self.repo.id)

    def test_read_leases_can_coexist_but_write_conflicts(self) -> None:
        self._acquire(
            "read-1",
            self.database.id,
            repository_resource_id=None,
            lease_mode=LeaseMode.READ,
        )
        self._acquire(
            "read-2",
            self.database.id,
            repository_resource_id=None,
            lease_mode=LeaseMode.READ,
        )
        with self.assertRaises(ExecutionWorkspaceConflictError):
            self._acquire(
                "write-1",
                self.database.id,
                repository_resource_id=None,
                lease_mode=LeaseMode.WRITE,
            )

    def test_scratch_workspace_has_filesystem_without_resource_or_git_lease(self) -> None:
        workspace = self.service.acquire(
            ExecutionWorkspaceAcquire(
                subject=ExecutionSubject(
                    kind=ExecutionSubjectKind.THREAD_BOOTSTRAP,
                    ref="bootstrap-orchestrator",
                ),
                execution_id="scratch-exec",
                project_id="home",
                resource_ids=(),
                scratch=True,
                ttl_seconds=30,
            ),
            actor=self.actor,
        )
        lease = next(
            item
            for item in self.service.store.load().leases
            if item.id == workspace.lease_id
        )

        self.assertEqual(workspace.kind, ExecutionWorkspaceKind.SCRATCH)
        self.assertEqual(workspace.resource_ids, ())
        self.assertIsNone(workspace.repository_resource_id)
        self.assertIsNotNone(workspace.path)
        self.assertTrue(Path(workspace.path).is_dir())
        self.assertEqual(lease.resource_ids, ())
        self.assertEqual(lease.resource_modes, {})

        released = self.service.release(
            workspace.id,
            ExecutionWorkspaceRelease(reason="scratch complete"),
            actor=self.actor,
        )
        self.assertEqual(released.status, ExecutionWorkspaceStatus.RELEASED)
        self.assertFalse(Path(workspace.path).exists())

    def test_scratch_workspace_rejects_repository_authority(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "scratch execution workspace cannot lease canonical resources",
        ):
            ExecutionWorkspaceAcquire(
                work_item_ref=self.work_item.ref,
                execution_id="invalid-scratch",
                project_id="home",
                resource_ids=(self.repo.id,),
                repository_resource_id=self.repo.id,
                scratch=True,
            )

    def test_non_git_resource_uses_lease_only_workspace(self) -> None:
        workspace = self._acquire(
            "db-exec",
            self.database.id,
            repository_resource_id=None,
        )
        self.assertEqual(workspace.kind, ExecutionWorkspaceKind.RESOURCE_LEASE)
        self.assertIsNone(workspace.path)
        self.assertIsNone(workspace.branch_name)
        self.assertEqual(workspace.status, ExecutionWorkspaceStatus.ACTIVE)

    def test_expired_lease_is_abandoned_and_worktree_cleaned_without_deleting_branch(self) -> None:
        workspace = self._acquire("exec-expire", self.repo.id)
        recovered = self.service.recover_expired(
            now=time.time() + 31,
            scope=self.actor.tenant,
        )

        self.assertEqual([item.id for item in recovered], [workspace.id])
        current = self.service.get(workspace.id, self.actor)
        self.assertEqual(current.status, ExecutionWorkspaceStatus.ABANDONED)
        self.assertIsNotNone(current.cleaned_at)
        self.assertEqual(len(self.backend.cleaned), 1)
        self.assertFalse(self.backend.cleaned[0][2])

    def test_release_cleanup_and_discard_are_explicit(self) -> None:
        workspace = self._acquire("exec-discard", self.repo.id)
        released = self.service.release(
            workspace.id,
            ExecutionWorkspaceRelease(discard=True, reason="operator discard"),
            actor=self.actor,
        )
        self.assertEqual(released.status, ExecutionWorkspaceStatus.DISCARDED)
        self.assertIsNotNone(released.cleaned_at)
        self.assertTrue(self.backend.cleaned[-1][2])

    def test_merge_conflict_is_canonical_structured_state(self) -> None:
        workspace = self._acquire("exec-conflict", self.repo.id)
        conflicted = self.service.record_integration(
            workspace.id,
            WorkspaceIntegrationRecord(
                strategy=IntegrationStrategy.MERGE,
                outcome=IntegrationOutcome.CONFLICT,
                target_revision="main@abc",
                conflicts=("src/app.py", "tests/test_app.py"),
            ),
            actor=self.actor,
        )
        self.assertEqual(conflicted.status, ExecutionWorkspaceStatus.CONFLICTED)
        self.assertEqual(conflicted.integration.outcome, IntegrationOutcome.CONFLICT)
        self.assertEqual(
            conflicted.integration.conflicts,
            ("src/app.py", "tests/test_app.py"),
        )

    def test_quota_blocks_parallel_workspace_creation(self) -> None:
        self.service.quota = WorkspaceQuota(
            max_active_per_tenant=10,
            max_active_per_identity=1,
            max_resources_per_workspace=4,
            max_requested_disk_bytes=1024,
        )
        self._acquire("quota-1", self.repo.id)
        with self.assertRaises(ExecutionWorkspaceQuotaError):
            self._acquire("quota-2", self.repo2.id)

    def test_provisioning_failure_releases_reserved_resource_lease(self) -> None:
        failing = _FakeBackend(Path(self.temp.name) / "failing-workspaces", fail_provision=True)
        service = ExecutionWorkspaceService(
            ExecutionWorkspaceStateStore(self.sqlite),
            failing,
            self.resources,
            lambda project_id: self.project,
            work_item_host=self.host,
        )
        with self.assertRaises(RuntimeError):
            service.acquire(
                ExecutionWorkspaceAcquire(
                    work_item_ref=self.work_item.ref,
                    execution_id="failed-exec",
                    project_id="home",
                    resource_ids=(self.repo.id,),
                    repository_resource_id=self.repo.id,
                    ttl_seconds=30,
                ),
                actor=self.actor,
            )

        state = ExecutionWorkspaceStateStore(self.sqlite).load()
        failed = next(item for item in state.workspaces if item.execution_id == "failed-exec")
        lease = next(item for item in state.leases if item.execution_workspace_id == failed.id)
        self.assertEqual(failed.status, ExecutionWorkspaceStatus.ERROR)
        self.assertIsNotNone(lease.released_at)
        self.assertEqual(lease.release_reason, "provisioning-failed")

    def test_inspection_projects_active_expired_and_released_lease_state(self) -> None:
        workspace = self._acquire("inspect-exec", self.repo.id)
        current = self.service.inspect(self.actor)
        item = next(entry for entry in current if entry.workspace.id == workspace.id)

        self.assertEqual(item.lease.id, workspace.lease_id)
        self.assertEqual(item.lease.owner_identity_id, self.actor.identity_id)
        self.assertEqual(item.lease.mode, LeaseMode.WRITE)
        self.assertTrue(item.lease_active)
        self.assertFalse(item.lease_expired)

        future = self.service.inspect(self.actor, now=time.time() + 31)
        expired = next(entry for entry in future if entry.workspace.id == workspace.id)
        self.assertFalse(expired.lease_active)
        self.assertTrue(expired.lease_expired)

        self.service.release(
            workspace.id,
            ExecutionWorkspaceRelease(reason="inspection release"),
            actor=self.actor,
        )
        released = next(
            entry
            for entry in self.service.inspect(self.actor, now=time.time() + 31)
            if entry.workspace.id == workspace.id
        )
        self.assertFalse(released.lease_active)
        self.assertFalse(released.lease_expired)
        self.assertIsNotNone(released.lease.released_at)
        self.assertEqual(released.lease.release_reason, "inspection release")

    def test_inspection_is_tenant_scoped(self) -> None:
        self._acquire("tenant-visible", self.repo.id)
        other_actor = self.actor.model_copy(
            update={
                "organization_id": "other-org",
                "workspace_id": "other-workspace",
            }
        )

        self.assertEqual(self.service.inspect(other_actor), [])

    def test_workspace_reference_is_projected_into_execution_contract(self) -> None:
        workspace = self._acquire("contract-exec", self.repo.id)
        state = self.host.states[self.work_item.ref]

        self.assertIsNotNone(state.execution.workspace)
        self.assertEqual(state.execution.workspace.workspace_id, workspace.id)
        self.assertEqual(state.execution.workspace.base_revision, "base-revision")

        contract = execution_contract_for_work_item(
            state,
            ROLE_CONTRACTS["james"],
        )
        self.assertEqual(contract.schema_version, "1.4")
        self.assertEqual(contract.target.workspace.workspace_id, workspace.id)
        self.assertEqual(contract.target.workspace.branch_name, workspace.branch_name)
        self.assertEqual(contract.target.workspace.base_revision, "base-revision")
        self.assertEqual(contract.target.workspace.resource_ids, (self.repo.id,))


if __name__ == "__main__":
    unittest.main()
