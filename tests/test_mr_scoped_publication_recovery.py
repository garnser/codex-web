"""Canonical MR recovery and GitLab edge; no application/server imports."""
import subprocess
import time
import unittest

from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.execution_subjects import ExecutionSubject
from codex_web.execution_workers import ExecutionWorker
from codex_web.models import Project, TaskSourceIdentity, WorkItemState
from codex_web.resources import RepositoryExecutionScope
from codex_web.services.action_providers import ActionRequirementError
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.execution_profile_definitions import install_execution_profile_definitions
from codex_web.services.execution_workspaces import ExecutionWorkspaceService
from codex_web.services.publication_recovery import ASSIGNMENTS, WORKERS
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.execution_workspaces import ExecutionWorkspaceStateStore
from tests import test_gitlab_action_provider as gitlab_fixtures
from tests import test_control_plane_broker as broker_fixtures
from tests import test_verified_publication_events as event_fixtures


class MRScopedPublicationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = gitlab_fixtures.GitLabActionProviderTests()
        await self.f.asyncSetUp()
        self.addAsyncCleanup(self.f.asyncTearDown)
        self.request = self.f._existing_mr_request().model_copy(update={
            'project_id': 'project-a', 'requested_by': self.f.actor.identity_id})
        self.backend = self.f.sqlite
        old = self.f.recovery_state
        subject = ExecutionSubject(kind='thread_bootstrap', ref='mr-recovery')
        ws = old.workspaces[0].model_copy(update={'subject': subject, 'work_item_ref': None})
        lease = old.leases[0].model_copy(update={'subject': subject, 'work_item_ref': None})
        self.store = ExecutionWorkspaceStateStore(self.backend)
        self.store.update(lambda s: s.model_copy(update={'workspaces': [ws], 'leases': [lease]}))
        workspaces = ExecutionWorkspaceService(self.store, self.f.provider.contract.workspaces.backend,
            self.f.resources, lambda _: Project(id='project-a', name='A', path=str(ws.path)))
        self.f.provider.contract.workspaces = workspaces
        registry = DefinitionRegistryService(DefinitionRegistryStore(self.backend))
        _, pin = install_execution_profile_definitions(registry).resolve('repository-write')
        self.assignment = broker_fixtures.ControlPlaneBrokerTests._assignment().model_copy(update={
            'execution_id': ws.execution_id, 'execution_workspace_id': ws.id, 'subject': subject,
            'execution_profile_id': 'repository-write', 'execution_profile_definition': pin,
            'resource_ids': ws.resource_ids,
            'repository_scope': RepositoryExecutionScope(organization_id='local', workspace_id='default',
                project_id='project-a', writable_repository_ids=(self.f.repository.id,),
                source='single_repository', source_ref=self.f.repository.id),
            'agent_profile': AgentProfileExecutionBinding(profile_id='carl', role_id='carl',
                profile_revision=6, profile_record_id='carl-r6')})
        self.backend.record_apply(ASSIGNMENTS, upserts={self.assignment.id: self.assignment.model_dump(mode='json')})
        worker = ExecutionWorker(id='worker-1', organization_id='local', workspace_id='default',
            service_identity_id='service-worker', version='fixture', registered_by='fixture',
            capabilities=('command_execution', 'git'),
            supported_execution_contract_versions=(self.assignment.execution_contract_version,))
        self.backend.record_apply(WORKERS, upserts={worker.id: worker.model_dump(mode='json')})
        self.ref = 'group/platform/codex-web!258'
        self.work = WorkItemState(ref=self.ref, project_id='project-a', project_path='group/platform/codex-web',
            kind='merge_request', current_owner='carl', resource_ids=[self.f.repository.id],
            last_meaningful_update_at=time.time(), updated_at=time.time(), created_at=time.time(),
            source_identity=TaskSourceIdentity(source_type='gitlab', source_instance='https://gitlab.example/api/v4',
                external_id=self.ref))
        self.write_work()
        # Normal existing admission is deliberately tested, not a fabricated marker.
        self.workspace = workspaces.authorize_publication_recovery(ws.id, work_item_ref=self.ref,
            repository_resource_id=self.f.repository.id, assignment_id=self.assignment.id,
            assignment_fence=self.assignment.fence, source_execution_id=ws.execution_id,
            source_subject=subject, assignment_deadline_at=self.assignment.deadline_at, actor=self.f.actor)
        self.f.client.merge_request_rows[0]['project_id'] = 5
        async def project(*args, **kwargs):
            return {'id': 5, 'path_with_namespace': 'group/platform/codex-web'}
        self.f.client.project = project

    def write_work(self):
        self.backend.record_apply('work_item_states', upserts={self.ref: self.work.model_dump(mode='json')})

    async def publish(self):
        return await self.f.execution.execute(self.f.binding.id, self.request, actor=self.f.actor)

    async def test_real_recovery_admission_can_publish_exact_owned_mr(self):
        self.assertEqual(self.workspace.publication_recovery.work_item_ref, self.ref)
        result = await self.publish()
        self.assertEqual(result.output['work_item_merge_request_number'], 258)
        self.assertIsNone(result.output['work_item_issue_number'])
        self.assertEqual(self.f.client.related_issue_reads, [])
        self.assertEqual(self.f.published[5], self.f.head)
        self.assertTrue((await self.f.execution.verify(self.f.binding.id, result, actor=self.f.actor)).verified)

    async def test_current_work_scope_and_owner_are_required(self):
        for change in [{'current_owner': 'james'}, {'project_id': 'foreign'}, {'workspace_id': 'foreign'},
                       {'resource_ids': ['foreign']}, {'kind': 'issue'}, {'current_stage': 'closed'},
                       {'project_path': 'other/repo'}, {'source_identity': None}]:
            with self.subTest(change=change):
                old = self.work
                self.work = old.model_copy(update=change); self.write_work()
                with self.assertRaises(ActionRequirementError): await self.publish()
                self.assertIsNone(self.f.published)
                self.work = old; self.write_work()

    async def test_wrong_mr_boolean_and_requester_refuse(self):
        for change in [{'existing_change_request_number': True}, {'existing_change_request_number': 259}]:
            with self.subTest(change=change):
                old = self.request
                self.request = old.model_copy(update={'parameters': {**old.parameters, **change}})
                with self.assertRaises(ActionRequirementError): await self.publish()
                self.assertIsNone(self.f.published); self.request = old
        self.request = self.request.model_copy(update={'requested_by': 'foreign'})
        with self.assertRaises(ActionRequirementError): await self.publish()
        self.assertIsNone(self.f.published)

    async def test_assignment_fence_and_lease_refuse(self):
        for change in [{'fence': 2}, {'project_id': 'foreign'}, {'created_by': 'foreign'}, {'status': 'cancelled'}]:
            with self.subTest(change=change):
                changed = self.assignment.model_copy(update=change)
                self.backend.record_apply(ASSIGNMENTS, upserts={changed.id: changed.model_dump(mode='json')})
                with self.assertRaises(ActionRequirementError): await self.publish()
                self.assertIsNone(self.f.published)
                self.backend.record_apply(ASSIGNMENTS, upserts={self.assignment.id: self.assignment.model_dump(mode='json')})
        self.store.update(lambda s: s.model_copy(update={'leases': [s.leases[0].model_copy(update={'expires_at': 0})]}))
        with self.assertRaises(ActionRequirementError): await self.publish()
        self.assertIsNone(self.f.published)

    async def test_provider_identity_head_and_protection_refuse(self):
        row = self.f.client.merge_request_rows[0]
        for field, value in [('iid', 259), ('project_id', 6), ('source_project_id', 6),
                             ('target_project_id', 6), ('state', 'merged'), ('source_branch', 'main'),
                             ('target_branch', self.f.legacy_branch), ('sha', 'f'*40)]:
            with self.subTest(field=field):
                old = row[field]; row[field] = value
                with self.assertRaises(ActionRequirementError): await self.publish()
                self.assertIsNone(self.f.published); row[field] = old
        self.f.client.branch_protected = True
        with self.assertRaises(ActionRequirementError): await self.publish()
        self.assertIsNone(self.f.published)
        self.f.client.branch_protected = False
        async def foreign_project(*args, **kwargs): return {'id': 5, 'path_with_namespace': 'other/repo'}
        self.f.client.project = foreign_project
        with self.assertRaises(ActionRequirementError): await self.publish()
        self.assertIsNone(self.f.published)

    async def test_owner_change_during_remote_reads_refuses_before_push(self):
        original = self.f.client.branch
        async def branch(*args, **kwargs):
            value = await original(*args, **kwargs)
            self.work = self.work.model_copy(update={'current_owner': 'james'}); self.write_work()
            return value
        self.f.client.branch = branch
        with self.assertRaises(ActionRequirementError): await self.publish()
        self.assertIsNone(self.f.published)

    async def test_dirty_head_and_remote_cas_remain_required(self):
        path = self.f.provider.contract.workspaces.backend.root / 'workspace-a'
        (path / 'delivery.txt').write_text('dirty\n')
        with self.assertRaises(ActionRequirementError): await self.publish()
        self.assertIsNone(self.f.published)
        subprocess.run(['git', 'checkout', '--', 'delivery.txt'], cwd=path, check=True, capture_output=True)
        async def raced_push(path, project, branch, revision, credential, expected):
            self.f.client.branches[branch] = 'f'*40
            if self.f.client.branches[branch] != expected: raise RuntimeError('compare-and-swap refused')
            raise AssertionError('must not push')
        self.f.provider.branch_publisher = raced_push
        with self.assertRaisesRegex(RuntimeError, 'compare-and-swap'): await self.publish()
        self.assertIsNone(self.f.published)

    async def test_default_branch_and_unrelated_local_history_refuse(self):
        original = self.f.client.branch
        async def default_branch(*args, **kwargs):
            value = await original(*args, **kwargs)
            return {**value, 'default': True}
        self.f.client.branch = default_branch
        with self.assertRaises(ActionRequirementError): await self.publish()
        self.assertIsNone(self.f.published)
        self.f.client.branch = original
        path = self.f.provider.contract.workspaces.backend.root / 'workspace-a'
        subprocess.run(['git', 'checkout', '--orphan', 'orphan-check'], cwd=path, check=True, capture_output=True)
        subprocess.run(['git', 'commit', '-m', 'Unrelated history'], cwd=path, check=True, capture_output=True)
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=path, text=True).strip()
        self.store.update(lambda s: s.model_copy(update={'workspaces': [s.workspaces[0].model_copy(update={
            'repository_members': (s.workspaces[0].repository_members[0].model_copy(update={'branch_name': 'orphan-check'}),)})]}))
        self.request = self.request.model_copy(update={'parameters': {**self.request.parameters, 'head_revision': head}})
        with self.assertRaises(ActionRequirementError): await self.publish()
        self.assertIsNone(self.f.published)


class MRScopedPublicationEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = event_fixtures.VerifiedPublicationEventTests()
        await self.f.asyncSetUp()
        self.addCleanup(self.f.doCleanups)
        self.ref = 'group/project!291'
        work = self.f.original.model_copy(update={'ref': self.ref, 'kind': 'merge_request'})
        self.f.backend.record_apply('work_item_states', upserts={self.ref: work.model_dump(mode='json')})
        records = self.f.store.load()
        intent = next(x for x in records.intents if x.action_id == 'code-host.branch.publish')
        intent = intent.model_copy(update={'work_item_ref': self.ref, 'request': intent.request.model_copy(update={
            'parameters': {**intent.request.parameters, 'existing_change_request_number': 291}})})
        receipt = next(x for x in records.receipts if x.id == intent.last_receipt_id)
        self.receipt = receipt.model_copy(update={'result': receipt.result.model_copy(update={'output': {
            **receipt.result.output, 'existing_change_request_number': 291,
            'work_item_merge_request_number': 291, 'work_item_issue_number': None}})})
        records.intents = [intent]
        records.receipts = [self.receipt]
        records.verifications = [x for x in records.verifications if x.id == intent.last_verification_id]
        self.f.store.update(lambda _: records)

    async def test_verified_mr_only_publication_routes_same_canonical_work(self):
        route = self.f.resolve()
        self.assertEqual(route.state.ref, self.ref)
        self.assertEqual(route.mr_ref, self.ref)
        self.assertIsNone(route.request_intent_id)

    async def test_forged_or_mismatched_mr_proof_refuses(self):
        for value in [None, True, '291', 292]:
            with self.subTest(value=value):
                records = self.f.store.load()
                records.receipts = [self.receipt.model_copy(update={'result': self.receipt.result.model_copy(update={
                    'output': {**self.receipt.result.output, 'work_item_merge_request_number': value}})})]
                self.f.store.update(lambda _: records)
                with self.assertRaisesRegex(ValueError, 'ambiguous_or_stale'): self.f.resolve()
