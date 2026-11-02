"""Canonical keyed recovery and Home selection, with a synthetic provider edge."""
import copy
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from codex_web.execution_workers import ExecutionAssignment
from codex_web.models import HomeOperatorUpdate, IndexedThread, ThreadRunSettings
from codex_web.services.home_operator import HomeOperatorConflict, HomeOperatorService
from codex_web.services.projects import ProjectService
from codex_web.services.thread_scope import ThreadScopeService
from codex_web.storage.execution_workers import ExecutionWorkerStore
from codex_web.storage.projects import ProjectRepository
from tests import test_queued_bootstrap_runtime as runtime_cases
from tests import test_queued_bootstrap_recovery as reservation_cases


class HomeScratchBootstrapRecoveryTests(unittest.IsolatedAsyncioTestCase):
    reopen = reservation_cases.QueuedBootstrapRecoveryTests.reopen
    make_runtime = runtime_cases.QueuedBootstrapRuntimeTests.make_runtime
    recover = runtime_cases.QueuedBootstrapRuntimeTests.recover

    def setUp(self):
        runtime_cases.QueuedBootstrapRuntimeTests.setUp(self)
        self.project = self.project.model_copy(update={'id': 'home', 'name': 'Home'})
        self.index.upsert(IndexedThread(id='archived', name='Permanent operator', project_id='home'))
        self.queued = self.queued.model_copy(update={
            'id': 'home-original', 'project_id': 'home', 'work_item_ref': None, 'reply_target': None,
            'execution_profile_id': 'orchestration-only', 'agent_profile_id': 'home-operator',
            'agent_profile_revision': 1, 'repository_resource_id': None,
            'writable_repository_resource_ids': (), 'read_only_repository_resource_ids': (),
        })
        self.attempt = self.service.retain_queued(self.queued, organization_id='org', workspace_id='ws')
        self.digest = self.service.queued_snapshot_digest(self.attempt.queued_turn_snapshot)
        self.scope = dict(organization_id='org', workspace_id='ws', project_id='home',
            source='orchestration_only', source_ref='explicit', writable_repository_ids=[],
            read_only_repository_ids=[], write_mode='single', selection_evidence=[])
        self.proof = dict(code='native_session_prior_assignment_missing', message='Refused before resume',
            phase='before_provider_resume', threadId='archived', preflight_attempt_id=self.attempt.id,
            admission_claim_id='owned-claim', snapshot_sha256=self.digest, bootstrap_id='old-bootstrap',
            assignment_id='old-assignment', previous_assignment_id='missing-predecessor',
            agent_session_id='original-session', assignment_fence=1, execution_id='old-execution',
            execution_workspace_id='old-workspace', original_resource_ids=[], original_repository_scope=None,
            requested_resource_ids=[], requested_repository_scope=self.scope)
        self.record(self.proof)
        self.bindings.bind(bootstrap_id='old-bootstrap', thread_id='archived', execution_id='old-execution',
            assignment_id='old-assignment', execution_workspace_id='old-workspace', actor=self.admin)
        self.worker_store = ExecutionWorkerStore(self.service.store.store)
        self.persist('old-assignment', 'old-bootstrap', 'old-execution', 'old-workspace', 'cancelled', 2)
        self.runtime.assignments = self.worker_store.assignment
        repository = ProjectRepository(Path(self.directory.name) / 'projects.json', store=self.service.store.store)
        repository.save([self.project])
        self.projects = ProjectService(repository)
        settings = SimpleNamespace(get=lambda key: ThreadRunSettings(execution_profile_id='orchestration-only'))
        sessions = SimpleNamespace(store=SimpleNamespace(native_owner=lambda key: ('org', 'ws', 'home')))
        profile = SimpleNamespace(profile_id='home-operator', revision=1, name='Home',
                                  execution_profile_id='orchestration-only')
        self.home = HomeOperatorService(projects=self.projects, thread_index=self.index, settings=settings,
            thread_scope=ThreadScopeService(self.projects, sessions, self.index),
            agent_profiles=SimpleNamespace(resolve_for_execution=lambda *a, **kw: (profile, None)))
        self.selected = self.home.configure(HomeOperatorUpdate(thread_id='archived', expected_revision=0,
            agent_profile_id='home-operator'), self.admin)

    def record(self, proof):
        self.attempt = self.service.store.update(self.attempt.id, lambda a: a.model_copy(update={
            'queued_admission_claim_id': 'owned-claim'}))
        self.attempt = self.service.queued_disposition(self.attempt.id, status='blocked', detail=proof,
            claim_id='owned-claim', preinput_refusal=True)

    def persist(self, key, bootstrap, execution, workspace, status, fence):
        model = ExecutionAssignment(id=key, organization_id='org', workspace_id='ws', project_id='home',
            subject={'kind': 'thread_bootstrap', 'ref': bootstrap}, execution_id=execution,
            execution_workspace_id=workspace, resource_ids=(), execution_contract_version='1.0',
            required_capabilities=(), sandbox='workspace-write', approval_policy='on-request',
            network={}, limits={}, repository_scope=self.scope, execution_profile_id='orchestration-only',
            agent_profile={'profile_id': 'home-operator', 'profile_revision': 1, 'profile_record_id': 'exact-profile'},
            created_by='admin', status=status, fence=fence)
        self.worker_store._ensure_records()
        self.service.store.store.record_apply(self.worker_store.assignment_namespace,
            upserts={key: model.model_dump(mode='json')})

    async def create(self, **values):
        recovery = self.service.store.get(self.attempt.id).bootstrap_recovery
        self.assertIsNotNone(recovery)
        self.assertEqual(values['actor'].identity_id, 'admin')
        self.assertEqual(values['project_id'], 'home')
        self.assertEqual(values['agent_profile_id'], 'home-operator')
        self.assertEqual(values['agent_profile_revision'], 1)
        self.assertEqual(values['execution_profile_id'], 'orchestration-only')
        self.assertIsNone(values['repository_resource_id'])
        self.assertEqual(values['writable_repository_resource_ids'], ())
        self.assertEqual(values['read_only_repository_resource_ids'], ())
        self.index.upsert(IndexedThread(id='fresh', name='Recovered operator', project_id='home'))
        self.persist('assignment', recovery.bootstrap_id, recovery.execution_id, 'workspace', 'running', 1)
        self.bindings.bind(bootstrap_id=recovery.bootstrap_id, thread_id='fresh',
            execution_id=recovery.execution_id, assignment_id='assignment',
            execution_workspace_id='workspace', actor=self.admin)
        return {'thread': {'id': 'fresh'}}

    async def test_exact_scratch_recovery_requires_explicit_home_reselection_before_original_retry(self):
        self.assertTrue(self.service.public(self.attempt, actor=self.admin)['can_recover_bootstrap'])
        original = self.attempt.queued_turn_snapshot
        old_row = self.worker_store.assignment('old-assignment').model_dump(mode='json')
        result = await self.recover()
        self.assertEqual(result['threadId'], 'fresh')
        self.assertFalse(result['native_turn_started'])
        self.assertIsNone(self.worker_store.assignment('missing-predecessor'))
        self.assertEqual(self.worker_store.assignment('old-assignment').model_dump(mode='json'), old_row)
        self.assertEqual(self.home.status(self.admin)['status'], 'unavailable')
        self.assertEqual(self.home.status(self.admin)['binding'], self.selected['binding'])
        with self.assertRaisesRegex(HomeOperatorConflict, 'current conversation'):
            self.home.configure(HomeOperatorUpdate(thread_id='archived', expected_revision=1,
                agent_profile_id='home-operator'), self.admin)
        selected = self.home.configure(HomeOperatorUpdate(thread_id='fresh', expected_revision=1,
            agent_profile_id='home-operator'), self.admin)
        self.assertEqual(selected['status'], 'configured')
        self.assertEqual(selected['binding']['revision'], 2)
        self.assertEqual(selected['binding']['thread_id'], 'fresh')
        current = self.service.store.get(self.attempt.id)
        self.assertEqual(current.queued_turn_snapshot, original)
        self.assertEqual(current.status, 'blocked')
        self.assertIsNone(current.queued_start_receipt)
        self.assertFalse(self.index.get('archived').archived)
        await self.recover()
        self.assertEqual(self.threads.create.await_count, 1)
        _, claim, acquired = self.service.claim_retry(current.id, actor=self.admin)
        self.assertTrue(acquired)
        self.assertTrue(claim)
        self.assertEqual(self.service.store.get(current.id).queued_turn_snapshot, original)

    async def test_empty_scope_does_not_authorize_foreign_or_undeclared_repository_recovery(self):
        changes = [dict(project_id='foreign'), dict(organization_id='foreign'),
                   dict(workspace_id='foreign'), dict(source='explicit'), dict(source_ref='inferred'),
                   dict(writable_repository_ids=['repo']), dict(read_only_repository_ids=['repo'])]
        for change in changes:
            with self.subTest(change=change):
                proof = copy.deepcopy(self.proof)
                proof['requested_repository_scope'].update(change)
                self.record(proof)
                self.assertFalse(self.service.recorded_custody_refusal(self.attempt))
                with self.assertRaises(HTTPException):
                    await self.recover()
        self.threads.create.assert_not_called()

    async def test_snapshot_profile_or_repository_drift_does_not_acquire_scratch_recovery(self):
        snapshot = self.attempt.queued_turn_snapshot
        for change in ({'agent_profile_id': None}, {'agent_profile_revision': None},
                       {'agent_profile_actor_id': None}, {'execution_profile_id': 'repository-write'},
                       {'project_id': 'foreign'}, {'repository_resource_id': 'repo'},
                       {'writable_repository_resource_ids': ['repo']},
                       {'read_only_repository_resource_ids': ['repo']}):
            with self.subTest(change=change):
                altered = dict(snapshot, **change)
                proof = dict(self.proof, snapshot_sha256=self.service.queued_snapshot_digest(altered))
                self.service.store.update(self.attempt.id, lambda a: a.model_copy(update={
                    'queued_turn_snapshot': altered}))
                self.record(proof)
                self.assertFalse(self.service.recorded_custody_refusal(self.attempt))
        self.threads.create.assert_not_called()

    async def test_unknown_or_changed_claim_never_uses_old_pre_resume_refusal(self):
        for changes in ({'status': 'reconciliation_required'}, {'queued_admission_claim_id': 'new-claim'},
                        {'started_at': 123.0}):
            with self.subTest(changes=changes):
                current = self.attempt.model_copy(update=changes)
                self.assertFalse(self.service.recorded_custody_refusal(current))
        self.threads.create.assert_not_called()

    async def test_downstream_revalidates_current_actor_scope_fence_and_missing_predecessor(self):
        original = self.worker_store.assignment('old-assignment')
        for changes in ({'fence': 3}, {'created_by': 'other'}, {'resource_ids': ('repo',)}):
            with self.subTest(changes=changes):
                self.service.store.store.record_apply(self.worker_store.assignment_namespace,
                    upserts={original.id: original.model_copy(update=changes).model_dump(mode='json')})
                with self.assertRaises(HTTPException):
                    await self.recover()
        self.service.store.store.record_apply(self.worker_store.assignment_namespace,
            upserts={original.id: original.model_dump(mode='json'),
                     'missing-predecessor': original.model_copy(update={'id': 'missing-predecessor'}).model_dump(mode='json')})
        with self.assertRaises(HTTPException):
            await self.recover()
        self.threads.create.assert_not_called()

    async def test_changed_fresh_profile_after_creation_never_publishes_replacement(self):
        create = self.create
        async def changed(**values):
            result = await create(**values)
            current = self.worker_store.assignment('assignment')
            profile = current.agent_profile.model_copy(update={'profile_revision': 2})
            self.service.store.store.record_apply(self.worker_store.assignment_namespace,
                upserts={current.id: current.model_copy(update={'agent_profile': profile}).model_dump(mode='json')})
            return result
        self.threads.create.side_effect = changed
        with self.assertRaises(HTTPException):
            await self.recover()
        self.assertIsNone(self.index.replacement_thread_id('archived'))
        self.assertEqual(self.home.status(self.admin)['binding'], self.selected['binding'])
        self.assertEqual(self.service.store.get(self.attempt.id).bootstrap_recovery.state, 'pending')
        self.assertIsNone(self.service.store.get(self.attempt.id).queued_start_receipt)
