"""Native preparation reuses account metadata ownership, never a model RPC."""
import ast
import asyncio
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from codex_web.events import EventHub
from codex_web.model_providers import ModelProviderAdapterError
from codex_web.runtime.codex import CodexRuntime
from codex_web.services.native_reasoning_bridge import CanonicalNativeReasoningBridge, NativeReasoningBinding
from codex_web.services.post_turn_readiness import PostTurnReadinessService


def application_refresh(runtime):
    # Read the real application functions without importing its startup owner.
    source = ast.parse((Path(__file__).parents[1] / 'codex_web/application.py').read_text())
    names = {'_local_codex_session_available', '_refresh_local_codex_session'}
    functions = [node for node in source.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    scope = dict(asyncio=asyncio, app=NS(state=NS(codex_runtime=runtime)),
                 _local_codex_session_refresh_lock=asyncio.Lock())
    exec(compile(ast.Module(body=functions, type_ignores=[]), '<application-auth-owners>', 'exec'), scope)
    return scope['_refresh_local_codex_session']


class AccountMetadata:
    authenticated_account_available = CodexRuntime.authenticated_account_available
    AUTHENTICATED_ACCOUNT_MAX_AGE_SECONDS = CodexRuntime.AUTHENTICATED_ACCOUNT_MAX_AGE_SECONDS

    def __init__(self):
        self._authenticated_account_proof = None
        self._authentication_epoch = 0
        self.calls = []
        self.valid = True

    def _authentication_context(self):
        return 'private-test-context'

    async def request(self, method, params):
        self.calls.append((method, params))
        if self.valid:
            self._authenticated_account_proof = (time.monotonic(), self._authentication_context(), 0)
        return {}  # No token material, model, thread or turn request.


class NativeAuthenticationPreparationTests(unittest.IsolatedAsyncioTestCase):
    def build(self, refresh, guard=lambda: None):
        calls, events = [], []
        async def create(**kwargs):
            calls.append(kwargs)
            raise RuntimeError('test stops at unchanged canonical thread boundary')
        bridge = CanonicalNativeReasoningBridge(
            binding=NativeReasoningBinding(provider_id='native', model_id='model', concrete_model='model',
                agent_profile_id='thinker', agent_profile_revision=1,
                execution_profile_id='reasoning', worker_id='worker'),
            threads=NS(create=create, event_sink=events.append), turns=None, sessions=None,
            usage=None, preflight=None, execution=None, hub=EventHub(),
            attest_session=lambda *a: None, authentication_preparation=refresh)
        context = NS(project_id='project', validate_current=guard, process_owner=None,
            organization_id='org', workspace_id='workspace', execution_id='executive:one:cpo',
            activation_id='one', role_id='cpo')
        args = (NS(id='native'), NS(id='model', concrete_model='model', supports_tools=False), None)
        return bridge, context, args, calls, events

    async def invoke(self, built):
        bridge, context, args, _, _ = built
        return await bridge.invoke(*args, actor=None, context=context)

    async def test_cold_and_stale_metadata_refresh_once_before_thread_preflight(self):
        for stale in (False, True):
            runtime = AccountMetadata()
            if stale:
                runtime._authenticated_account_proof = (time.monotonic()-120, runtime._authentication_context(), 0)
            self.assertFalse(runtime.authenticated_account_available())
            refresh = application_refresh(runtime)
            async def prepared():
                await refresh()
                self.assertTrue(runtime.authenticated_account_available())
            built = self.build(prepared)
            with self.assertRaises(ModelProviderAdapterError):
                await self.invoke(built)
            self.assertEqual(runtime.calls, [('account/read', {'refreshToken': False})])
            self.assertEqual(len(built[3]), 1)
            preparation, = [event for event in built[4]
                            if event['type'] == 'native_reasoning_preparation_failed']
            self.assertEqual(preparation['native_preparation_stage'], 'thread_bootstrap')

    async def test_refresh_reaches_real_binding_and_revoked_auth_still_allocates_nothing(self):
        from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
        from codex_web.execution_workers import ExecutionRuntimeBinding
        from codex_web.services.turn_execution_binding import TurnExecutionBindingError
        from tests import test_turn_execution_binding as canonical
        for valid in (False, True):
            f = canonical.TurnExecutionBindingTests()
            f.setUp()
            self.addCleanup(f.tearDown)
            f.service._clock = time.time
            payload = f.execution_profiles.record(organization_id=f.actor.organization_id,
                workspace_id=f.actor.workspace_id).payload.copy()
            payload['profiles'] = [*payload['profiles'], dict(id='reasoning-only', name='Reasoning',
                description='Private canonical authentication fixture', workspace_mode='scratch',
                repository_access='none', network_enabled=False,
                required_worker_capabilities=['command_execution'], control_plane_operations=[])]
            registry = f.execution_profiles.registry
            record = registry.create_draft(DefinitionDraftCreate(definition_id='execution.profiles.default',
                kind='execution-profile-catalog', definition_schema_version='1.3', scope_type='global',
                payload=payload, actor=f.actor.identity_id))
            registry.publish(record.record_id, DefinitionPublishRequest(actor=f.actor.identity_id))
            runtime = AccountMetadata()
            runtime.valid = valid
            f.service.local_session_probe = runtime.authenticated_account_available
            arguments = dict(bootstrap_id='one-bootstrap', execution_id='one-execution',
                project_id=f.project.id, sandbox='read-only', approval_policy='never',
                execution_profile_id='reasoning-only', runtime_binding=ExecutionRuntimeBinding(
                    provider_id='openai', runtime_id='codex-reasoning', capability_revision=1,
                    sandbox_profiles=('read-only',)))
            # The previous native path reaches this preflight without refreshing.
            with self.assertRaises(TurnExecutionBindingError) as caught:
                f.service.prepare_bootstrap(**arguments)
            self.assertEqual(caught.exception.code, 'local_session_unavailable')
            self.assertEqual(f.backend.provisioned, [])
            built = self.build(application_refresh(runtime))
            async def create(**kwargs):
                f.service.prepare_bootstrap(**arguments)
                raise RuntimeError('stop before provider process')
            built[0].threads.create = create
            with self.assertRaises(ModelProviderAdapterError):
                await self.invoke(built)
            assignments = f.workers.list_assignments(f.actor)
            self.assertEqual(len(assignments), int(valid))
            self.assertEqual(len(f.backend.provisioned), int(valid))
            if valid:
                self.assertEqual(assignments[0].status, 'pending')
                self.assertIsNone(assignments[0].lease)
                self.assertEqual(assignments[0].resource_ids, ())
                self.assertEqual(assignments[0].runtime_binding.authentication_mode, 'trusted_local_session')

    async def test_fresh_metadata_and_concurrent_refresh_reuse_real_coalesced_owner(self):
        runtime = AccountMetadata()
        refresh = application_refresh(runtime)
        await asyncio.gather(refresh(), refresh())
        await refresh()
        self.assertTrue(runtime.authenticated_account_available())
        self.assertEqual(runtime.calls, [('account/read', {'refreshToken': False})])

    async def test_revoked_metadata_does_not_bypass_existing_authentication_preflight(self):
        from codex_web.execution_workers import ExecutionRuntimeBinding
        from codex_web.runtime_credentials import runtime_authentication_preflight
        runtime = AccountMetadata()
        runtime.valid = False
        await application_refresh(runtime)()
        result = runtime_authentication_preflight(ExecutionRuntimeBinding(provider_id='openai',
            runtime_id='codex-reasoning', capability_revision=1),
            local_session_probe=runtime.authenticated_account_available)
        self.assertFalse(result.available)
        self.assertEqual(result.code, 'local_session_unavailable')

    async def test_refresh_failure_and_initial_revocation_never_create_thread(self):
        async def fail():
            raise RuntimeError('account unavailable')
        built = self.build(fail)
        with self.assertRaises(ModelProviderAdapterError):
            await self.invoke(built)
        self.assertEqual(built[3], [])
        preparation, = [event for event in built[4]
                        if event['type'] == 'native_reasoning_preparation_failed']
        self.assertEqual(preparation['native_preparation_stage'], 'authentication_metadata')
        refresh_calls = []
        async def refresh():
            refresh_calls.append(True)
        def revoked():
            raise ValueError('current authority revoked')
        built = self.build(refresh, revoked)
        with self.assertRaises(ValueError):
            await self.invoke(built)
        self.assertEqual(refresh_calls, [])
        self.assertEqual(built[3], [])

    async def test_current_guard_after_metadata_refuses_before_thread_and_has_distinct_stage(self):
        current = [True]
        async def refresh():
            current[0] = False
        def guard():
            if not current[0]:
                raise ValueError('authority changed')
        built = self.build(refresh, guard)
        with self.assertRaises(ModelProviderAdapterError):
            await self.invoke(built)
        self.assertEqual(built[3], [])
        preparation, = [event for event in built[4]
                        if event['type'] == 'native_reasoning_preparation_failed']
        self.assertEqual(preparation['native_preparation_stage'], 'post_authentication_current_guard')

    async def test_timeout_and_cancel_join_same_metadata_owner_without_late_thread(self):
        original = PostTurnReadinessService._run_owned
        for cancelled in (False, True):
            entered, release, joined = asyncio.Event(), asyncio.Event(), asyncio.Event()
            async def refresh():
                entered.set()
                await release.wait()
                joined.set()
            built = self.build(refresh)
            async def bounded(operation, *, seconds, name):
                return await original(operation, seconds=.01 if name == 'native-authentication-metadata' else seconds, name=name)
            with patch.object(PostTurnReadinessService, '_run_owned', bounded):
                task = asyncio.create_task(self.invoke(built))
                await entered.wait()
                if cancelled:
                    task.cancel()
                await asyncio.sleep(.03)
                self.assertFalse(task.done())
                self.assertEqual(built[3], [])
                release.set()
                with self.assertRaises(asyncio.CancelledError if cancelled else ModelProviderAdapterError):
                    await task
                self.assertTrue(joined.is_set())
                self.assertEqual(built[3], [])
