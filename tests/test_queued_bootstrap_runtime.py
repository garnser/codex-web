from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException

from codex_web.models import IndexedThread, Project, QueuedNativeStartReceipt
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
        self.assignments['assignment'] = SimpleNamespace(execution_id=recovery.execution_id,
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
