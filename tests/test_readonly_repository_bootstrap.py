"""Real private bootstrap/workspace owners; Git materialization is controlled."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.execution_workers import ExecutionAssignmentCreate, ExecutionWorkerState
from codex_web.execution_workspaces import ExecutionWorkspaceAcquire, ExecutionWorkspaceState, LeaseMode
from codex_web.resources import RepositoryExecutionScope, RepositoryExecutionTarget, ResourceAlias, ResourceCreate, ResourceType
from codex_web.services.execution_workers import WorkerConflictError
from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime, LocalExecutionWorkerRuntimeError
from codex_web.services.turn_execution_binding import TurnExecutionBindingError
from tests import test_turn_execution_binding as binding_fixtures


class ReadonlyRepositoryBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.f = binding_fixtures.TurnExecutionBindingTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f._publish_secret()

    def prepare(self, **kwargs):
        return self.f.service.prepare_bootstrap(
            bootstrap_id='readonly-bootstrap', execution_id='readonly-execution',
            project_id=self.f.project.id, sandbox='read-only', approval_policy='on-request',
            explicit_repository_id=self.f.repository.id, **kwargs)

    def assignment(self, binding):
        return self.f.workers.assignment_for_execution(binding.execution_id, actor=self.f.actor)

    def custom_profile(self):
        profiles = self.f.execution_profiles
        active = profiles.record(organization_id=self.f.actor.organization_id,
            workspace_id=self.f.actor.workspace_id, project_id=self.f.project.id)
        payload = deepcopy(active.payload)
        profile = deepcopy(next(x for x in payload['profiles'] if x['id'] == 'repository-write'))
        profile.update(id='readonly-repository', repository_access='read-only')
        payload['profiles'].append(profile)
        draft = profiles.registry.create_draft(DefinitionDraftCreate(
            definition_id=active.definition_id, kind=active.kind,
            definition_schema_version=active.definition_schema_version,
            payload=payload, actor=self.f.actor.identity_id, reason='Private READ bootstrap fixture'))
        profiles.registry.approve_publication(draft.record_id, actor='private-reviewer',
            reference='PRIVATE', reason='Controlled fixture')
        profiles.registry.publish(draft.record_id, DefinitionPublishRequest(actor=self.f.actor.identity_id,
            expected_active_revision=active.revision, reason='Private READ profile'))
        return profile['id']

    def second_repository(self):
        path = Path(self.f.temp.name) / 'second-source'
        path.mkdir()
        repo = self.f.resources.create(ResourceCreate(resource_type=ResourceType.REPOSITORY,
            name='Second READ context', aliases=[ResourceAlias(namespace='filesystem', value=str(path))]),
            actor=self.f.actor)
        self.f.resources.bind_project(project=self.f.project, resource_id=repo.id, actor=self.f.actor)
        return repo

    def payload(self, assignment):
        return ExecutionAssignmentCreate.model_validate({k: v for k, v in assignment.model_dump().items()
            if k in ExecutionAssignmentCreate.model_fields})

    def test_standard_readonly_bootstrap_has_actual_read_primary_and_zero_write(self):
        error = None
        binding = None
        try:
            binding = self.prepare()
        except Exception as exc:
            error = exc
        self.assertIsNone(error, str(error))
        assignment = self.assignment(binding)
        workspace = self.f.workspaces.get(binding.workspace_id, self.f.actor)
        lease = self.f.workspaces.store.lease(workspace.lease_id)
        self.assertIsNone(assignment.repository_target.mutable_repository_id)
        self.assertEqual(assignment.repository_target.read_only_primary_repository_id, self.f.repository.id)
        self.assertEqual(assignment.repository_scope.write_mode, 'read_only')
        self.assertEqual(assignment.repository_scope.writable_repository_ids, ())
        self.assertEqual(workspace.writable_repository_ids, ())
        self.assertEqual(workspace.repository_resource_id, self.f.repository.id)
        self.assertEqual(workspace.read_only_primary_repository_id, self.f.repository.id)
        self.assertEqual(workspace.path, workspace.repository_members[0].workspace_path)
        self.assertEqual(workspace.repository_members[0].access_mode, LeaseMode.READ)
        self.assertEqual(lease.mode, LeaseMode.READ)
        self.assertEqual(lease.resource_modes, {self.f.repository.id: LeaseMode.READ})
        self.assertEqual(set(assignment.resource_ids), {self.f.repository.id})
        self.assertEqual(assignment.sandbox, 'read-only')
        self.assertEqual(assignment.repository_target.source, 'explicit')
        self.assertEqual(assignment.repository_scope.selection_evidence,
                         assignment.repository_target.selection_evidence)

    def test_custom_readonly_profile_uses_same_actual_builder(self):
        binding = self.prepare(execution_profile_id=self.custom_profile())
        self.assertEqual(self.assignment(binding).repository_scope.writable_repository_ids, ())
        self.assertEqual(self.f.workspaces.get(binding.workspace_id, self.f.actor).kind, 'git_worktree')

    def test_readonly_multiple_repositories_preserve_primary_and_context(self):
        other = self.second_repository()
        binding = self.prepare(read_only_repository_ids=(other.id,))
        assignment = self.assignment(binding)
        self.assertEqual(assignment.repository_target.primary_repository_id, self.f.repository.id)
        self.assertEqual(assignment.repository_scope.read_only_repository_ids, (self.f.repository.id, other.id))
        workspace = self.f.workspaces.get(binding.workspace_id, self.f.actor)
        self.assertEqual({m.resource_id: m.access_mode for m in workspace.repository_members},
                         {self.f.repository.id: LeaseMode.READ, other.id: LeaseMode.READ})

    def test_explicit_writable_request_under_readonly_sandbox_is_refused(self):
        with self.assertRaises(TurnExecutionBindingError):
            self.prepare(writable_repository_ids=(self.f.repository.id,))
        self.assertEqual(self.f.workers.list_assignments(self.f.actor), [])

    def test_coordinated_project_does_not_expand_readonly_explicit_selection(self):
        other = self.second_repository()
        self.f.project.repository_selection_policy = 'coordinated'
        assignment = self.assignment(self.prepare())
        self.assertEqual(assignment.resource_ids, (self.f.repository.id,))
        self.assertNotIn(other.id, assignment.repository_scope.read_only_repository_ids)

    def test_readonly_acquire_refuses_explicit_write_collection(self):
        with self.assertRaisesRegex(ValidationError, 'READ lease cannot request writable repositories'):
            ExecutionWorkspaceAcquire(subject={'kind': 'thread_bootstrap', 'ref': 'invalid-read-bootstrap'},
                execution_id='invalid', project_id=self.f.project.id,
                resource_ids=(self.f.repository.id,), repository_resource_id=self.f.repository.id,
                writable_repository_ids=(self.f.repository.id,), lease_mode=LeaseMode.READ)

    def test_readonly_scope_rejects_write_empty_or_orchestration_masquerade(self):
        base = dict(organization_id=self.f.actor.organization_id, workspace_id=self.f.actor.workspace_id,
            project_id=self.f.project.id, write_mode='read_only', source='explicit',
            read_only_repository_ids=(self.f.repository.id,))
        for change in ({'writable_repository_ids': (self.f.repository.id,)},
                       {'read_only_repository_ids': ()}, {'source': 'orchestration_only'}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                RepositoryExecutionScope.model_validate({**base, **change})

    def test_readonly_target_rejects_mutable_or_unscoped_primary(self):
        base = dict(organization_id=self.f.actor.organization_id, workspace_id=self.f.actor.workspace_id,
            project_id=self.f.project.id, source='explicit', read_only_primary_repository_id=self.f.repository.id,
            read_only_repository_ids=(self.f.repository.id,))
        for change in ({'mutable_repository_id': self.f.repository.id}, {'read_only_repository_ids': ()}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                RepositoryExecutionTarget.model_validate({**base, **change})

    def test_actual_read_lease_expiry_refuses_assignment_before_duplicate_write(self):
        assignment = self.assignment(self.prepare())
        workspace = self.f.workspaces.get(assignment.execution_workspace_id, self.f.actor)
        def expire(state):
            state.leases = [l.model_copy(update={'expires_at': 0}) if l.id == workspace.lease_id else l
                            for l in state.leases]
            return state
        self.f.workspaces.store.update(expire)
        with self.assertRaisesRegex(WorkerConflictError, 'exact live READ lease'):
            self.f.workers.create_assignment(self.payload(assignment), actor=self.f.actor)

    def test_write_member_cannot_satisfy_readonly_assignment(self):
        assignment = self.assignment(self.prepare())
        workspace = self.f.workspaces.get(assignment.execution_workspace_id, self.f.actor)
        wrong = workspace.model_copy(update={'repository_members': tuple(
            m.model_copy(update={'access_mode': LeaseMode.WRITE}) for m in workspace.repository_members)})
        with patch.object(self.f.workspaces, 'get', return_value=wrong):
            with self.assertRaises(WorkerConflictError):
                self.f.workers.create_assignment(self.payload(assignment), actor=self.f.actor)
        self.assertEqual(len(self.f.workers.list_assignments(self.f.actor)), 1)

    def test_new_readonly_rows_emit_new_contract_without_legacy_backfill(self):
        assignment = self.assignment(self.prepare())
        workspace = self.f.workspaces.get(assignment.execution_workspace_id, self.f.actor)
        self.assertEqual(ExecutionWorkerState(assignments=[assignment]).model_dump()['schema_version'], '1.15')
        self.assertEqual(ExecutionWorkspaceState(workspaces=[workspace]).model_dump()['schema_version'], '1.9')
        self.assertEqual(ExecutionWorkerState().model_dump()['schema_version'], '1.14')
        self.assertEqual(ExecutionWorkspaceState().model_dump()['schema_version'], '1.8')

    def test_mutable_target_scope_and_workspace_keep_legacy_serialization(self):
        binding = self.f._prepare()
        assignment = self.assignment(binding)
        workspace = self.f.workspaces.get(binding.workspace_id, self.f.actor)
        self.assertNotIn('read_only_primary_repository_id', assignment.repository_target.model_dump())
        self.assertNotIn('read_only_primary_repository_id', workspace.model_dump())
        self.assertEqual(assignment.repository_scope.write_mode, 'single')
        self.assertEqual(assignment.repository_scope.writable_repository_ids, (self.f.repository.id,))
        self.assertEqual(ExecutionWorkerState(assignments=[assignment]).model_dump()['schema_version'], '1.14')
        self.assertEqual(ExecutionWorkspaceState(workspaces=[workspace]).model_dump()['schema_version'], '1.8')

    def test_local_mount_owner_accepts_read_primary_and_rejects_forged_write_member(self):
        assignment = self.assignment(self.prepare())
        runtime = LocalExecutionWorkerRuntime(self.f.workers, self.f.workspaces, None,
            worker=self.f.worker, worker_actor=self.f.actor, control_actor=self.f.actor)
        self.assertEqual(runtime.repository_mounts(assignment), ((), ()))
        workspace = self.f.workspaces.get(assignment.execution_workspace_id, self.f.actor)
        wrong = workspace.model_copy(update={'repository_members': tuple(
            m.model_copy(update={'access_mode': LeaseMode.WRITE}) for m in workspace.repository_members)})
        with patch.object(self.f.workspaces, 'get', return_value=wrong):
            with self.assertRaises(LocalExecutionWorkerRuntimeError):
                runtime.repository_mounts(assignment)

    def test_retained_ingress_read_scope_reaches_normal_bootstrap_without_write_inference(self):
        from codex_web.models import ThreadRunSettings
        from codex_web.services.conversation_channels import capture_repository_selection
        self.second_repository()
        self.f.project.repository_selection_policy = 'coordinated'
        profile, _ = self.f.execution_profiles.resolve('repository-write')
        captured = capture_repository_selection(ThreadRunSettings(sandbox='read-only',
            repository_resource_id=self.f.repository.id), self.f.project, profile,
            self.f.resources, self.f.actor)
        self.assertEqual(captured.writable_repository_resource_ids, ())
        self.assertEqual(captured.read_only_repository_resource_ids, (self.f.repository.id,))
        binding = self.prepare(writable_repository_ids=captured.writable_repository_resource_ids,
            read_only_repository_ids=captured.read_only_repository_resource_ids)
        self.assertEqual(self.assignment(binding).repository_scope.writable_repository_ids, ())

    def test_retained_ingress_refuses_explicit_write_under_read_scope(self):
        from codex_web.models import ThreadRunSettings
        from codex_web.services.conversation_channels import capture_repository_selection
        profile, _ = self.f.execution_profiles.resolve('repository-write')
        with self.assertRaisesRegex(ValueError, 'READ execution cannot capture writable'):
            capture_repository_selection(ThreadRunSettings(sandbox='read-only',
                writable_repository_resource_ids=(self.f.repository.id,)),
                self.f.project, profile, self.f.resources, self.f.actor)

    def test_readonly_custody_cannot_launch_with_write_sandbox(self):
        assignment = self.assignment(self.prepare())
        payload = self.payload(assignment).model_copy(update={'sandbox': 'workspace-write'})
        with self.assertRaisesRegex(WorkerConflictError, 'exact live READ lease'):
            self.f.workers.create_assignment(payload, actor=self.f.actor)

    def test_single_repository_read_acquisition_inference_remains_read_only(self):
        workspace = self.f.workspaces.acquire(ExecutionWorkspaceAcquire(
            subject={'kind': 'thread_bootstrap', 'ref': 'sole-read-bootstrap'},
            execution_id='sole-read', project_id=self.f.project.id,
            resource_ids=(self.f.repository.id,), lease_mode=LeaseMode.READ), actor=self.f.actor)
        self.assertEqual(workspace.repository_resource_id, self.f.repository.id)
        self.assertEqual(workspace.read_only_primary_repository_id, self.f.repository.id)
        self.assertEqual(workspace.writable_repository_ids, ())
        self.assertEqual(workspace.repository_members[0].access_mode, LeaseMode.READ)
