"""Real fresh Thread/Binding preflight, with only account/native transports synthetic."""
import asyncio
import inspect
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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
        self.account_clock = time.monotonic
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
                        (self.account_clock(), ('private-context',), 0)
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

    async def test_account_evidence_expiring_during_readiness_is_refreshed_before_reservation(self):
        # Advance only the account-evidence clock at a real existing metadata
        # boundary. No elapsed sleeps, changed TTL, or native response evidence.
        clock = [1000.0]
        self.account_clock = lambda: clock[0]
        readiness = self.f.workers.execution_readiness
        delayed = []

        def inspect_readiness(**kwargs):
            result = readiness(**kwargs)
            if not delayed:
                self.assertTrue(self.available())
                clock[0] += CodexRuntime.AUTHENTICATED_ACCOUNT_MAX_AGE_SECONDS + 1
                self.assertFalse(self.available())
                delayed.append(True)
            return result

        self.f.workers.execution_readiness = inspect_readiness
        original = self.original_request.model_dump(mode='json')
        with patch('codex_web.runtime.codex.time', SimpleNamespace(monotonic=self.account_clock)):
            result = await self.create()
            self.assertTrue(self.available())
        self.assertEqual(result['thread']['id'], 'thread-created')
        self.assertEqual(delayed, [True])
        self.assertEqual(self.account_reads, [{'refreshToken': False}] * 2)
        self.assertEqual(self.original_request.model_dump(mode='json'), original)
        self.assertEqual([r[0] for r in self.session.requests], ['thread/start'])
        self.assertEqual(len(self.manager.started), 1)

    def configure_restricted_reasoning_bootstrap(self):
        from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
        from codex_web.services.codex_reasoning_policy import ReasoningOnlyCodexAdapter
        # Compose the restricted reasoning contract, not a renamed repository
        # runtime: published scratch/no-repository/no-network execution profile,
        # read-only runtime, and the fixed trusted-local authentication policy.
        registry = self.f.execution_profiles.registry
        payload = self.f.execution_profiles.record(organization_id=self.f.actor.organization_id,
            workspace_id=self.f.actor.workspace_id).payload.copy()
        payload['profiles'] = [*payload['profiles'], dict(id='reasoning-only', name='Reasoning only',
            description='Canonical restricted native authentication regression',
            workspace_mode='scratch', repository_access='none', network_enabled=False,
            required_worker_capabilities=['command_execution'], control_plane_operations=[])]
        draft = registry.create_draft(DefinitionDraftCreate(definition_id='execution.profiles.default',
            kind='execution-profile-catalog', definition_schema_version='1.3', scope_type='global',
            payload=payload, actor=self.f.actor.identity_id))
        registry.publish(draft.record_id, DefinitionPublishRequest(actor=self.f.actor.identity_id))
        self.binding = ExecutionRuntimeBinding(provider_id='openai', runtime_id='codex-reasoning',
            capability_revision=1, sandbox_profiles=('read-only',),
            authentication_mode='trusted_local_session')
        self.threads.session_managers[('openai', 'codex-reasoning')] = self.manager
        def adapter(binding, session):
            self.assertEqual(binding, self.binding)
            return ReasoningOnlyCodexAdapter(session)
        self.threads.runtime_adapter_factory = adapter
        self.original_request = self.original_request.model_copy(update={
            'sandbox':'read-only', 'approval_policy':'never', 'execution_profile_id':'reasoning-only',
            'repository_resource_id':None, 'writable_repository_resource_ids':(),
            'read_only_repository_resource_ids':()})

    async def test_restricted_reasoning_bootstrap_refreshes_after_canonical_readiness(self):
        from codex_web.execution_workers import WorkerCapability
        self.configure_restricted_reasoning_bootstrap()
        clock = [1000.0]
        self.account_clock = lambda: clock[0]
        readiness = self.f.workers.execution_readiness
        delayed = []

        def inspect_readiness(**kwargs):
            result = readiness(**kwargs)
            if not delayed:
                self.assertTrue(self.available())
                self.assertEqual(tuple(kwargs['required_capabilities']), (WorkerCapability.COMMAND_EXECUTION,))
                clock[0] += CodexRuntime.AUTHENTICATED_ACCOUNT_MAX_AGE_SECONDS + 1
                self.assertFalse(self.available())
                delayed.append(True)
            return result

        self.f.workers.execution_readiness = inspect_readiness
        original = self.original_request.model_dump(mode='json')
        with patch('codex_web.runtime.codex.time', SimpleNamespace(monotonic=self.account_clock)):
            # Real native reasoning runs this same metadata owner before its
            # ThreadService call; that earlier positive alone is insufficient.
            await self.prepare()
            result = await self.create()
            self.assertTrue(self.available())
        self.assertEqual(result['thread']['id'], 'thread-created')
        assignment = self.f.workers.store.assignments_for_execution('thread-bootstrap-'+'a'*32,
            organization_id=self.f.actor.organization_id, workspace_id=self.f.actor.workspace_id)[0]
        self.assertEqual(assignment.runtime_binding, self.binding)
        self.assertEqual(assignment.execution_profile_id, 'reasoning-only')
        self.assertEqual(assignment.resource_ids, ())
        self.assertEqual(assignment.repository_scope.source, 'orchestration_only')
        self.assertEqual(assignment.sandbox, 'read-only')
        self.assertFalse(assignment.network.enabled)
        self.assertEqual(self.account_reads, [{'refreshToken': False}] * 2)
        self.assertEqual(self.original_request.model_dump(mode='json'), original)
        self.assertEqual([r[0] for r in self.session.requests], ['thread/start'])
        self.assertEqual(len(self.manager.started), 1)

    async def test_restricted_reasoning_final_policy_or_context_change_refuses_reservation(self):
        self.configure_restricted_reasoning_bootstrap()
        clock = [1000.0]
        self.account_clock = lambda: clock[0]
        readiness = self.f.workers.execution_readiness
        original = self.original_request.model_dump(mode='json')
        for change in ('policy', 'context'):
            with self.subTest(change=change):
                self.proof._authenticated_account_proof = None
                self.proof._authentication_context = lambda: ('private-context',)
                self.account_reads.clear()
                self.f.service.permitted_codex_authentication_modes = None
                delayed = []

                def inspect_readiness(**kwargs):
                    result = readiness(**kwargs)
                    if not delayed:
                        self.assertTrue(self.available())
                        clock[0] += CodexRuntime.AUTHENTICATED_ACCOUNT_MAX_AGE_SECONDS + 1
                        delayed.append(True)
                    return result

                def invalidate():
                    if len(self.account_reads) == 2:
                        if change == 'policy':
                            self.f.service.permitted_codex_authentication_modes = ()
                        else:
                            self.proof._authentication_context = lambda: ('changed-context',)

                self.after_refresh = invalidate
                self.f.workers.execution_readiness = inspect_readiness
                with patch('codex_web.runtime.codex.time', SimpleNamespace(monotonic=self.account_clock)):
                    await self.prepare()  # Earlier native bridge authentication owner.
                    with self.assertRaises(ThreadBootstrapPrestartRefusal) as caught:
                        await self.create()
                blocker = caught.exception.detail['blockers'][0]
                self.assertEqual(blocker['code'], 'authentication_mode_denied'
                    if change == 'policy' else 'local_session_unavailable')
                self.assertEqual(blocker['target_id'], 'openai/codex-reasoning')
                self.assertEqual(self.account_reads, [{'refreshToken': False}] * 2)
                self.assertEqual(self.manager.started, [])
                self.assertEqual(self.session.requests, [])
                self.assertEqual(self.original_request.model_dump(mode='json'), original)
                self.assertEqual(self.f.workers.store.assignments_for_execution(
                    'thread-bootstrap-'+'a'*32, organization_id=self.f.actor.organization_id,
                    workspace_id=self.f.actor.workspace_id), [])

    async def test_negative_account_response_preserves_preprovider_auth_refusal(self):
        self.account_result = {'account': None}
        with self.assertRaises(ThreadBootstrapPrestartRefusal) as caught:
            await self.create()
        self.assertEqual(caught.exception.detail['code'], 'execution_preflight_blocked')
        self.assertEqual(caught.exception.detail['blockers'][0]['code'], 'local_session_unavailable')
        self.assertEqual(self.account_reads, [{'refreshToken': False}])
        self.assertEqual(self.manager.started, [])
        self.assertEqual(self.session.requests, [])

    async def test_expired_during_readiness_never_reuses_negative_or_invalidated_refresh(self):
        clock = [1000.0]
        self.account_clock = lambda: clock[0]
        readiness = self.f.workers.execution_readiness
        original = self.original_request.model_dump(mode='json')
        for change in ('negative', 'epoch', 'context', 'policy'):
            with self.subTest(change=change):
                self.proof._authenticated_account_proof = None
                self.proof._authentication_epoch = 0
                self.proof._authentication_context = lambda: ('private-context',)
                self.account_result = {'account': {'type': 'chatgpt'}}
                self.account_reads.clear()
                self.f.service.permitted_codex_authentication_modes = None
                delayed = []

                def inspect_readiness(**kwargs):
                    result = readiness(**kwargs)
                    if not delayed:
                        self.assertTrue(self.available())
                        clock[0] += CodexRuntime.AUTHENTICATED_ACCOUNT_MAX_AGE_SECONDS + 1
                        delayed.append(True)
                        if change == 'negative':
                            self.account_result = {'account': None}
                    return result

                def invalidate():
                    if len(self.account_reads) != 2:
                        return
                    if change == 'epoch':
                        self.proof._authentication_epoch += 1
                    elif change == 'context':
                        self.proof._authentication_context = lambda: ('changed-context',)
                    elif change == 'policy':
                        self.f.service.permitted_codex_authentication_modes = ()

                self.after_refresh = invalidate
                self.f.workers.execution_readiness = inspect_readiness
                with patch('codex_web.runtime.codex.time', SimpleNamespace(monotonic=self.account_clock)):
                    with self.assertRaises(ThreadBootstrapPrestartRefusal) as caught:
                        await self.create()
                expected = 'authentication_mode_denied' if change == 'policy' else 'local_session_unavailable'
                self.assertEqual(caught.exception.detail['blockers'][0]['code'], expected)
                self.assertEqual(self.account_reads, [{'refreshToken': False}] * 2)
                self.assertEqual(self.manager.started, [])
                self.assertEqual(self.session.requests, [])
                self.assertEqual(self.original_request.model_dump(mode='json'), original)
                self.assertEqual(self.f.workers.store.assignments_for_execution(
                    'thread-bootstrap-'+'a'*32, organization_id=self.f.actor.organization_id,
                    workspace_id=self.f.actor.workspace_id), [])

    async def test_cancellation_during_final_refresh_never_reserves_or_starts(self):
        clock = [1000.0]
        self.account_clock = lambda: clock[0]
        readiness = self.f.workers.execution_readiness
        entered, release = asyncio.Event(), asyncio.Event()
        delayed = []

        def inspect_readiness(**kwargs):
            result = readiness(**kwargs)
            if not delayed:
                clock[0] += CodexRuntime.AUTHENTICATED_ACCOUNT_MAX_AGE_SECONDS + 1
                delayed.append(True)
            return result

        async def prepare():
            if len(self.account_reads) == 1 and not self.available():
                entered.set()
                await release.wait()
            await self.prepare()

        self.f.workers.execution_readiness = inspect_readiness
        self.threads.authentication_preparation = prepare
        original = self.original_request.model_dump(mode='json')
        with patch('codex_web.runtime.codex.time', SimpleNamespace(monotonic=self.account_clock)):
            task = asyncio.create_task(self.create())
            try:
                await entered.wait()
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.manager.started, [])
        self.assertEqual(self.session.requests, [])
        self.assertEqual(self.original_request.model_dump(mode='json'), original)

        self.assertEqual(self.f.workers.store.assignments_for_execution(
            'thread-bootstrap-'+'a'*32, organization_id=self.f.actor.organization_id,
            workspace_id=self.f.actor.workspace_id), [])

    async def test_current_admission_revoked_during_final_refresh_refuses_before_reservation(self):
        from codex_web.services.resources import RepositoryTargetUnauthorizedError
        clock = [1000.0]
        self.account_clock = lambda: clock[0]
        readiness = self.f.workers.execution_readiness
        acquired, guards, delayed = [], [], []
        acquire = self.f.workspaces.acquire

        def inspect_readiness(**kwargs):
            result = readiness(**kwargs)
            if not delayed:
                clock[0] += CodexRuntime.AUTHENTICATED_ACCOUNT_MAX_AGE_SECONDS + 1
                delayed.append(True)
            return result

        def current_admission():
            guards.append(len(self.account_reads))
            self.f.resources.resolve_repository_target(self.f.project,
                actor=self.f.actor, explicit_repository_id=self.f.repository.id)

        def revoke():
            if len(self.account_reads) == 2:
                self.f.resources.update(self.f.repository.id,
                    binding_fixture.ResourceUpdate(lifecycle='disabled'), actor=self.f.actor)

        def reserve(*args, **kwargs):
            acquired.append(True)
            return acquire(*args, **kwargs)

        self.after_refresh = revoke
        self.f.workers.execution_readiness = inspect_readiness
        self.f.workspaces.acquire = reserve
        with patch('codex_web.runtime.codex.time', SimpleNamespace(monotonic=self.account_clock)):
            with self.assertRaises(ThreadBootstrapPrestartRefusal) as caught:
                await self.threads.create(project_id=self.f.project.id,
                    sandbox='workspace-write', approval_policy='on-request',
                    repository_resource_id=self.f.repository.id,
                    writable_repository_resource_ids=(self.f.repository.id,),
                    execution_profile_id='repository-write', actor=self.f.actor,
                    bootstrap_token='a'*32, admission_guard=current_admission)
        self.assertIsInstance(caught.exception.__cause__, RepositoryTargetUnauthorizedError)
        self.assertEqual(guards[-1], 2)
        self.assertEqual(self.account_reads, [{'refreshToken': False}] * 2)
        self.assertEqual(acquired, [])
        self.assertEqual(self.manager.started, [])
        self.assertEqual(self.session.requests, [])
        self.assertEqual(self.f.workers.store.assignments_for_execution(
            'thread-bootstrap-'+'a'*32, organization_id=self.f.actor.organization_id,
            workspace_id=self.f.actor.workspace_id), [])

    async def test_changed_authentication_hook_while_reserve_queued_refuses_before_call(self):
        loop = asyncio.get_running_loop()
        ready, occupied = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        gate = []
        preparations, substituted = [], []
        prepare = self.threads._prepare_bootstrap_authentication
        authentication = self.f.service._authentication_preflight

        def occupy():
            loop.call_soon_threadsafe(occupied.set)
            release.wait()

        async def prepare_then_queue(*args):
            result = await prepare(*args)
            preparations.append(True)
            if len(preparations) == 2:
                gate.append(loop.run_in_executor(None, occupy))
                await occupied.wait()
                ready.set()
            return result

        def changed_authentication(*args, **kwargs):
            substituted.append(True)
            return authentication(*args, **kwargs)

        # A real one-worker executor makes the intervening queue wait explicit.
        # All preparation and reservation callbacks still execute normally.
        with ThreadPoolExecutor(max_workers=1) as executor:
            loop.set_default_executor(executor)
            self.threads._prepare_bootstrap_authentication = prepare_then_queue
            task = asyncio.create_task(self.create())
            try:
                await ready.wait()
                self.f.service._authentication_preflight = changed_authentication
                release.set()
                with self.assertRaises(ThreadBootstrapPrestartRefusal) as caught:
                    await task
                self.assertEqual(caught.exception.detail['code'], 'execution_preflight_blocked')
            finally:
                release.set()
                await asyncio.gather(task, *gate, return_exceptions=True)
        self.assertEqual(substituted, [])
        self.assertEqual(self.manager.started, [])
        self.assertEqual(self.session.requests, [])
        self.assertEqual(self.f.workers.store.assignments_for_execution(
            'thread-bootstrap-'+'a'*32, organization_id=self.f.actor.organization_id,
            workspace_id=self.f.actor.workspace_id), [])

    async def test_cancellation_joins_in_progress_reservation_without_native_start(self):
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release, finished = threading.Event(), threading.Event()
        acquire = self.f.workspaces.acquire
        calls = []

        def reserve(*args, **kwargs):
            calls.append(True)
            loop.call_soon_threadsafe(entered.set)
            release.wait()
            try:
                return acquire(*args, **kwargs)
            finally:
                finished.set()

        self.f.workspaces.acquire = reserve
        task = asyncio.create_task(self.create())
        try:
            await entered.wait()
            task.cancel()
            # A scheduling checkpoint, not an elapsed-time assertion: process
            # the cancellation while the actual reservation owner stays gated.
            checkpoint = loop.create_future()
            loop.call_soon(checkpoint.set_result, None)
            await checkpoint
            self.assertFalse(task.done())
            self.assertFalse(finished.is_set())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(finished.is_set())
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(calls, [True])
        assignments = self.f.workers.store.assignments_for_execution(
            'thread-bootstrap-'+'a'*32, organization_id=self.f.actor.organization_id,
            workspace_id=self.f.actor.workspace_id)
        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0].execution_id, 'thread-bootstrap-'+'a'*32)
        self.assertEqual(self.manager.started, [])
        self.assertEqual(self.session.requests, [])
        # The joined canonical reservation is retained, never retried or
        # represented as no effects merely because its observer was cancelled.

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
