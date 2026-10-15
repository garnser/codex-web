"""Actual admitted body -> Codex adapter -> assignment session -> guarded send.

No native process/provider is launched: the wire is StringIO and replies are
synthetic. Canonical project/Goal/binding/worker/preflight state remains real.
"""
import asyncio
import io
import json
import threading
import time
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from codex_web.runtime.codex import CodexRuntime
from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSession
from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime
from codex_web.services.queued_native_start import QueuedNativeStartCapture
from codex_web.services.thread_transcript import ThreadTranscriptService
from tests import test_project_coordination_delegation as delegation
from tests.test_codex_runtime import _Host as CodexHost
from tests.test_codex_worker_session import _FakeBackend, _FakeDelegationService


class ProjectSendBoundaryTests(unittest.IsolatedAsyncioTestCase):
    _request = delegation.ProjectDelegatedDispatchTests._request
    publish = delegation.ProjectDelegatedDispatchTests.publish
    call = delegation.ProjectDelegatedDispatchTests.call
    create = delegation.ProjectDelegatedDispatchTests.create
    async def message(self, *args, **kwargs):
        queued, receipt = await delegation.ProjectDelegatedDispatchTests.message(self, *args, **kwargs)
        # Queue retry bookkeeping mutates attempts; freeze the actual original
        # before admission rather than comparing against that mutable object.
        return queued.model_copy(deep=True), receipt

    async def asyncSetUp(self):
        await delegation.ProjectDelegatedDispatchTests.asyncSetUp(self)
        del self.execution._start_thread_turn_now_admitted
        self.execution.binding_service = self.f.service
        self.execution.bootstrap_bindings = self.bindings
        self.execution.control_actor = self.actor
        self.execution.routing_service = self.thread_service.routing_service
        self.execution.transcript = ThreadTranscriptService(self.f.sqlite)
        self.sessions_by_assignment = {}
        self.completed = []
        async def start(assignment_id):
            return self.sessions_by_assignment[assignment_id]
        async def complete(assignment_id, **kwargs):
            self.completed.append((assignment_id, kwargs))
        self.execution.session_manager = SimpleNamespace(get=self.sessions_by_assignment.get,
            start=start, complete=complete)
        self.wires = {}
        self.natives = {}
        self.writes_entered = asyncio.Event()
        self.hold_writer = False
        self.positive_then_error = False

    async def child(self, profile='engineer', key='create'):
        thread, _ = await self.create(profile, key)
        bootstrap = self.bindings.get_by_thread(thread, self.actor)
        assignment = self.f.workers.store.assignment(bootstrap.assignment_id)
        credentials = _FakeDelegationService()
        local = LocalExecutionWorkerRuntime(self.f.workers, self.f.workspaces, _FakeBackend(),
            worker=self.f.worker, worker_actor=self.worker_actor, control_actor=self.actor,
            codex_auth_delegation=credentials)
        # Initialize the real session and its real validate_current/_request;
        # only native process launch and credential material are synthetic.
        session = AssignmentBoundAgentProcessSession(local, self.host, assignment.id,
            runtime_factory=CodexRuntime, credential_provider=credentials,
            runtime_binding=assignment.runtime_binding)
        self.addCleanup(session._supervision_executor.shutdown, wait=True, cancel_futures=True)
        session.fence = assignment.fence
        session.delegation = credentials._delegation(assignment, self.f.worker.id, assignment.fence)
        session.workspace_path = Path(self.f.workspaces.get(assignment.execution_workspace_id, self.actor).path)
        session.started_monotonic = time.monotonic()
        native = CodexRuntime(CodexHost(), restart_on_timeout=False)
        native.ensure_started = AsyncMock()  # Synthetic, already-started native transport.
        native.ready.set()
        wire = io.StringIO()
        native.proc = SimpleNamespace(poll=lambda: None, stdin=wire, pid=99999999)
        session.runtime = native
        self.sessions_by_assignment[assignment.id] = session
        self.wires[thread], self.natives[thread] = wire, native
        send = native._send
        async def wire_reply(message, **options):
            if message['method'] == 'turn/start' and self.hold_writer:
                await native.write_lock.acquire()
                self.writes_entered.set()
            await send(message, **options)  # Real lock, guard, write and flush.
            result = ({'turn': {'id': 'turn-'+thread, 'threadId': thread}}
                      if message['method'] == 'turn/start' else {'thread': {'id': thread}})
            await native._dispatch_message({'id': message['id'], 'result': result})
            if message['method'] == 'turn/start' and self.positive_then_error:
                raise RuntimeError('synthetic caller loss after positive reader evidence')
        native._send = wire_reply
        return thread

    def starts(self, thread):
        return [json.loads(line) for line in self.wires[thread].getvalue().splitlines()
                if json.loads(line)['method'] == 'turn/start']

    def retained(self, original):
        return self.execution.queued_preflight.store.get('preflight-queued-'+original.id)

    def assert_refused(self, child, original, order):
        self.assertEqual(self.starts(child), [])
        self.assertEqual(self.completed, [])
        self.assertNotIn(child, self.host.active)
        current = self.host._thread_queue(child)
        self.assertEqual([v.id for v in current], order)
        saved = self.retained(original)
        self.assertEqual(saved.queued_turn_snapshot, original.model_dump(mode='json'))
        self.assertEqual(saved.status, 'queued')
        self.assertIsNone(saved.queued_start_receipt)
        self.assertEqual(current[0].project_coordination_dispatch, original.project_coordination_dispatch)
        self.assertEqual(self.execution.transcript.read(child)['turns'], [])
        self.assertTrue(any(v.blockers[0].code == 'native_preinput_admission_refused'
            for v in saved.blocker_history))
        assignment = self.f.workers.store.assignment(self.bindings.get_by_thread(child, self.actor).assignment_id)
        self.assertEqual(assignment.status, 'running')

    async def test_two_children_send_with_real_body_capture_and_paused_business(self):
        for profile in ('engineer', 'scanner'):
            child = await self.child(profile, profile)
            original, _ = await self.message(child, key=profile)
            await self.execution.drain_thread_queue(child)
            self.assertEqual(len(self.starts(child)), 1)
            self.assertEqual(self.retained(original).status, 'started')
            self.assertEqual(self.retained(original).queued_start_receipt.provider_native_turn_id, 'turn-'+child)
            self.assertEqual(self.host.active[child].source, 'queued:project-coordination')
        self.execution.enqueue_turn(thread_id='business', project_id='home', source='business', message='held')
        await self.execution.drain_thread_queue('business')
        self.assertEqual(self.host._thread_queue_depth('business'), 1)
        self.assertEqual(self.completed, [])

    async def test_revoke_during_capture_preparation_preserves_child_then_reauthorization_sends_once(self):
        child = await self.child()
        original, _ = await self.message(child)
        self.execution.enqueue_turn(thread_id=child, project_id='home', message='held later business', source='business')
        order = [v.id for v in self.host._thread_queue(child)]
        entered, release = threading.Event(), threading.Event()
        prepare = QueuedNativeStartCapture.prepare
        def held(capture, **kwargs):
            result = prepare(capture, **kwargs)
            entered.set()
            if not release.wait(5):
                raise AssertionError('controlled capture phase was not released')
            return result
        with patch.object(QueuedNativeStartCapture, 'prepare', held):
            task = asyncio.create_task(self.execution.drain_thread_queue(child))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                self.goal_bindings.operator_stop(self.goal_binding.id, scope=self.actor.tenant,
                    actor_id=self.actor.identity_id, cancelled=False, reason='Revoke at preparation')
            finally:
                release.set()
                await task
        self.assert_refused(child, original, order)
        self.goal_bindings.resume_operator_pause(self.goal_binding.id, scope=self.actor.tenant,
            actor_id=self.actor.identity_id, reason='Legitimate explicit reauthorization')
        await self.execution.drain_thread_queue(child)
        self.assertEqual(len(self.starts(child)), 1)
        self.assertEqual(self.retained(original).status, 'started')
        self.assertEqual(self.completed, [])
        self.assertEqual([v.source for v in self.host._thread_queue(child)], ['business'])
        history = self.execution.transcript.read(child)['turns']
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['status'], 'inProgress')

    async def writer_revoke(self, change):
        child = await self.child()
        original, _ = await self.message(child)
        order = [original.id]
        self.hold_writer = True
        task = asyncio.create_task(self.execution.drain_thread_queue(child))
        try:
            await asyncio.wait_for(self.writes_entered.wait(), 3)
            if change == 'binding':
                self.goal_bindings.operator_stop(self.goal_binding.id, scope=self.actor.tenant,
                    actor_id=self.actor.identity_id, cancelled=False, reason='Revoke at writer lock')
            else:
                self.f.workers.store.update(lambda state: state.model_copy(update={'assignments': [
                    v.model_copy(update=({'fence': v.fence+1} if change == 'fence' else {'lease': None}))
                    if v.id == self.assignment.id else v for v in state.assignments]}))
        finally:
            if self.natives[child].write_lock.locked():
                self.natives[child].write_lock.release()
            await task
        self.assert_refused(child, original, order)

    async def test_binding_revocation_while_waiting_for_writer(self):
        await self.writer_revoke('binding')

    async def test_origin_fence_revocation_while_waiting_for_writer(self):
        await self.writer_revoke('fence')

    async def test_origin_lease_loss_while_waiting_for_writer(self):
        await self.writer_revoke('lease')

    async def test_positive_receipt_survives_post_send_caller_error(self):
        child = await self.child()
        original, _ = await self.message(child)
        self.positive_then_error = True
        await self.execution.drain_thread_queue(child)
        self.assertEqual(len(self.starts(child)), 1)
        self.assertEqual(self.retained(original).status, 'started')
        self.assertIsNotNone(self.retained(original).queued_start_receipt)
        self.assertEqual(len(self.execution.transcript.read(child)['turns']), 1)
        self.assertEqual(self.completed, [])
        self.assertEqual(self.host._thread_queue(child), [])

    async def test_queue_status_exposes_delegation_reason_with_pause_on_and_off(self):
        child = await self.child()
        await self.message(child)
        self.assertEqual(self.turn_service.queue(child)['automationWaiting'], 0)
        self.goal_bindings.operator_stop(self.goal_binding.id, scope=self.actor.tenant,
            actor_id=self.actor.identity_id, cancelled=False, reason='User paused this binding')
        for enabled in (False, True):
            self.execution.automated_admission_allowed = lambda: enabled
            status = self.turn_service.queue(child)
            self.assertEqual(status['automationWaiting'], 1)
            self.assertEqual(status['automationWaitReason']['code'], 'project_delegation_blocked')
            self.assertIn('Project delegation blocked:', status['automationWaitReason']['message'])
            self.assertIn('binding is blocked', status['automationWaitReason']['message'])
            self.assertNotIn('enabled and resumed', status['automationWaitReason']['message'])
            # Render this actual canonical response through the existing UI
            # module; no browser, application server or new UI policy.
            script = '''
import {readFileSync} from 'node:fs';
const source=readFileSync('static/queue_admission_status.js','utf8');
const ui=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const status=JSON.parse(readFileSync(0,'utf8'));
const state={threadId:status.threadId,queueAdmissionMessageByThread:new Map()};
const notice={hidden:true,textContent:''};
ui.applyQueueAdmissionStatus(state,status);
ui.renderQueueAdmissionStatus(state,{getElementById:()=>notice});
if(notice.hidden || !notice.textContent.includes('Project delegation blocked:')) throw Error('Missing visible blocker');
'''
            subprocess.run(['node', '--max-old-space-size=64', '--input-type=module', '-e', script],
                input=json.dumps(status), text=True, check=True, timeout=10, capture_output=True)
        self.execution.enqueue_turn(thread_id=child, project_id='home', source='business', message='held')
        self.execution.automated_admission_allowed = lambda: False
        mixed = self.turn_service.queue(child)
        self.assertEqual(mixed['automationWaiting'], 2)
        self.assertIn('Project delegation blocked:', mixed['automationWaitReason']['message'])
        self.assertIn('Automated work is waiting', mixed['automationWaitReason']['message'])
        self.goal_bindings.resume_operator_pause(self.goal_binding.id, scope=self.actor.tenant,
            actor_id=self.actor.identity_id, reason='Explicit reauthorization')
        ordinary = self.turn_service.queue(child)
        self.assertEqual(ordinary['automationWaiting'], 1)
        self.assertEqual(ordinary['automationWaitReason']['code'], 'autonomy_paused')

    async def test_delegated_transport_without_write_guard_fails_closed(self):
        child = await self.child()
        original, _ = await self.message(child)
        assignment_id = self.bindings.get_by_thread(child, self.actor).assignment_id
        self.sessions_by_assignment[assignment_id].request_with_start_capture = None
        await self.execution.drain_thread_queue(child)
        self.assertEqual(self.starts(child), [])
        self.assertEqual(self.completed, [])
        self.assertEqual(self.host._thread_queue(child)[0].id, original.id)
