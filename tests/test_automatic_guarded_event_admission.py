"""Real selected-procedure admission/queue/capture; synthetic native I/O only."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from codex_web.models import BotBinding
from codex_web.runtime.execution import ExecutionPrestartRefusal
from codex_web.services.bot_event_dispatch import BotEventDispatchService
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.turn_queue_policy import TurnQueuePolicy
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from tests import test_native_provider_preparation as native_fixtures
from tests import test_turn_execution as turn_fixtures


class AutomaticGuardedEventAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.native = native_fixtures.NativeProviderPreparationTests()
        await self.native.asyncSetUp()
        self.addAsyncCleanup(self.native.asyncTearDown)
        f = self.f = self.native.fixture
        # The source fixture defaults to provider-a/runtime-a. Register its
        # private adapter as Codex and prepare the canonical assignment's exact
        # binding accordingly, so real capture accepts the tested transport.
        from tests.test_agent_runtime import _Runtime
        adapter = _Runtime()
        adapter.provider_id, adapter.runtime_id = 'openai', 'codex'
        f.runtimes.register(adapter, capability_revision=7)
        f.providers.upsert(f.provider_config.model_copy(update={'id':'openai'}), actor=f.actor)
        binding = f.native_assignment.runtime_binding.model_copy(update={
            'provider_id':'openai', 'runtime_id':'codex'})
        f.workers.store.update_assignment(f.procedure_worker.id, f.native_assignment.id,
            lambda state:state.model_copy(update={'assignments':[
                a.model_copy(update={'runtime_binding':binding, 'skill_refs':(self.native.reference,)})
                if a.id==f.native_assignment.id else a
                for a in state.assignments]}))
        f.native_assignment = f.workers.store.assignment(f.native_assignment.id)
        host, provisioning, sessions, runtime = turn_fixtures.TurnExecutionStartTests._service(self)
        self.host, self.sessions, self.runtime = host, sessions, runtime
        self.bootstrap = SimpleNamespace(bootstrap_id='private-canonical-bootstrap',
            thread_id='native-provider-thread', assignment_id=f.native_assignment.id,
            execution_id=f.native_assignment.execution_id, execution_workspace_id=f.workspace.id,
            initial_turn_pending=False)
        runtime.bootstrap_bindings.get_by_thread = lambda *_args:self.bootstrap
        sessions.get = lambda assignment_id:sessions.session if assignment_id==f.native_assignment.id else None
        host._project = lambda _id: f.project
        host._release_stale_active_turn = lambda *_args: False
        # Established native-admission fixture seam: provisioning and native I/O
        # are synthetic; selected registry/Profile/assignment/workspace are real.
        provisioning.prepare = provisioning.prepare_bootstrap = lambda **kw: SimpleNamespace(
            assignment_id=f.native_assignment.id, workspace_id=f.workspace.id,
            execution_id=f.native_assignment.execution_id)
        sessions.session.validate_current = lambda: f.workers.store.assignment(f.native_assignment.id)
        sessions.session.status = lambda: SimpleNamespace(worker_id=f.procedure_worker.id,
            fence=f.native_assignment.fence)
        sessions.session.workspace_path = Path(f.workspace.path)
        runtime.control_actor = f.actor
        runtime.actor_resolver = lambda *_args: f.actor
        runtime.procedure_admission = self.native.admission
        # Same canonical scoped context resolver used by application wiring.
        runtime.skill_context_resolver = lambda refs, project, objective: f.skills.context_for_refs_scoped(
            refs, organization_id=project.organization_id, workspace_id=project.workspace_id,
            project_id=project.id, objective=objective)
        runtime.queued_preflight = ExecutionPreflightService(ExecutionPreflightStore(f.sqlite))
        runtime.schedule_queue_drain = Mock()
        self.queue = TurnQueuePolicy(lambda: host.queues, get_queue=host._thread_queue)
        self.binding = BotBinding(id='canonical-owner', provider='slack',
            external_conversation_id='canonical-owner-conversation', thread_id='native-provider-thread',
            project_id=f.project.id, sandbox='workspace-write', approval_policy='on-request',
            created_at=1, updated_at=1)
        self.dispatch = BotEventDispatchService(
            projects=SimpleNamespace(get=lambda _id:f.project),
            settings=SimpleNamespace(get=lambda _id:SimpleNamespace(model=None, reasoning_effort=None,
                execution_profile_id='repository-write')),
            targets=SimpleNamespace(event_reply_target=lambda _binding:None,
                conversation_target=lambda _binding:None), bindings=SimpleNamespace(upsert=Mock()),
            execution=runtime, queue_policy=self.queue,
            recovery=SimpleNamespace(release_stale_active_turn=Mock()),
            resume=SimpleNamespace(is_timeout_error=lambda e:isinstance(e,TimeoutError),
                is_stale_thread_error=lambda _e:False),
            telemetry=SimpleNamespace(append=Mock()), publish_event=AsyncMock(),
            binding_name=lambda _binding:'james',
            agent_profile_resolver=lambda _binding:(None,f.actor))

    async def accept(self, **kwargs):
        return await self.dispatch.dispatch(self.binding, 'Read current owned repository facts',
            'owner-work-watchdog', writable_repository_resource_ids=(self.f.repo.id,), **kwargs)

    async def test_idle_canonical_no_alias_owner_gets_real_selected_guard_and_captures(self):
        try:
            result = await self.accept()
        except ExecutionPrestartRefusal as exc:
            self.assertEqual(exc.detail.get('code'), 'operator_input_transport_unsupported')
            self.fail('idle automatic dispatch bypassed durable admission before real selected-procedure guard')
        self.assertTrue(result.get('queued'), 'guarded automatic input must retain original before native work')
        original, = self.queue.queue(self.binding.thread_id)
        snapshot = original.model_dump(mode='json')
        self.assertIsNone(original.owner_routing_pin)
        self.assertEqual(original.agent_profile_actor_id, self.f.actor.identity_id)
        self.assertEqual(original.writable_repository_resource_ids, (self.f.repo.id,))
        self.assertEqual(self.sessions.session.requests, [])
        self.runtime.schedule_queue_drain.assert_called_once_with(self.binding.thread_id)
        await self.runtime.drain_thread_queue(self.binding.thread_id)
        saved = self.runtime.queued_preflight.store.get('preflight-queued-'+original.id)
        self.assertEqual(saved.status, 'started')
        self.assertEqual(saved.queued_turn_snapshot, snapshot)
        self.assertEqual(saved.queued_start_receipt.requested_execution_id, saved.execution_id)
        self.assertEqual(saved.queued_start_correlation.assignment_id, self.f.native_assignment.id)
        self.assertEqual(saved.queued_start_correlation.fence, self.f.native_assignment.fence)
        self.assertEqual(saved.queued_resume_evidence.outcome, 'resumed')
        self.assertEqual([method for method,_ in self.sessions.session.requests], ['thread/resume','turn/start'])
        current = self.f.workers.store.assignment(self.f.native_assignment.id)
        self.assertTrue(current.procedure_discoveries[-1].selected)
        self.assertEqual(self.f.ops.calls, [])

    async def test_initial_native_input_uses_same_durable_capture_without_resume(self):
        self.bootstrap.initial_turn_pending = True
        await self.sessions.session.create_initial_native_session(self.binding.thread_id)
        result = await self.accept()
        self.assertTrue(result.get('queued'))
        await self.runtime.drain_thread_queue(self.binding.thread_id)
        saved = self.runtime.queued_preflight.store.get('preflight-queued-'+result['queuedId'])
        self.assertEqual(saved.status, 'started')
        self.assertIsNotNone(saved.queued_start_receipt)
        self.assertIsNone(saved.queued_resume_correlation)
        self.assertEqual([m for m,_ in self.sessions.session.requests], ['turn/start'])
        self.assertEqual(self.runtime.bootstrap_bindings.initial_turn_started[0][0], self.binding.thread_id)

    async def test_current_procedure_quarantine_refuses_before_native_resume_or_start(self):
        result = await self.accept()
        self.assertTrue(result.get('queued'))
        self.f.definition_registry.quarantine(self.native.reference.record_id,
            actor=self.f.actor.identity_id, reason='Private current revision revoked before drain')
        await self.runtime.drain_thread_queue(self.binding.thread_id)
        saved = self.runtime.queued_preflight.store.get('preflight-queued-'+result['queuedId'])
        self.assertEqual(saved.status, 'blocked')
        self.assertIsNone(saved.queued_start_receipt)
        self.assertIsNone(saved.queued_resume_correlation)
        self.assertEqual(self.sessions.session.requests, [])

    async def test_lost_native_start_response_stays_unknown_without_automatic_replay(self):
        result = await self.accept()
        self.assertTrue(result.get('queued'))
        request = self.sessions.session.request
        async def lose(method, params=None):
            if method == 'turn/start':
                self.sessions.session.requests.append((method, params))
                raise TimeoutError('Synthetic lost native response after possible send')
            return await request(method, params)
        self.sessions.session.request = lose
        await self.runtime.drain_thread_queue(self.binding.thread_id)
        saved = self.runtime.queued_preflight.store.get('preflight-queued-'+result['queuedId'])
        self.assertEqual(saved.status, 'reconciliation_required')
        self.assertIsNotNone(saved.queued_start_correlation)
        self.assertIsNone(saved.queued_start_receipt)
        _, retry_id, acquired = self.runtime.queued_preflight.claim_retry(saved.id, actor=self.f.actor)
        self.assertFalse(acquired)
        self.assertIsNone(retry_id)
        await self.runtime.drain_thread_queue(self.binding.thread_id)
        self.assertEqual(sum(m=='turn/start' for m,_ in self.sessions.session.requests),1)
        self.assertEqual(self.runtime.queued_preflight.store.get(saved.id), saved)

    async def test_require_idle_does_not_duplicate_existing_queued_work(self):
        first = await self.accept()
        self.assertTrue(first.get('queued'))
        second = await self.accept(require_idle=True)
        self.assertFalse(second['queued'])
        self.assertEqual(len(self.queue.queue(self.binding.thread_id)),1)
        self.assertEqual(self.sessions.session.requests, [])
