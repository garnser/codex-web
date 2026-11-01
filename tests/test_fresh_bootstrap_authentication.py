"""Real fresh Thread/Binding preflight, with only account/native transports synthetic."""
import asyncio
import inspect
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException

from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.models import QueuedTurn
from codex_web.runtime.codex import CodexRuntime
from codex_web.services.queued_bootstrap_recovery import QueuedBootstrapRecoveryService
from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingService
from codex_web.services.threads import ThreadBootstrapPrestartRefusal
from codex_web.services.agent_profiles import AgentProfileAccessDenied
from codex_web.storage.thread_bootstrap_bindings import ThreadBootstrapBindingStore
from tests import test_turn_execution_binding as binding_fixture
from tests import test_thread_bootstrap_routing as thread_fixture


class FreshBootstrapAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = binding_fixture.TurnExecutionBindingTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.addCleanup(self.f.doCleanups)
        self.host = thread_fixture._Host()
        self.host.project = self.f.project
        self.session = thread_fixture._Session()
        self.manager = thread_fixture._SessionManager(self.session)
        self.bindings = ThreadBootstrapBindingService(ThreadBootstrapBindingStore(self.f.sqlite))
        self.proof = SimpleNamespace(_authenticated_account_proof=None, _authentication_epoch=0,
            _authentication_context=lambda: ('private-context',),
            AUTHENTICATED_ACCOUNT_MAX_AGE_SECONDS=CodexRuntime.AUTHENTICATED_ACCOUNT_MAX_AGE_SECONDS)
        self.available = lambda: CodexRuntime.authenticated_account_available(self.proof)
        self.f.service.local_session_probe = self.available
        self.lock = asyncio.Lock()
        self.account_reads = []
        self.account_result = {'account': {'type': 'chatgpt'}}
        self.account_error = None
        self.trace = []
        self.after_refresh = None
        self.binding = ExecutionRuntimeBinding(provider_id='openai', runtime_id='codex',
            capability_revision=1, authentication_mode='trusted_local_session')

        async def prepare():
            # The application supplies its existing coalesced account owner.
            # Here its external account/read response is synthetic, never sign-in.
            async with self.lock:
                if not self.available():
                    self.trace.append('account/read')
                    self.account_reads.append({'refreshToken': False})
                    if self.account_error:
                        raise self.account_error
                    account = self.account_result.get('account')
                    self.proof._authenticated_account_proof = (
                        (time.monotonic(), ('private-context',), 0)
                        if isinstance(account, dict) and account.get('type') in {'apiKey', 'chatgpt'} else None)
                    if self.after_refresh:
                        self.after_refresh()
        self.prepare = prepare
        options = dict(binding_service=self.f.service, session_manager=self.manager,
            bootstrap_bindings=self.bindings, control_actor=self.f.actor)
        # Baseline imports/constructor remain valid: the old existing entrypoint
        # must reach stale-auth refusal, not fail on a missing new keyword.
        if 'authentication_preparation' in inspect.signature(thread_fixture.ThreadService).parameters:
            options['authentication_preparation'] = prepare
        self.threads = thread_fixture._thread_service(self.host, **options)
        self.threads._select_runtime_route = AsyncMock(side_effect=lambda **_: (self.binding, None))
        self.original_request = QueuedTurn(id='exact-original', thread_id='old-thread',
            project_id=self.f.project.id, message='Retained exact original', created_at=1,
            source='web', agent_profile_actor_id=self.f.actor.identity_id,
            execution_id='retained-original-execution', execution_profile_id='repository-write',
            repository_resource_id=self.f.repository.id,
            writable_repository_resource_ids=(self.f.repository.id,),
            sandbox='workspace-write', approval_policy='on-request')
        self.recovery = QueuedBootstrapRecoveryService(
            preflight=SimpleNamespace(record_bootstrap_refusal=lambda *a, **k: None), threads=self.threads,
            bootstrap_bindings=self.bindings, assignments=self.f.workers.store.assignment,
            thread_index=None, actor_resolver=None, thread_is_active=lambda _: False)
        self.reservation = SimpleNamespace(id='original-attempt', bootstrap_recovery=SimpleNamespace(
            id='original-recovery', bootstrap_id='bootstrap-'+'a'*32))

    async def create(self):
        return await self.recovery.create_reserved(self.reservation, self.original_request,
            self.f.actor, self.f.project)

    def expire(self):
        self.proof._authenticated_account_proof = (time.monotonic()-121, ('private-context',), 0)
        self.assertFalse(self.available())

    async def test_exact_recovery_refreshes_expired_proof_before_real_bootstrap_preflight(self):
        self.expire()
        original = self.original_request.model_dump(mode='json')
        result = await self.create()
        self.assertEqual(result['thread']['id'], 'thread-created')
        self.assertEqual(self.account_reads, [{'refreshToken': False}])
        self.assertEqual(self.original_request.model_dump(mode='json'), original)
        self.assertEqual([r[0] for r in self.session.requests], ['thread/start'])
        self.assertEqual(len(self.manager.started), 1)
        binding = self.bindings.get_by_thread('thread-created', self.f.actor)
        assignment = self.f.workers.store.assignment(binding.assignment_id)
        self.assertEqual(assignment.subject.ref, self.reservation.bootstrap_recovery.bootstrap_id)
        self.assertEqual(assignment.execution_id, 'thread-bootstrap-'+'a'*32)
        self.assertEqual(assignment.repository_scope.writable_repository_ids, (self.f.repository.id,))
        self.assertEqual(assignment.created_by, self.f.actor.identity_id)
        self.assertIsNone(assignment.procedure_checkpoint)
        self.assertTrue(binding.initial_turn_pending)

    async def test_fresh_proof_and_prior_native_preparation_do_not_read_account_again(self):
        await self.prepare()  # Same owner may already have run on the native bridge.
        self.account_reads.clear()
        await self.create()
        self.assertEqual(self.account_reads, [])
        self.assertEqual([r[0] for r in self.session.requests], ['thread/start'])

    async def test_negative_account_response_preserves_preprovider_auth_refusal(self):
        self.account_result = {'account': None}
        with self.assertRaises(ThreadBootstrapPrestartRefusal) as caught:
            await self.create()
        self.assertEqual(caught.exception.detail['code'], 'execution_preflight_blocked')
        self.assertEqual(caught.exception.detail['blockers'][0]['code'], 'local_session_unavailable')
        self.assertEqual(self.account_reads, [{'refreshToken': False}])
        self.assertEqual(self.manager.started, [])
        self.assertEqual(self.session.requests, [])

    async def test_account_rpc_error_is_prestart_and_does_not_create_or_retry(self):
        self.account_error = RuntimeError('PRIVATE account transport refused')
        # RPC failure must be a controller-owned prestart refusal. This direct
        # creation call uses an already reserved original, so record_refusal is
        # the existing recovery boundary, not a new authorization decision.
        self.recovery.preflight = SimpleNamespace(record_bootstrap_refusal=lambda *a, **k: None)
        self.reservation.id = 'original-attempt'
        self.reservation.bootstrap_recovery.id = 'original-recovery'
        with self.assertRaises(ThreadBootstrapPrestartRefusal) as caught:
            await self.create()
        self.assertEqual(caught.exception.detail['code'], 'bootstrap_prestart_failed')
        self.assertNotIn('PRIVATE', str(caught.exception.detail))
        self.assertEqual(len(self.account_reads), 1)
        self.assertEqual(self.manager.started, [])
        self.assertEqual(self.session.requests, [])

    async def test_routing_scope_refusal_happens_before_account_preparation(self):
        self.threads._select_runtime_route.side_effect = HTTPException(status_code=403, detail='scope refused')
        self.recovery.preflight = SimpleNamespace(record_bootstrap_refusal=lambda *a, **k: None)
        self.reservation.id = 'original-attempt'
        self.reservation.bootstrap_recovery.id = 'original-recovery'
        with self.assertRaises(ThreadBootstrapPrestartRefusal) as caught:
            await self.create()
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(self.account_reads, [])
        self.assertEqual(self.manager.started, [])

    async def test_authority_changed_during_refresh_still_refuses_before_worker(self):
        self.after_refresh = lambda: self.f.resources.update(self.f.repository.id,
            binding_fixture.ResourceUpdate(lifecycle='disabled'), actor=self.f.actor)
        self.recovery.preflight = SimpleNamespace(record_bootstrap_refusal=lambda *a, **k: None)
        self.reservation.id = 'original-attempt'
        self.reservation.bootstrap_recovery.id = 'original-recovery'
        with self.assertRaises(ThreadBootstrapPrestartRefusal) as caught:
            await self.create()
        self.assertEqual(caught.exception.detail['code'], 'execution_preflight_blocked')
        self.assertEqual(caught.exception.detail['blockers'][0]['code'], 'repository_target_unauthorized')
        self.assertEqual(len(self.account_reads), 1)
        self.assertEqual(self.manager.started, [])
        self.assertEqual(self.session.requests, [])

    async def test_explicit_credentials_other_provider_and_denied_mode_do_not_refresh_local_account(self):
        for provider, runtime, mode, allowed in [
            ('openai', 'codex', 'api_key', None),
            ('openai', 'codex', 'delegated_worker_token', None),
            ('anthropic', 'claude-code', None, None),
            ('openai', 'codex', 'trusted_local_session', ()),
        ]:
            with self.subTest(provider=provider, runtime=runtime, mode=mode):
                self.binding = ExecutionRuntimeBinding(provider_id=provider, runtime_id=runtime,
                    capability_revision=1, authentication_mode=mode)
                self.f.service.permitted_codex_authentication_modes = allowed
                self.recovery.preflight = SimpleNamespace(record_bootstrap_refusal=lambda *a, **k: None)
                self.reservation.id = 'original-attempt'
                self.reservation.bootstrap_recovery.id = 'original-recovery'
                with self.assertRaises(ThreadBootstrapPrestartRefusal):
                    await self.create()
                self.assertEqual(self.account_reads, [])
                self.assertEqual(self.manager.started, [])
                self.assertEqual(self.session.requests, [])

    async def test_profile_access_refusal_precedes_account_preparation(self):
        self.original_request = self.original_request.model_copy(update={
            'agent_profile_id': 'restricted-profile', 'agent_profile_revision': 6})
        denied = Mock(side_effect=AgentProfileAccessDenied('Private profile access refused'))
        self.threads.agent_profiles = SimpleNamespace(resolve_for_execution=denied)
        with self.assertRaises(ThreadBootstrapPrestartRefusal) as caught:
            await self.create()
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(caught.exception.detail['code'], 'agent_profile_access_denied')
        denied.assert_called_once()
        self.assertEqual(self.account_reads, [])
        self.assertEqual(self.manager.started, [])
        self.assertEqual(self.session.requests, [])

    async def test_authentication_policy_revoked_during_preparation_is_rechecked(self):
        self.after_refresh = lambda: setattr(self.f.service, 'permitted_codex_authentication_modes', ())
        with self.assertRaises(ThreadBootstrapPrestartRefusal) as caught:
            await self.create()
        self.assertEqual(caught.exception.detail['code'], 'execution_preflight_blocked')
        self.assertEqual(caught.exception.detail['blockers'][0]['code'], 'authentication_mode_denied')
        self.assertEqual(len(self.account_reads), 1)
        self.assertEqual(self.manager.started, [])
        self.assertEqual(self.session.requests, [])
