"""Actual Thread/worker/workspace ownership; only isolated process transport is controlled."""
import asyncio
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.thread_bootstrap_composition_harness import build_real_thread_create
from codex_web.execution_workers import AssignmentClaimRequest, AssignmentStartRequest
from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSessionManager
from codex_web.services.post_turn_readiness import PostTurnReadinessService
from codex_web.services.threads import ThreadBootstrapPrestartRefusal
from codex_web.thread_bootstrap import RecipientBootstrapOrigin


class PostworkerPreproviderBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = build_real_thread_create(self)
        f = self.f
        local = SimpleNamespace(worker_service=f.workers, workspace_service=f.workspaces,
            control_actor=f.actor, worker_actor=f.worker_actor,
            _pending_assignment=lambda aid: f.workers.store.assignment(aid))
        self.manager = AssignmentBoundAgentProcessSessionManager(local, f.transport_host,
            runtime_factory=lambda *a, **k: None, credential_provider=None)
        self.started = []; self.stopped = []; self.cleanup_error = False
        self.pending = 0; self.guard_settled = []; self.observed = None
        async def start(aid):
            claimed = f.workers.claim(f.worker.id, AssignmentClaimRequest(),
                actor=f.worker_actor, assignment_id=aid)
            original = f.workers.start(f.worker.id, aid,
                AssignmentStartRequest(fence=claimed.fence, lease_token=claimed.lease.lease_token),
                actor=f.worker_actor)
            self.started.append(original)
            session = f.process_session
            session.worker_id = f.worker.id; session.fence = original.fence
            session.workspace_path = Path(f.workspaces.get(original.execution_workspace_id, f.actor).path)
            session.status = lambda: SimpleNamespace(worker_id=f.worker.id, fence=original.fence,
                running=aid not in self.stopped, pending_executor_work=self.pending)
            async def stop():
                if self.cleanup_error: raise RuntimeError('controlled teardown failure')
                self.stopped.append(aid)
            session.stop = stop
            self.manager.sessions[aid] = session
            return session
        self.manager.start = start
        f.thread_service.session_manager = self.manager
        f.thread_service.session_managers[('openai', 'codex')] = self.manager
        self.origin = RecipientBootstrapOrigin('a'*32, 0, 'b'*64,
            work_item_ref='private/repository!1', handoff_requested_at=100.,
            binding_id='private-existing-binding', actor_id=f.actor.identity_id)

    async def refuse(self, *, timed=False, provider=False, origin=True):
        calls = 0
        def guard():
            nonlocal calls
            calls += 1
            if calls == 3 and not provider:
                if timed: time.sleep(.04); self.guard_settled.append(True)
                else: raise TimeoutError('Post-turn readiness operation deadline exceeded')
        run = PostTurnReadinessService._run_owned
        owned_calls = 0
        async def limited(operation, *, seconds, name):
            nonlocal owned_calls
            owned_calls += 1
            return await run(operation, seconds=min(seconds, .01) if owned_calls == 3 else seconds, name=name)
        if provider:
            self.f.process_session.request = AsyncMock(side_effect=TimeoutError('provider reply unknown'))
        with patch.object(PostTurnReadinessService, '_run_owned', staticmethod(limited if timed else run)):
            with self.assertRaises((ThreadBootstrapPrestartRefusal, TimeoutError)):
                await self.f.thread_service.create(project_id=self.f.project.id,
                    bootstrap_token='a'*32, admission_guard=guard,
                    **({'recipient_origin': self.origin} if origin else {}))
        self.assertEqual(len(self.started), 1)
        self.observed = self.f.workers.store.assignment(self.started[0].id)
        return self.observed

    async def test_actual_postworker_guard_refusal_records_settled_origin(self):
        current = await self.refuse()
        self.assertEqual((current.status, current.fence, current.lease), ('cancelled', 2, None))
        self.assertIsNotNone(current.started_at)
        self.assertEqual(self.stopped, [current.id])
        self.assertIsNotNone(current.failure, 'joined pre-provider refusal lost canonical recovery proof')
        self.assertEqual(current.failure.source_native_code, 'before_provider_start')
        self.assertEqual(current.failure.worker_id, current.assigned_worker_id)
        details = current.failure.details
        self.assertEqual(details['logical_id'], self.origin.logical_id)
        self.assertEqual(details['attribution_sha256'], self.origin.attribution_sha256)
        self.assertEqual(details['handoff_requested_at'], self.origin.handoff_requested_at)
        self.assertEqual(details['generation'], 0)
        self.assertIs(details['provider_start_called'], False)
        self.assertIs(details['worker_start_called'], True)
        self.assertIs(details['admission_guard_joined'], True)
        self.assertIs(details['worker_teardown_settled'], True)
        self.assertEqual(self.f.workspaces.get(current.execution_workspace_id, self.f.actor).status, 'released')
        self.assertEqual(self.f.bootstrap_bindings.store.load().bindings, [])
        self.assertEqual(self.f.process_session.requests, [])

    async def test_real_owned_thread_deadline_joins_guard_before_proof(self):
        current = await self.refuse(timed=True)
        self.assertEqual(self.guard_settled, [True])
        self.assertIsNotNone(current.failure)
        self.assertTrue(current.failure.details['guard_deadline_exceeded'])
        self.assertEqual(self.stopped, [current.id])

    async def test_failed_teardown_never_records_recovery_proof(self):
        self.cleanup_error = True
        current = await self.refuse()
        self.assertIsNone(current.failure)
        self.assertIn(current.id, self.manager.sessions)

    async def test_pending_executor_never_records_recovery_proof(self):
        self.pending = 1
        current = await self.refuse()
        self.assertIsNone(current.failure)
        self.assertIn(current.id, self.manager.sessions)

    async def test_after_provider_unknown_never_records_preprovider_proof(self):
        current = await self.refuse(provider=True)
        self.assertIsNone(current.failure)
        self.f.process_session.request.assert_awaited_once()

    async def test_missing_recipient_origin_keeps_legacy_cancellation(self):
        current = await self.refuse(origin=False)
        self.assertIsNone(current.failure)

    async def test_terminal_fence_drift_refuses_witness_commit(self):
        service = self.f.workers
        original = service.record_bootstrap_preprovider_refusal
        def changed(before, refusal, *, actor):
            service.store.update(lambda state: state.model_copy(update={'assignments':[
                row.model_copy(update={'fence': row.fence + 1}) if row.id == before.id else row
                for row in state.assignments]}))
            return original(before, refusal, actor=actor)
        service.record_bootstrap_preprovider_refusal = changed
        current = await self.refuse()
        self.assertEqual(current.fence, 3)
        self.assertIsNone(current.failure)

    async def test_cancelled_waiter_joins_terminal_witness_writer(self):
        import threading
        entered = threading.Event()
        service = self.f.workers
        original = service.record_bootstrap_preprovider_refusal
        def delayed(*args, **kwargs):
            entered.set()
            time.sleep(.04)
            return original(*args, **kwargs)
        service.record_bootstrap_preprovider_refusal = delayed
        task = asyncio.create_task(self.refuse())
        deadline = time.monotonic() + 2
        while not entered.is_set() and not task.done() and time.monotonic() < deadline:
            await asyncio.sleep(.001)
        self.assertTrue(entered.is_set(), 'actual cleanup never reached terminal writer')
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        current = service.store.assignment(self.started[0].id)
        self.assertIsNotNone(current.failure)
        self.assertEqual(self.stopped, [current.id])
        self.assertNotIn(current.id, self.manager.sessions)


class PostworkerRecipientRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """Typed witness consumer controls, separate from the real producer above."""
    def setUp(self):
        from tests import test_owner_preworker_reservation as existing
        self.f = existing.RecipientRecoveryTests(); self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.failures = 0
        self.remaining_refusals = 1; self.typed = True; self.origins = []; self.original = None
        self.f.n.threads.create.side_effect = self.create

    async def create(self, **kwargs):
        from codex_web.execution_workers import AssignmentCancelRequest
        from codex_web.failures import FailureReason, create_failure
        self.origins.append(kwargs['recipient_origin'])
        result = await self.f.create(**kwargs)
        if self.remaining_refusals:
            self.remaining_refusals -= 1
            bound = self.f.n.bound
            original = self.f.workers.store.assignment(bound.assignment_id)
            cancelled = self.f.workers.cancel_bootstrap(original.id,
                AssignmentCancelRequest(expected_fence=original.fence,
                    reason='Private consumer-only typed settled refusal'), actor=self.f.actor)
            origin = kwargs['recipient_origin']
            # This is explicitly a consumer fixture. The producer tests above
            # exercise creation of this closed witness through actual owners.
            details = {'phase':'before_provider_start','provider_start_called':False,
                'worker_start_called':True,'admission_guard_joined':True,
                'worker_teardown_settled':True,'reservation_fence':original.fence,
                'generation':origin.generation,'logical_id':origin.logical_id,
                'created_by':original.created_by,'attribution_sha256':origin.attribution_sha256,
                'work_item_ref':origin.work_item_ref,'handoff_requested_at':origin.handoff_requested_at,
                'binding_id':origin.binding_id,'origin_actor_id':origin.actor_id}
            failure = create_failure(FailureReason.EXECUTION_TIMEOUT,
                source_subsystem='thread-bootstrap-admission', correlation_id=original.subject.ref,
                assignment_id=original.id, execution_id=original.execution_id,
                worker_id=original.assigned_worker_id, source_native_code='before_provider_start', details=details)
            if self.typed:
                self.f.workers.store.update(lambda state: state.model_copy(update={'assignments':[
                    row.model_copy(update={'failure':failure}) if row.id==original.id else row
                    for row in state.assignments]}))
            self.f.n.fixture.bootstraps.pop(bound.thread_id)
            self.f.n.fixture.assignments.pop('assignment-platform-thread', None)
            self.f.n.bound = None
            if self.original is None: self.original = self.f.workers.store.assignment(original.id)
            raise ThreadBootstrapPrestartRefusal(status_code=503, detail={'code':'bootstrap_prestart_failed'})
        return result

    async def refuse_once(self):
        with self.assertRaises(ThreadBootstrapPrestartRefusal): await self.f.n.prepare()
        return self.original.model_dump(mode='json')

    async def test_same_pending_nonce_recovers_once_and_preserves_original(self):
        before = await self.refuse_once()
        pin = await self.f.n.prepare()
        self.f.n.fixture.validate(pin)
        self.assertEqual([origin.generation for origin in self.origins], [0,1])
        self.assertEqual(self.f.workers.store.assignment(self.original.id).model_dump(mode='json'), before)
        self.assertEqual(self.f.n.state.handoff.requested_at, 1)
        self.assertIsNone(self.f.n.state.handoff.acknowledged_at)
        await self.f.n.prepare()
        self.assertEqual(len(self.origins), 2)

    async def test_concurrent_recovery_has_one_distinct_generation(self):
        await self.refuse_once()
        pins = await asyncio.gather(self.f.n.prepare(), self.f.n.prepare())
        self.assertEqual(pins[0].recipient, pins[1].recipient)
        self.assertEqual([origin.generation for origin in self.origins], [0,1])

    async def test_second_failure_never_gets_third_generation(self):
        from codex_web.owner_routing import OwnerRoutingError
        self.remaining_refusals = 2
        await self.refuse_once()
        with self.assertRaises(ThreadBootstrapPrestartRefusal): await self.f.n.prepare()
        with self.assertRaisesRegex(OwnerRoutingError, 'recovery_pending'): await self.f.n.prepare()
        self.assertEqual([origin.generation for origin in self.origins], [0,1])

    async def test_historical_missing_proof_remains_held(self):
        from codex_web.owner_routing import OwnerRoutingError
        self.typed = False
        before = await self.refuse_once()
        with self.assertRaisesRegex(OwnerRoutingError, 'bootstrap_pending'): await self.f.n.prepare()
        self.assertEqual(len(self.origins), 1)
        self.assertEqual(self.f.workers.store.assignment(self.original.id).model_dump(mode='json'), before)

    async def test_exact_proof_guards_reject_changed_identity_unknown_or_unsettled(self):
        from codex_web.owner_routing import OwnerRoutingError
        await self.refuse_once()
        original = self.original
        changes = [('provider_start_called', True), ('worker_teardown_settled',False),
            ('admission_guard_joined',False), ('generation',1), ('binding_id','foreign'),
            ('origin_actor_id','foreign'), ('handoff_requested_at',2),
            ('attribution_sha256','f'*64)]
        for key,value in changes:
            with self.subTest(key=key):
                details = {**original.failure.details,key:value}
                changed = original.model_copy(update={'failure':original.failure.model_copy(update={'details':details})})
                self.f.workers.store.update(lambda state: state.model_copy(update={'assignments':[
                    changed if row.id==original.id else row for row in state.assignments]}))
                with self.assertRaisesRegex(OwnerRoutingError,'bootstrap_pending'): await self.f.n.prepare()
                self.assertEqual(len(self.origins),1)
        from codex_web.failures import FailureReason, create_failure
        unknown_failure = create_failure(FailureReason.UNKNOWN_OUTCOME,
            source_subsystem=original.failure.source_subsystem,
            correlation_id=original.failure.correlation_id,
            assignment_id=original.id, execution_id=original.execution_id,
            worker_id=original.assigned_worker_id,
            source_native_code=original.failure.source_native_code,
            details=original.failure.details)
        unknown = original.model_copy(update={'failure': unknown_failure})
        self.f.workers.store.update(lambda state: state.model_copy(update={'assignments':[
            unknown if row.id==original.id else row for row in state.assignments]}))
        with self.assertRaisesRegex(OwnerRoutingError,'bootstrap_pending'): await self.f.n.prepare()
        self.assertEqual(len(self.origins),1)

    async def test_changed_source_fence_never_creates(self):
        from codex_web.owner_routing import OwnerRoutingError
        await self.refuse_once()
        self.f.n.fixture.assignments['assignment-existing-thread'].fence += 1
        with self.assertRaisesRegex(OwnerRoutingError,'bootstrap_pending'): await self.f.n.prepare()
        self.assertEqual(len(self.origins),1)

    async def test_changed_current_nonce_never_creates_recovery(self):
        from codex_web.owner_routing import OwnerRoutingError
        await self.refuse_once()
        current = self.f.n.state.model_copy(deep=True)
        current.handoff.requested_at += 1
        self.f.n.fixture.f.env.states.put(current.ref, current)
        with self.assertRaisesRegex(OwnerRoutingError, 'preparation_changed'):
            await self.f.n.prepare()
        self.assertEqual(len(self.origins), 1)

    async def test_context_proof_serializes_only_exact_assignment_fields_once(self):
        from unittest.mock import patch
        from codex_web.execution_workers import ExecutionAssignment
        preparation = self.f.n.routing.recipient_preparer
        real = ExecutionAssignment.model_dump
        seen = []
        def counted(instance,*args,**kwargs):
            seen.append(kwargs.get('include'))
            return real(instance,*args,**kwargs)
        proof,profile,actor = preparation.context(self.f.n.state,self.f.n.binding,
            'orchestrator',('platform',),discover=False)
        # Test the pure attribution constructor, not current authority reads.
        route = self.f.n.routing
        bootstrap = route.bootstraps.get_by_thread(self.f.n.binding.thread_id,self.f.actor)
        assignment = route.assignments(bootstrap.assignment_id)
        source = route.sessions.native_custody(self.f.n.binding.thread_id)
        with patch.object(ExecutionAssignment,'model_dump',counted):
            repeated = preparation._context_proof(self.f.n.state,self.f.n.binding,
                'orchestrator',('platform',),bootstrap,assignment,source,None,profile,actor,
                route.snapshot(self.f.n.state))
        self.assertEqual(len(seen),1)
        self.assertEqual(set(repeated['assignment']),set(seen[0]))
        self.assertEqual(repeated['assignment'],{key:real(assignment,mode='json').get(key)
            for key in repeated['assignment']})
