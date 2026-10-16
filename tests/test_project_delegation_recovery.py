"""Real startup recovery plus canonical human binding/broker/queue admission."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from fastapi import HTTPException
from codex_web.storage.turn_queue import TurnQueueRepository
from codex_web.services.project_coordination import ProjectCoordinationActionProvider

from fastapi import FastAPI
from fastapi.testclient import TestClient
from codex_web.api.goal_execution_bindings import build_goal_execution_bindings_router
from codex_web.autonomy import AutonomyMode
from codex_web.goal_execution_bindings import (
    GoalExecutionMode, ProjectDelegationAuthorization, GoalExecutionBindingUpdate)
from codex_web.identity import PrincipalKind
from codex_web.services.goal_continuation import GoalContinuationService
from codex_web.services.goal_continuation_recovery import GoalContinuationRecoveryService
from codex_web.services.goal_execution_bindings import GoalExecutionBindingConflictError
from codex_web.storage.autonomy import AutonomyStateStore
from codex_web.storage.goal_execution_bindings import GoalExecutionBindingStore
from tests import test_project_coordination_delegation as fixtures


class ProjectDelegationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ProjectDelegatedDispatchTests.asyncSetUp
    _request = fixtures.ProjectDelegatedDispatchTests._request
    call = fixtures.ProjectDelegatedDispatchTests.call
    publish = fixtures.ProjectDelegatedDispatchTests.publish
    create = fixtures.ProjectDelegatedDispatchTests.create
    message = fixtures.ProjectDelegatedDispatchTests.message

    def recovery(self):
        store = AutonomyStateStore(self.f.sqlite)
        store.update(lambda s: s.model_copy(update={'control': s.control.model_copy(update={'mode': AutonomyMode.PAUSED})}))
        continuation = GoalContinuationService(self.goal_bindings, self.goals, self.sessions,
            owner_id='real-startup-controller', autonomy=SimpleNamespace(store=store))
        return GoalContinuationRecoveryService(self.goal_bindings, continuation, self.sessions), continuation

    def authorize(self, *, actor=None, expected=None):
        current = self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant)
        return self.goal_bindings.authorize_project_delegation(current.id, actor=actor or self.actor,
            expected_updated_at=current.updated_at if expected is None else expected,
            expected_delegation_revision=current.delegation_revision,
            reason='Explicit human Project delegation, not app continuation')

    async def test_real_recovery_and_reopen_admit_two_children_but_no_automatic_continuation(self):
        children = [await self.create(), await self.create('scanner', 'scanner')]
        before = self.goal_bindings.store.load()
        recovery, continuation = self.recovery()
        result = await recovery.recover_scope(scope=self.actor.tenant)
        self.assertEqual(result.eligible, 0)
        self.assertEqual(result.dispatched, ())
        self.assertEqual(self.goal_bindings.store.load(), before)
        direct = await continuation.dispatch_once(self.goal_binding.id, scope=self.actor.tenant)
        self.assertEqual(direct.outcome, 'not_dispatched')
        reopened = GoalExecutionBindingStore(self.f.sqlite).load()
        self.assertEqual(reopened.bindings[0].delegation_authorization, 'authorized')
        for i, (child, _) in enumerate(children):
            queued, _ = await self.message(child, key=f'restart-child-{i}')
            self.assertEqual(queued.source, 'project-coordination')
            await self.execution.drain_thread_queue(child)
        self.assertEqual(self.execution._start_thread_turn_now_admitted.await_count, 2)
        self.assertIsNotNone(self.execution.automation_wait_reason('business-scheduler'))
        self.assertEqual(self.sessions.get(self.goal_binding.agent_session_id, self.actor).status, 'ready')

    async def test_real_legacy_recovery_reproduces_block_then_explicit_authorization_preserves_history(self):
        # Legacy binding has no typed explicit delegation grant. Real recovery
        # must still enforce global pause and retain the original failure facts.
        self.goal_bindings.store.update(lambda s: s.model_copy(update={'bindings': [
            b.model_copy(update={'execution_mode': GoalExecutionMode.APPLICATION,
                'delegation_authorization': ProjectDelegationAuthorization.NONE}) for b in s.bindings]}))
        child, _ = await self.create()
        recovery, _ = self.recovery()
        result = await recovery.recover_scope(scope=self.actor.tenant)
        self.assertEqual(result.dispatched[0].reason, 'global_autonomy_paused')
        blocked = self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant)
        self.assertEqual(blocked.status, 'blocked')
        events = list(self.goal_bindings.store.load().events)
        status, _, _ = await self.call('POST', f'/threads/{child}/messages', {
            'message':'refuse before explicit authorization', 'idempotency_key':'legacy-denied',
            'goal_binding_id':self.goal_binding.id})
        self.assertEqual(status, 403)
        authorized = self.authorize()
        self.assertEqual(authorized.status, blocked.status)
        self.assertEqual(authorized.stop_reason, blocked.stop_reason)
        self.assertEqual(self.goal_bindings.store.load().events[:-1], events)
        self.assertEqual((await recovery.recover_scope(scope=self.actor.tenant)).eligible, 0)
        await self.message(child, key='legacy-authorized')
        await self.execution.drain_thread_queue(child)
        self.execution._start_thread_turn_now_admitted.assert_awaited_once()

    async def test_operator_pause_revokes_queued_input_and_cannot_start_app_resume(self):
        child, _ = await self.create()
        queued, _ = await self.message(child)
        self.goal_bindings.operator_stop(self.goal_binding.id, scope=self.actor.tenant,
            actor_id=self.actor.identity_id, cancelled=False, reason='explicit operator stop')
        await self.execution.drain_thread_queue(child)
        self.assertEqual(self.host._thread_queue(child), [queued])
        self.execution._start_thread_turn_now_admitted.assert_not_awaited()
        with self.assertRaises(GoalExecutionBindingConflictError):
            self.goal_bindings.resume_operator_pause(self.goal_binding.id, scope=self.actor.tenant,
                actor_id=self.actor.identity_id, reason='must not start another engine')
        self.assertEqual(self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant).delegation_authorization, 'revoked')

    async def test_authorization_cas_owner_lease_and_service_actor_refuse_without_events(self):
        before = self.goal_bindings.store.load()
        for actor, expected in ((self.actor, 0),
                (self.actor.model_copy(update={'identity_id':'foreign'}), None),
                (self.actor.model_copy(update={'principal_kind':PrincipalKind.SERVICE}), None)):
            with self.assertRaises(GoalExecutionBindingConflictError):
                self.authorize(actor=actor, expected=expected)
            self.assertEqual(self.goal_bindings.store.load(), before)
        self.goal_bindings.store.update(lambda s: s.model_copy(update={'bindings': [
            b.model_copy(update={'lease_owner_id':'other-controller','lease_expires_at':time.time()+60}) for b in s.bindings]}))
        leased = self.goal_bindings.store.load()
        with self.assertRaises(GoalExecutionBindingConflictError):
            self.authorize()
        self.assertEqual(self.goal_bindings.store.load(), leased)

    async def test_legacy_decode_grants_nothing_and_old_reader_refuses_new_state(self):
        from tests.fixtures.goal_binding_previous_reader import GoalExecutionBindingState as OldState
        current = self.goal_bindings.store.load().model_dump(mode='json')
        with self.assertRaises(ValueError):
            OldState.model_validate(current)
        legacy = {**current, 'schema_version':'1.0', 'bindings':[
            {k:v for k,v in b.items() if k not in {'execution_mode','delegation_authorization','delegation_revision'}}
            for b in current['bindings']]}
        decoded = GoalExecutionBindingStore._decode(legacy)
        self.assertEqual(decoded.bindings[0].execution_mode, 'application_continuation')
        self.assertEqual(decoded.bindings[0].delegation_authorization, 'none')
        self.assertEqual(decoded.events, self.goal_bindings.store.load().events)
        with self.assertRaises(ValueError):
            GoalExecutionBindingStore._decode({**legacy,'schema_version':'99'})

    async def test_operator_route_is_scoped_audited_and_preserves_blocked_status(self):
        self.goal_bindings.update(self.goal_binding.id, GoalExecutionBindingUpdate(status='blocked', reason='retained stop'),
            scope=self.actor.tenant, actor_id=self.actor.identity_id)
        current = self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant)
        app = FastAPI()
        @app.middleware('http')
        async def actor(request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)
        app.include_router(build_goal_execution_bindings_router(self.goal_bindings, self.sessions, self.goals))
        with TestClient(app) as client:
            path=f'/api/goals/{self.goal.id}/execution-bindings/{current.id}/authorize-project-delegation?project_id=home'
            response=client.post(path,json={'expected_updated_at':current.updated_at,'expected_delegation_revision':current.delegation_revision,'reason':'explicit new scope authorization'})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['item']['status'],'blocked')
            self.assertEqual(response.json()['item']['delegation_authorization'],'authorized')
            stale=client.post(path,json={'expected_updated_at':current.updated_at,'expected_delegation_revision':current.delegation_revision,'reason':'stale duplicate'})
            self.assertEqual(stale.status_code,409)
            forged=client.post(path,json={'expected_updated_at':current.updated_at,
                'expected_delegation_revision':current.delegation_revision,'reason':'forged grant',
                'delegation_authorization':'authorized'})
            self.assertEqual(forged.status_code,422)
            self.actor=self.actor.model_copy(update={'principal_kind':PrincipalKind.SERVICE,
                'service_scopes':('goals:admin',)})
            denied=client.post(path,json={'expected_updated_at':current.updated_at,
                'expected_delegation_revision':current.delegation_revision,'reason':'not a human operator'})
            self.assertEqual(denied.status_code,403)

    async def test_late_automatic_policy_write_cannot_revoke_new_operator_authorization(self):
        recovery, continuation = self.recovery()
        old = self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant).model_copy(
            update={'execution_mode':GoalExecutionMode.APPLICATION})
        before = self.goal_bindings.store.load()
        # Simulate the real stale continuation callback after a concurrent
        # transfer: its final transactional writer must refuse the new mode.
        result = continuation._finish_binding(old, scope=self.actor.tenant,
            status='blocked', reason='global_autonomy_paused')
        self.assertEqual(result.outcome,'not_dispatched')
        self.assertEqual(self.goal_bindings.store.load(),before)
        self.goal_bindings.heartbeat_continuation(old.id, scope=self.actor.tenant,
            owner_id='expired-app-owner')
        self.goal_bindings.release_continuation(old.id, scope=self.actor.tenant,
            owner_id='expired-app-owner',status='idle',reason='late app release')
        self.assertEqual(self.goal_bindings.store.load(),before)

    async def test_authorization_revision_denies_stale_control_even_when_clock_repeats(self):
        current = self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant)
        self.goal_bindings.operator_stop(current.id,scope=self.actor.tenant,
            actor_id=self.actor.identity_id,cancelled=False,reason='operator revoked')
        # Timestamp can repeat under clock skew; the typed grant revision cannot.
        self.goal_bindings.store.update(lambda s:s.model_copy(update={'bindings':[
            b.model_copy(update={'updated_at':current.updated_at}) for b in s.bindings]}))
        with self.assertRaises(GoalExecutionBindingConflictError):
            self.goal_bindings.authorize_project_delegation(current.id,actor=self.actor,
                expected_updated_at=current.updated_at,expected_delegation_revision=current.delegation_revision,
                reason='stale authorization must not undo operator stop')
        self.assertEqual(self.goal_bindings.get(current.id,scope=self.actor.tenant).delegation_authorization,'revoked')

    async def test_actual_terminal_event_cannot_dispatch_or_mutate_project_authorization(self):
        from codex_web.agent_runtime import AgentRuntimeEvent
        from codex_web.services.goal_continuation_events import GoalContinuationEventService
        _, continuation = self.recovery()
        service = GoalContinuationEventService(self.goal_bindings,self.sessions,continuation)
        before = self.goal_bindings.store.load()
        event = AgentRuntimeEvent(event_type='turn/completed',
            provider_native_session_id='native-governor')
        result = await service.handle_event(event,scope=self.actor.tenant)
        self.assertEqual(result.outcome, 'ignored')
        self.assertEqual(self.goal_bindings.store.load(),before)

    async def test_concurrent_operator_stop_always_wins_over_stale_authorization(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        current = self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        barrier = threading.Barrier(2)
        def authorize():
            barrier.wait(timeout=5)
            try:
                return self.goal_bindings.authorize_project_delegation(current.id,actor=self.actor,
                    expected_updated_at=current.updated_at,expected_delegation_revision=current.delegation_revision,
                    reason='concurrent operator authorization')
            except GoalExecutionBindingConflictError:
                return None
        def stop():
            barrier.wait(timeout=5)
            return self.goal_bindings.operator_stop(current.id,scope=self.actor.tenant,
                actor_id=self.actor.identity_id,cancelled=False,reason='new operator stop')
        with ThreadPoolExecutor(max_workers=2) as pool:
            a,b=pool.submit(authorize),pool.submit(stop)
            a.result(timeout=15);b.result(timeout=15)
        self.assertEqual(self.goal_bindings.get(current.id,scope=self.actor.tenant).delegation_authorization,'revoked')


    async def test_revoked_generation_stays_held_after_reauthorize_and_queue_reopen(self):
        child, _ = await self.create()
        old, _ = await self.message(child, key='generation-A')
        self.goal_bindings.operator_stop(self.goal_binding.id, scope=self.actor.tenant,
            actor_id=self.actor.identity_id, cancelled=False, reason='revoke A')
        await self.execution.drain_thread_queue(child)
        self.execution._start_thread_turn_now_admitted.assert_not_awaited()
        self.assertEqual(self.host._thread_queue(child), [old])
        current = self.authorize()
        repo = TurnQueueRepository(self.f.sqlite, self.f.sqlite.path.parent / 'queues.json')
        repo.put(child, [old])
        reopened_repo = TurnQueueRepository(self.f.sqlite, self.f.sqlite.path.parent / 'queues.json')
        self.host._thread_queue = reopened_repo.get
        self.host._thread_queue_record = reopened_repo.get
        self.host._update_thread_queue_record = reopened_repo.update
        self.host._put_thread_queue_record = reopened_repo.put
        self.turn_service.queue_policy.queue = reopened_repo.get
        self.turn_service.queue_policy.depth = lambda thread: len(reopened_repo.get(thread))
        reopened = reopened_repo.get(child)[0]
        self.assertEqual(reopened, old)
        with self.assertRaises(HTTPException):
            self.coordination.validate_queued_delegation(reopened)
        await self.execution.drain_thread_queue(child)
        self.execution._start_thread_turn_now_admitted.assert_not_awaited()
        new, _ = await self.message(child, key='generation-B')
        self.assertEqual(new.project_coordination_dispatch.delegation_revision, current.delegation_revision)
        self.assertNotEqual(new.project_coordination_dispatch.delegation_revision,
            old.project_coordination_dispatch.delegation_revision)
        await self.execution.drain_thread_queue(child)
        self.execution._start_thread_turn_now_admitted.assert_awaited_once()
        self.assertEqual(self.host._thread_queue(child), [old])
        self.assertEqual(self.execution._start_thread_turn_now_admitted.call_args.kwargs['execution_id'], new.execution_id)

    async def test_legacy_queue_proof_is_retained_but_never_upgraded_to_new_grant(self):
        child, _ = await self.create()
        queued, _ = await self.message(child)
        raw = queued.model_dump(mode='json')
        raw['project_coordination_dispatch'].pop('delegation_revision')
        raw['project_coordination_dispatch']['schema_version'] = '1.0'
        # Actual old persisted shape, not a new proof with a fabricated revision.
        repo = TurnQueueRepository(self.f.sqlite, self.f.sqlite.path.parent / 'queues.json')
        self.f.sqlite.record_apply(repo.namespace, upserts={child: [raw]})
        reopened_repo = TurnQueueRepository(self.f.sqlite, self.f.sqlite.path.parent / 'queues.json')
        self.host._thread_queue = reopened_repo.get
        self.host._thread_queue_record = reopened_repo.get
        self.host._update_thread_queue_record = reopened_repo.update
        self.host._put_thread_queue_record = reopened_repo.put
        self.turn_service.queue_policy.queue = reopened_repo.get
        self.turn_service.queue_policy.depth = lambda thread: len(reopened_repo.get(thread))
        legacy = reopened_repo.get(child)[0]
        self.assertIsNone(legacy.project_coordination_dispatch.delegation_revision)
        self.authorize()
        await self.execution.drain_thread_queue(child)
        self.execution._start_thread_turn_now_admitted.assert_not_awaited()
        self.assertEqual(self.host._thread_queue(child), [legacy])

    async def test_final_prestart_revoke_and_reauthorize_cannot_resurrect_selected_input(self):
        from concurrent.futures import ThreadPoolExecutor
        child, _ = await self.create()
        original, _ = await self.message(child)
        original = original.model_copy(deep=True)
        real = self.execution.start_thread_turn_now
        def advance_authority():
            self.goal_bindings.operator_stop(self.goal_binding.id, scope=self.actor.tenant,
                actor_id=self.actor.identity_id, cancelled=False, reason='concurrent prestart revoke')
            return self.authorize()
        async def race_then_enter(*args, **kwargs):
            with ThreadPoolExecutor(max_workers=1) as pool:
                current = pool.submit(advance_authority).result(timeout=10)
            self.assertGreater(current.delegation_revision,
                original.project_coordination_dispatch.delegation_revision)
            return await real(*args, **kwargs)
        self.execution.start_thread_turn_now = race_then_enter
        await self.execution.drain_thread_queue(child)
        self.execution._start_thread_turn_now_admitted.assert_not_awaited()
        self.assertEqual(self.host._thread_queue(child), [original])

    async def test_durable_intent_cannot_refresh_proof_during_execution(self):
        child, _ = await self.create()
        real = ProjectCoordinationActionProvider.execute
        captured = []
        async def advance_before_execution(provider, request, **kwargs):
            captured.append(request.parameters['project_coordination_dispatch'])
            self.goal_bindings.operator_stop(self.goal_binding.id, scope=self.actor.tenant,
                actor_id=self.actor.identity_id, cancelled=False, reason='after intent persistence')
            current = self.authorize()
            self.assertLess(captured[-1]['delegation_revision'], current.delegation_revision)
            # An old accepted intent without generation must not mint new authority either.
            params = dict(request.parameters)
            params.pop('project_coordination_dispatch')
            with self.assertRaises(HTTPException):
                await real(provider, request.model_copy(update={'parameters': params}), **kwargs)
            return await real(provider, request, **kwargs)
        with patch.object(ProjectCoordinationActionProvider, 'execute', advance_before_execution):
            status, _, result = await self.call('POST', f'/threads/{child}/messages', {
                'message':'old durable intent', 'idempotency_key':'intent-generation-A',
                'goal_binding_id':self.goal_binding.id})
        self.assertEqual(status, 200, result)
        self.assertNotEqual(result['intent']['status'], 'succeeded')
        self.assertEqual(len(captured), 1)
        self.assertEqual(self.host._thread_queue(child), [])
        await self.message(child, key='intent-generation-B')
