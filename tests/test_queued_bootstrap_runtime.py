from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException

from codex_web.models import IndexedThread, Project, QueuedNativeStartReceipt, ExecutionPreflightBlocker
from codex_web.services.queued_bootstrap_recovery import QueuedBootstrapRecoveryService
from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingService
from codex_web.storage.thread_bootstrap_bindings import ThreadBootstrapBindingStore
from codex_web.storage.thread_index import ThreadIndexRepository
from tests import test_queued_bootstrap_recovery as reservation_tests


class QueuedBootstrapRuntimeTests(unittest.IsolatedAsyncioTestCase):
    reopen = reservation_tests.QueuedBootstrapRecoveryTests.reopen

    def setUp(self):
        reservation_tests.QueuedBootstrapRecoveryTests.setUp(self)
        self.project = Project(id='project', name='Project', path='/fixture',
            organization_id='org', workspace_id='ws')
        self.index = ThreadIndexRepository(self.service.store.store,
            Path(self.directory.name) / 'legacy-threads.json')
        self.index.upsert(IndexedThread(id='archived', name='Original', archived=True, project_id='project'))
        self.bindings = ThreadBootstrapBindingService(ThreadBootstrapBindingStore(self.service.store.store))
        self.assignments = {}
        self.threads = SimpleNamespace(create=AsyncMock(side_effect=self.create))
        self.runtime = self.make_runtime()

    def make_runtime(self):
        return QueuedBootstrapRecoveryService(preflight=self.service, threads=self.threads,
            bootstrap_bindings=self.bindings, assignments=self.assignments.get,
            thread_index=self.index, actor_resolver=lambda identity, project:self.admin,
            thread_is_active=lambda thread:False)

    async def create(self, **values):
        recovery = self.service.store.get(self.attempt.id).bootstrap_recovery
        self.assertIsNotNone(recovery, 'Reservation must exist before runtime I/O')
        self.assertEqual(values['actor'], self.admin)
        self.assertEqual(values['agent_profile_revision'], 6)
        self.assertEqual(values['writable_repository_resource_ids'], ('repo',))
        self.assertEqual(values['read_only_repository_resource_ids'], ('reference',))
        self.assertEqual(values['bootstrap_token'], recovery.bootstrap_id.removeprefix('bootstrap-'))
        self.index.upsert(IndexedThread(id='fresh', name='Fresh', project_id='project'))
        self.assignments['assignment'] = SimpleNamespace(execution_id=recovery.execution_id, created_by='admin',
            status='running', organization_id='org', workspace_id='ws', project_id='project',
            subject=SimpleNamespace(kind='thread_bootstrap', ref=recovery.bootstrap_id),
            execution_workspace_id='workspace', sandbox='workspace-write', approval_policy='on-request',
            execution_profile_id='repository-write',
            agent_profile=SimpleNamespace(profile_id='james', profile_revision=6),
            repository_scope=SimpleNamespace(writable_repository_ids=('repo',), read_only_repository_ids=('reference',)),
            repository_target=SimpleNamespace(mutable_repository_id='repo'))
        self.bindings.bind(bootstrap_id=recovery.bootstrap_id, thread_id='fresh',
            execution_id=recovery.execution_id, assignment_id='assignment',
            execution_workspace_id='workspace', actor=self.admin)
        return {'thread':{'id':'fresh'}}

    async def recover(self):
        return await self.runtime.recover(self.attempt.id, project=self.project,
            actor=self.admin, expected_snapshot_sha256=self.digest, reason='Recover the exact original')

    def prior_pending_route(self, *, recovery=None):
        self.index.upsert(IndexedThread(id='prior', name='Prior', project_id='project'))
        self.bindings.bind(bootstrap_id=recovery.bootstrap_id if recovery else 'prior-bootstrap',
            thread_id='prior', execution_id=recovery.execution_id if recovery else 'prior-execution',
            assignment_id='prior-assignment', execution_workspace_id='prior-workspace', actor=self.admin)
        self.assignments['prior-assignment'] = SimpleNamespace(status='failed', lease=None,
            execution_id=recovery.execution_id if recovery else 'prior-execution',
            organization_id='org', workspace_id='ws', project_id='project')
        self.index.remember_replacement('archived', 'prior', project_id='project',
            provenance={'organization_id':'org', 'workspace_id':'ws'})

    async def recover_from_prior(self, source='prior'):
        return await self.runtime.recover(self.attempt.id, project=self.project, actor=self.admin,
            expected_snapshot_sha256=self.digest, reason='Recover the exact original',
            expected_source_thread_id=source)

    def custody_refusal(self):
        self.index.upsert(IndexedThread(id='archived', name='Original with history', project_id='project'))
        proof = dict(code='native_session_scope_incompatible', message='Refused before resume',
            phase='before_provider_resume', threadId='archived',
            preflight_attempt_id=self.attempt.id, admission_claim_id='owned-claim',
            snapshot_sha256=self.digest, bootstrap_id='old-bootstrap', assignment_id='old-assignment',
            previous_assignment_id='prior-assignment', agent_session_id='original-session',
            assignment_fence=1, execution_id='old-execution', execution_workspace_id='old-workspace',
            original_resource_ids=['other-repo'], requested_resource_ids=['repo', 'reference'],
            original_repository_scope={}, requested_repository_scope={'writable_repository_ids':['repo'],
                'read_only_repository_ids':['reference']})
        self.attempt = self.service.store.update(self.attempt.id, lambda current: current.model_copy(update={
            'blockers':(ExecutionPreflightBlocker(**proof),), 'queued_admission_claim_id':'owned-claim'}))
        self.bindings.bind(bootstrap_id='old-bootstrap', thread_id='archived', execution_id='old-execution',
            assignment_id='old-assignment', execution_workspace_id='old-workspace', actor=self.admin)
        self.assignments['old-assignment'] = SimpleNamespace(status='failed', lease=None, fence=1,
            execution_id='old-execution', execution_workspace_id='old-workspace', created_by='admin',
            organization_id='org', workspace_id='ws', project_id='project',
            agent_profile=SimpleNamespace(profile_id='james', profile_revision=6),
            resource_ids=('repo','reference'), repository_scope=SimpleNamespace(
                model_dump=lambda **kwargs:proof['requested_repository_scope']))
        return proof

    async def test_scoped_custody_refusal_recovers_without_archiving_history_or_claiming_input(self):
        self.custody_refusal()
        original = self.index.get('archived')
        before = self.attempt.queued_turn_snapshot
        self.assertTrue(self.runtime.preview(self.attempt.id, project=self.project, actor=self.admin)['can_create'])
        result = await self.recover()
        self.assertEqual(result['threadId'], 'fresh')
        self.assertFalse(result['native_turn_started'])
        self.assertEqual(self.index.get('archived'), original)
        current = self.service.store.get(self.attempt.id)
        self.assertEqual(current.queued_turn_snapshot, before)
        self.assertEqual(current.status, 'blocked')
        self.assertIsNone(current.queued_start_receipt)
        self.assertEqual(self.index.replacement_thread_id('archived'), 'fresh')
        # A lost HTTP response reconciles the existing creation, including for
        # a source that keeps its unarchived history.
        repeated = await self.recover()
        self.assertEqual(repeated['threadId'], 'fresh')
        self.assertEqual(self.threads.create.await_count, 1)

    async def test_custody_recovery_refuses_live_lease_changed_binding_and_unknown_outcome(self):
        self.custody_refusal()
        assignment = self.assignments['old-assignment']
        for field, value in (('status','running'), ('lease',object()), ('created_by','another-actor'),
                             ('resource_ids',('wrong-repo',)), ('execution_id','changed'), ('fence',3)):
            old = getattr(assignment, field)
            setattr(assignment, field, value)
            try:
                with self.assertRaises(HTTPException):
                    await self.recover()
            finally:
                setattr(assignment, field, old)
        self.service.store.update(self.attempt.id, lambda current:current.model_copy(update={
            'status':'reconciliation_required'}))
        with self.assertRaises(HTTPException):
            await self.recover()
        self.threads.create.assert_not_called()

    async def test_fenced_pending_successor_extends_history_without_rewriting_the_old_edge(self):
        self.prior_pending_route()
        preview = self.runtime.preview(self.attempt.id, project=self.project, actor=self.admin)
        self.assertEqual(preview['source_thread_id'], 'prior')
        self.assertTrue(preview['can_create'])
        result = await self.recover_from_prior()
        self.assertEqual(result['threadId'], 'fresh')
        self.assertEqual(self.index.replacement_thread_id('archived'), 'fresh')
        graph = self.service.store.store.get(self.index.REPLACEMENTS_NAMESPACE)
        self.assertEqual(graph['edges']['archived']['new_thread_id'], 'prior')
        self.assertEqual(graph['edges']['prior']['new_thread_id'], 'fresh')
        retained = self.service.store.get(self.attempt.id)
        self.assertEqual(retained.queued_turn_snapshot, self.attempt.queued_turn_snapshot)
        self.assertIsNone(retained.queued_start_receipt)
        self.assertEqual(self.threads.create.await_count, 1)

    async def test_successor_requires_reviewed_source_and_a_fenced_same_project_worker(self):
        self.prior_pending_route()
        for source in (None, 'another-destination'):
            with self.subTest(source=source), self.assertRaises(HTTPException):
                await self.recover_from_prior(source)
        assignment = self.assignments['prior-assignment']
        for changes in ({'status':'running'}, {'lease':object()}, {'project_id':'foreign'}):
            self.assignments['prior-assignment'] = SimpleNamespace(**{**vars(assignment), **changes})
            with self.subTest(changes=changes), self.assertRaises(HTTPException):
                await self.recover_from_prior()
        self.threads.create.assert_not_awaited()
        self.assertIsNone(self.service.store.get(self.attempt.id).bootstrap_recovery)

    def created_initial_refusal(self):
        reserved, _ = self.service.reserve_bootstrap_recovery(self.attempt.id, actor=self.admin,
            expected_snapshot_sha256=self.digest, reason='Initial fixture bootstrap')
        previous = reserved.bootstrap_recovery.model_copy(update={'state':'created', 'destination_thread_id':'prior'})
        self.prior_pending_route(recovery=previous)
        blocker = ExecutionPreflightBlocker(code='initial_bootstrap_process_lost', message='Controller refused before input',
            phase='before_turn_input', threadId='prior', preflight_attempt_id=self.attempt.id,
            admission_claim_id='claim', snapshot_sha256=self.digest,
            bootstrap_id=previous.bootstrap_id, assignment_id='prior-assignment')
        self.service.store.update(self.attempt.id, lambda a:a.model_copy(update={
            'bootstrap_recovery':previous, 'replacement_thread_id':'prior',
            'queued_admission_claim_id':'claim', 'blockers':(blocker,)}))
        return previous

    async def test_created_recovery_can_renew_only_after_its_bound_controller_preinput_refusal(self):
        previous = self.created_initial_refusal()
        result = await self.recover_from_prior()
        retained = self.service.store.get(self.attempt.id)
        self.assertEqual(result['threadId'], 'fresh')
        self.assertEqual(retained.bootstrap_recovery_history, (previous,))
        self.assertNotEqual(retained.bootstrap_recovery.id, previous.id)
        self.assertEqual(retained.queued_turn_snapshot, self.attempt.queued_turn_snapshot)

    async def test_unknown_successor_creation_offers_only_reconciliation_and_never_creates_again(self):
        self.created_initial_refusal()
        self.threads.create.side_effect = TimeoutError('Provider creation outcome unavailable')
        with self.assertRaises(TimeoutError):
            await self.recover_from_prior()
        preview = self.runtime.preview(self.attempt.id, project=self.project, actor=self.admin)
        self.assertEqual(preview['state'], 'pending')
        self.assertFalse(preview['can_create'])
        self.assertTrue(preview['can_reconcile'])
        with self.assertRaises(HTTPException):
            await self.recover_from_prior()
        self.assertEqual(self.threads.create.await_count, 1)

    async def test_route_change_during_creation_cannot_publish_a_stale_recovery_edge(self):
        self.prior_pending_route()
        create = self.threads.create.side_effect
        async def raced(**values):
            result = await create(**values)
            self.service.store.update(self.attempt.id, lambda a:a.model_copy(update={'replacement_thread_id':'changed'}))
            return result
        self.threads.create.side_effect = raced
        with self.assertRaises(HTTPException):
            await self.recover_from_prior()
        self.assertEqual(self.index.replacement_thread_id('archived'), 'prior')
        self.assertEqual(self.service.store.get(self.attempt.id).bootstrap_recovery.state, 'pending')
        self.assertEqual(self.threads.create.await_count, 1)

    async def test_parent_becoming_live_during_creation_keeps_new_bootstrap_pending(self):
        self.prior_pending_route()
        create = self.threads.create.side_effect
        async def raced(**values):
            result = await create(**values)
            self.assignments['prior-assignment'].status = 'running'
            return result
        self.threads.create.side_effect = raced
        with self.assertRaises(HTTPException):
            await self.recover_from_prior()
        self.assertEqual(self.index.replacement_thread_id('archived'), 'prior')
        retained = self.service.store.get(self.attempt.id)
        self.assertEqual(retained.bootstrap_recovery.state, 'pending')
        self.assertEqual(retained.queued_turn_snapshot, self.attempt.queued_turn_snapshot)

    async def test_successor_shares_source_queue_lock_and_both_owned_fences(self):
        from codex_web.services.coordination import StateStoreCoordinationBackend
        from codex_web.services.replicated_ownership import ReplicatedOwnershipService
        self.prior_pending_route()
        self.runtime.ownership = ReplicatedOwnershipService(
            StateStoreCoordinationBackend(self.service.store.store), instance_id='fixture-recovery')
        locks = {name:asyncio.Lock() for name in ('archived', 'prior')}
        self.runtime.queue_lock = locks.__getitem__
        entered = asyncio.Event()
        create = self.threads.create.side_effect
        async def check_fences(**values):
            self.runtime.ownership.require_fence('thread-queue:archived')
            self.runtime.ownership.require_fence('thread-queue:prior')
            entered.set()
            return await create(**values)
        self.threads.create.side_effect = check_fences
        await locks['prior'].acquire()
        task = asyncio.create_task(self.recover_from_prior())
        try:
            await asyncio.sleep(0.05)
            self.assertFalse(entered.is_set())
            self.threads.create.assert_not_awaited()
        finally:
            locks['prior'].release()
        result = await asyncio.wait_for(task, 3)
        self.assertEqual(result['threadId'], 'fresh')
        self.assertTrue(entered.is_set())

    async def test_fresh_binding_routes_original_without_queue_or_turn_start(self):
        result = await self.recover()
        self.assertFalse(result['native_turn_started'])
        self.assertEqual(result['threadId'], 'fresh')
        current = self.service.store.get(self.attempt.id)
        self.assertEqual(current.queued_turn_snapshot, self.attempt.queued_turn_snapshot)
        self.assertEqual(current.attempt_number, 1)
        self.assertEqual(current.status, 'blocked')
        self.assertIsNone(current.queued_start_receipt)
        self.assertEqual(current.replacement_thread_id, 'fresh')
        self.assertEqual(self.index.replacement_thread_id('archived', project_id='project',
            organization_id='org', workspace_id='ws'), 'fresh')
        self.assertEqual(self.threads.create.await_count, 1)
        await self.recover()
        self.assertEqual(self.threads.create.await_count, 1)

    async def test_lost_response_reconciles_receipt_after_restart_without_second_creation(self):
        async def lost(**values):
            await self.create(**values)
            raise TimeoutError('Response observation expired')
        self.threads.create.side_effect = lost
        with self.assertRaises(TimeoutError):
            await self.recover()
        self.assertEqual(self.service.store.get(self.attempt.id).bootstrap_recovery.state, 'pending')
        self.assertIsNone(self.index.replacement_thread_id('archived'))
        self.service = self.reopen()
        self.runtime = self.make_runtime()
        result = await self.recover()
        self.assertEqual(result['threadId'], 'fresh')
        self.assertEqual(self.threads.create.await_count, 1)

    async def test_absent_receipt_never_authorizes_repeated_creation(self):
        self.threads.create.side_effect = TimeoutError('Unknown bootstrap outcome')
        with self.assertRaises(TimeoutError):
            await self.recover()
        self.now += 100000
        with self.assertRaises(HTTPException) as caught:
            await self.recover()
        self.assertEqual(caught.exception.detail['code'], 'bootstrap_recovery_pending')
        self.assertEqual(self.threads.create.await_count, 1)
        self.assertIsNone(self.index.replacement_thread_id('archived'))

    async def test_definite_prestart_refusal_allows_explicit_new_attempt_and_retains_proof(self):
        from codex_web.services.threads import ThreadBootstrapPrestartRefusal
        self.threads.create.side_effect = ThreadBootstrapPrestartRefusal(status_code=503,
            detail={'code':'execution_preflight_blocked', 'message':'Controller maintenance'})
        with self.assertRaises(ThreadBootstrapPrestartRefusal):
            await self.recover()
        refused = self.service.store.get(self.attempt.id)
        self.assertEqual(refused.bootstrap_recovery.state, 'refused')
        self.assertEqual(refused.bootstrap_recovery.prestart_refusal.phase, 'before_provider_thread_start')
        self.assertFalse(self.service.public(refused, actor=self.admin)['can_retry'])
        self.threads.create.side_effect = self.create
        result = await self.recover()
        self.assertEqual(result['threadId'], 'fresh')
        current = self.service.store.get(self.attempt.id)
        self.assertEqual(current.bootstrap_recovery_history, (refused.bootstrap_recovery,))
        self.assertNotEqual(current.bootstrap_recovery.id, refused.bootstrap_recovery.id)
        self.assertEqual(current.attempt_number, 1)

    async def test_wider_grant_cannot_become_a_recovery_route(self):
        async def wider(**values):
            result = await self.create(**values)
            self.assignments['assignment'].repository_scope.writable_repository_ids = ('repo', 'unrequested')
            return result
        self.threads.create.side_effect = wider
        with self.assertRaises(HTTPException) as caught:
            await self.recover()
        self.assertEqual(caught.exception.detail['code'], 'bootstrap_recovery_binding_mismatch')
        self.assertIsNone(self.index.replacement_thread_id('archived'))
        self.assertEqual(self.service.store.get(self.attempt.id).bootstrap_recovery.state, 'pending')

    async def test_concurrent_caller_cannot_duplicate_inflight_creation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(**values):
            entered.set()
            await release.wait()
            return await self.create(**values)
        self.threads.create.side_effect = delayed
        first = asyncio.create_task(self.recover())
        await entered.wait()
        try:
            with self.assertRaises(HTTPException) as caught:
                await self.recover()
            self.assertEqual(caught.exception.detail['code'], 'bootstrap_recovery_pending')
            self.assertEqual(self.threads.create.await_count, 1)
        finally:
            release.set()
            await first

    async def test_late_original_receipt_prevents_atomic_route_publication(self):
        receipt = QueuedNativeStartReceipt(admission_claim_id='original-claim',
            requested_execution_id='original-turn', execution_id='original-turn',
            thread_id='archived', provider_native_session_id='archived',
            provider_native_turn_id='original-native-turn', provider_id='openai',
            runtime_id='codex', observed_at=self.now)
        async def late_receipt(**values):
            result = await self.create(**values)
            self.service.store.update(self.attempt.id, lambda current:current.model_copy(update={
                'status':'started', 'queued_start_receipt':receipt, 'started_at':self.now}))
            return result
        self.threads.create.side_effect = late_receipt
        with self.assertRaises(HTTPException) as caught:
            await self.recover()
        self.assertEqual(caught.exception.detail['code'], 'bootstrap_recovery_changed')
        self.assertIsNone(self.index.replacement_thread_id('archived'))
        current = self.service.store.get(self.attempt.id)
        self.assertEqual(current.queued_start_receipt, receipt)
        self.assertEqual(current.status, 'started')
        self.assertIsNone(current.replacement_thread_id)

    async def test_latest_profile_substitution_is_rejected(self):
        async def substituted(**values):
            result = await self.create(**values)
            self.assignments['assignment'].agent_profile.profile_revision = 7
            return result
        self.threads.create.side_effect = substituted
        with self.assertRaises(HTTPException) as caught:
            await self.recover()
        self.assertEqual(caught.exception.detail['code'], 'bootstrap_recovery_binding_mismatch')
        self.assertIsNone(self.index.replacement_thread_id('archived'))

    async def test_http_scope_and_payload_guards_precede_creation(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from codex_web.api.turns import build_turns_router
        from codex_web.services.projects import ProjectService
        from codex_web.services.thread_scope import ThreadScopeService
        from codex_web.storage.agent_sessions import AgentSessionStore
        from codex_web.identity import AuthenticationAssurance
        projects = ProjectService(SimpleNamespace(load=lambda:[self.project]))
        scope = ThreadScopeService(projects, SimpleNamespace(store=AgentSessionStore(self.index.store)), self.index)
        app = FastAPI()
        self.request_actor = self.admin
        @app.middleware('http')
        async def identity(request, call_next):
            request.state.identity_actor = self.request_actor
            return await call_next(request)
        app.include_router(build_turns_router(SimpleNamespace(), scope, bootstrap_recovery=self.runtime))
        path = '/api/threads/archived/preflight-attempts/' + self.attempt.id + '/bootstrap-recovery'
        with TestClient(app) as client:
            preview = client.get(path)
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(self.threads.create.await_count, 0)
            payload = {'expected_snapshot_sha256':preview.json()['snapshot_sha256'], 'reason':'Exact original'}
            self.assertEqual(client.post(path, json={**payload, 'agent_profile_revision':7}).status_code, 422)
            self.assertEqual(client.post(path, json={**payload, 'expected_snapshot_sha256':'f' * 64}).status_code, 409)
            self.request_actor = self.admin.model_copy(update={'assurance':AuthenticationAssurance.PRIMARY})
            self.assertEqual(client.post(path, json=payload).status_code, 403)
            self.request_actor = self.admin.model_copy(update={'workspace_id':'foreign'})
            self.assertEqual(client.get(path).status_code, 404)
            self.assertEqual(self.threads.create.await_count, 0)
            self.request_actor = self.admin
            self.assertEqual(client.post(path, json=payload).status_code, 200)
            self.assertEqual(self.threads.create.await_count, 1)
