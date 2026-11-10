"""Real Home run/event/preflight owners; identity/selection and native edges are synthetic."""
from __future__ import annotations

import asyncio
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from codex_web.models import Project
from codex_web.runtime.execution import TurnExecutionService, ExecutionPrestartRefusal
from codex_web.services.automation_outcomes import AutomationOutcomeReconciliationService
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.turns import TurnService
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from tests import test_home_operator_followup as home_fixture
from tests import test_turn_execution as turn_fixture
from tests import test_native_reasoning_registration as registration_fixture


class HomePreinputReconciliationTests(unittest.IsolatedAsyncioTestCase):
    setUp = home_fixture.HomeFollowupTests.setUp
    publish = home_fixture.HomeFollowupTests.publish
    admit = home_fixture.HomeFollowupTests.admit
    launch = home_fixture.HomeFollowupTests.launch

    async def queued_run(self):
        self.host = turn_fixture._Host()
        self.host._project = lambda _: Project(id='home', name='Home', path='/private')
        self.runtime = TurnExecutionService(self.host, actor_resolver=lambda *_: self.actor)
        self.preflight = ExecutionPreflightService(ExecutionPreflightStore(self.sql), clock=lambda: 101.)
        prepare = getattr(self.preflight.store, 'prepare_home_followup_index', None)
        if prepare:
            prepare()
        self.runtime.queued_preflight = self.preflight
        self.sql.record_replace('active_turns', {})
        self.turns.preflight, self.turns.execution = self.preflight, self.runtime
        async def queue(thread, payload, **kw):
            kw['admission_guard']()
            self.original = self.runtime.enqueue_turn(thread_id=thread, project_id='home',
                message=payload.message, source=kw['source'], execution_id=kw['execution_id'],
                execution_profile_id='orchestration-only', agent_profile_id='operator',
                agent_profile_revision=3, agent_profile_actor_id=self.actor.identity_id)
            self.attempt = self.preflight.retain_queued(self.original,
                organization_id='local', workspace_id='default')
            return {'queued': True, 'queuedId': self.original.id}
        self.turns.start = queue
        run = self.admit().run
        self.run = await self.launch(run)
        self.snapshot = self.attempt.queued_turn_snapshot
        self.outcomes = AutomationOutcomeReconciliationService(self.runs, workers=NS())
        self.outcomes.home_operator_reconciler = getattr(self.followup, 'reconcile_preinput', None)
        return self.run

    def current_run(self):
        return self.runs.store.get(self.run.id, organization_id='local', workspace_id='default')

    def disposition(self, *, typed=True, observe=False):
        attempt, claim = self.preflight.claim_queued_admission(self.attempt.id)
        self.assertIsNotNone(claim)
        self.claim = claim
        self.assertTrue(self.preflight.begin_queued_start(attempt.id, claim_id=claim))
        if observe:
            self.preflight.queued_disposition_observer = getattr(self.followup, 'observe_preinput_refusal', None)
        return self.preflight.queued_disposition(attempt.id, status='blocked', claim_id=claim,
            preinput_refusal=typed, detail={'code': 'native_input_authority_denied', 'message': 'Local refusal'})

    async def test_typed_runtime_refusal_settles_run_and_releases_capacity_without_replay(self):
        await self.queued_run()
        self.preflight.queued_disposition_observer = getattr(self.followup, 'observe_preinput_refusal', None)
        self.runtime.start_thread_turn_now = AsyncMock(side_effect=ExecutionPrestartRefusal(
            status_code=403, detail={'code': 'native_input_authority_denied', 'message': 'Current Home event delegation is unavailable'}))
        await self.runtime.drain_thread_queue(self.original.thread_id)
        result = self.current_run()
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.result_code, 'home_followup_preinput_refused')
        retained = self.preflight.store.get(self.attempt.id)
        self.assertEqual(retained.status, 'blocked')
        self.assertEqual(retained.queued_turn_snapshot, self.snapshot)
        self.assertIsNone(retained.queued_start_receipt)
        self.runtime.start_thread_turn_now.assert_awaited_once()
        self.assertEqual(self.outcomes.reconcile(result.id, actor=self.actor), result)
        second = self.admit(event_id='genuine-next-event').run
        await self.launch(second)
        self.assertEqual(self.runs.store.get(second.id, organization_id='local', workspace_id='default').status, 'running')

    async def test_operator_reconcile_recovers_projection_after_observer_absent(self):
        await self.queued_run()
        saved = self.disposition()
        self.runs.clock = lambda: 1000.  # Expiry revokes input, not accounting.
        result = self.outcomes.reconcile(self.run.id, actor=self.actor)
        self.assertEqual(result.status, 'failed')
        self.assertEqual(self.preflight.store.get(saved.id), saved)

    async def test_legacy_blocked_requires_explicit_choice_and_preserves_unknown(self):
        await self.queued_run()
        first = self.disposition(typed=False)
        # Actual normal Retry creates attempt2 of the SAME immutable original.
        service = TurnService.__new__(TurnService)
        service.preflight, service.execution = self.preflight, self.runtime
        service.recovery = NS(replacement_thread_id=lambda _: None)
        self.runtime.schedule_queue_drain = Mock()
        response = await service.retry_preflight(self.original.thread_id, first.id, actor=self.actor)
        self.assertEqual(response['queuedId'], self.original.id)
        saved = self.disposition(typed=False)
        self.assertEqual(saved.attempt_number, 2)
        self.assertEqual(saved.queued_turn_snapshot, self.snapshot)
        self.runs.clock = lambda: 1000.
        self.assertEqual(self.outcomes.reconcile(self.run.id, actor=self.actor).status, 'running')
        result = self.outcomes.reconcile(self.run.id, actor=self.actor, cancel_unknown_home=True)
        self.assertEqual(result.status, 'cancelled')
        self.assertIn('UNKNOWN', result.result_reason)
        self.assertEqual(self.preflight.store.get(saved.id), saved)
        with self.assertRaises(ValueError):
            self.followup.authorize_input(source=self.original.source, assignment=self.assignment,
                actor=self.actor, project=NS(id='home'))
        self.assertFalse(self.preflight.begin_queued_start(saved.id, claim_id=self.claim))

    async def test_code_and_forged_detail_do_not_create_local_witness(self):
        await self.queued_run()
        self.disposition(typed=False)
        saved = self.preflight.store.get(self.attempt.id)
        forged = {'version': '1.0', 'attempt_id': saved.id, 'claim_id': saved.queued_admission_claim_id,
            'attempt_number': 1, 'snapshot_sha256': self.preflight.queued_snapshot_digest(self.snapshot)}
        self.preflight.queued_disposition(saved.id, status='blocked', claim_id=self.claim,
            detail={'code': 'native_input_authority_denied', 'message': 'provider supplied', 'local_preinput_refusal': forged})
        self.assertEqual(self.outcomes.reconcile(self.run.id, actor=self.actor).status, 'running')

    async def test_retry_winning_before_reconcile_prevents_settlement(self):
        await self.queued_run()
        self.disposition()
        _, _, acquired = self.preflight.claim_retry(self.attempt.id, actor=self.actor)
        self.assertTrue(acquired)
        self.assertEqual(self.outcomes.reconcile(self.run.id, actor=self.actor).status, 'running')

    async def test_stale_claim_and_changed_snapshot_refuse(self):
        await self.queued_run()
        saved = self.disposition()
        for change in ({'queued_admission_claim_id': 'foreign'},
                {'queued_turn_snapshot': {**self.snapshot, 'message': 'different'}}):
            with self.subTest(change=tuple(change)):
                self.preflight.store.update(saved.id, lambda _: saved.model_copy(update=change))
                self.assertEqual(self.outcomes.reconcile(self.run.id, actor=self.actor).status, 'running')
        self.preflight.store.update(saved.id, lambda _: saved)
        self.assertEqual(self.outcomes.reconcile(self.run.id, actor=self.actor).status, 'failed')

    async def test_unknown_or_begin_winning_is_not_terminalized_even_by_explicit_choice(self):
        await self.queued_run()
        attempt, claim = self.preflight.claim_queued_admission(self.attempt.id)
        self.preflight.begin_queued_start(attempt.id, claim_id=claim)
        self.runs.clock = lambda: 1000.
        self.assertEqual(self.outcomes.reconcile(self.run.id, actor=self.actor, cancel_unknown_home=True).status, 'running')
        self.assertIsNone(self.preflight.store.get(attempt.id).queued_start_receipt)

    async def test_current_authority_and_foreign_tenant_are_required(self):
        await self.queued_run()
        self.disposition()
        self.allowed = False
        with self.assertRaises(ValueError):
            self.outcomes.reconcile(self.run.id, actor=self.actor)
        self.allowed = True
        with self.assertRaises(KeyError):
            self.outcomes.reconcile(self.run.id, actor=self.actor.model_copy(update={'organization_id': 'foreign'}))
        self.selected['binding']['revision'] += 1
        with self.assertRaises(ValueError):
            self.outcomes.reconcile(self.run.id, actor=self.actor)
        self.assertEqual(self.current_run().status, 'running')

    async def test_cancel_refuses_unexpired_or_busy_home_without_interrupt(self):
        await self.queued_run()
        saved = self.disposition(typed=False)
        with self.assertRaises(ValueError):
            self.outcomes.reconcile(self.run.id, actor=self.actor, cancel_unknown_home=True)
        self.runs.clock = lambda: 1000.
        self.sql.record_apply('active_turns', upserts={self.original.thread_id: {'turn_id': 'foreign-turn'}})
        with self.assertRaises(ValueError):
            self.outcomes.reconcile(self.run.id, actor=self.actor, cancel_unknown_home=True)
        self.assertEqual(self.preflight.store.get(saved.id), saved)
        self.assertEqual(self.sql.record_get('active_turns', self.original.thread_id), {'turn_id': 'foreign-turn'})

    async def test_namespace_barrier_orders_cancel_before_concurrent_retry(self):
        await self.queued_run()
        self.disposition(typed=False)
        self.runs.clock = lambda: 1000.
        entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
        original_read = self.preflight.store.for_home_followup
        def read(*args, **kwargs):
            rows = original_read(*args, **kwargs)
            entered.set()
            if not release.wait(5):
                raise AssertionError('private barrier was not released')
            return rows
        self.preflight.store.for_home_followup = read
        cancel = asyncio.create_task(asyncio.to_thread(self.outcomes.reconcile, self.run.id,
            actor=self.actor, cancel_unknown_home=True))
        self.assertTrue(await asyncio.to_thread(entered.wait, 5))
        def retry():
            attempted.set()
            return self.preflight.claim_retry(self.attempt.id, actor=self.actor)
        task = asyncio.create_task(asyncio.to_thread(retry))
        self.assertTrue(await asyncio.to_thread(attempted.wait, 5))
        release.set()
        result, _ = await asyncio.gather(cancel, task)
        self.assertEqual(result.status, 'cancelled')
        with self.assertRaises(ValueError):
            self.followup.authorize_input(source=self.original.source, assignment=self.assignment,
                actor=self.actor, project=NS(id='home'))

    async def test_positive_started_state_and_ambiguous_original_refuse_cancellation(self):
        await self.queued_run()
        saved = self.disposition(typed=False)
        self.runs.clock = lambda: 1000.
        self.preflight.store.update(saved.id, lambda row: row.model_copy(update={
            'status': 'started', 'started_at': 102.}))
        self.assertEqual(self.outcomes.reconcile(self.run.id, actor=self.actor,
            cancel_unknown_home=True).status, 'running')
        self.preflight.store.update(saved.id, lambda _: saved)
        duplicate = self.original.model_copy(update={'id': 'another-original'})
        self.preflight.retain_queued(duplicate, organization_id='local', workspace_id='default')
        self.assertEqual(self.outcomes.reconcile(self.run.id, actor=self.actor,
            cancel_unknown_home=True).status, 'running')

    async def test_existing_http_reconcile_choice_is_strict_and_requires_operator(self):
        from fastapi import FastAPI, Request
        import httpx
        from codex_web.api.automation_outcomes import build_automation_outcomes_router
        await self.queued_run()
        saved = self.disposition(typed=False)
        self.runs.clock = lambda: 1000.
        self.actor = self.actor.model_copy(update={'assurance': 'mfa'})
        app = FastAPI()
        @app.middleware('http')
        async def actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)
        app.include_router(build_automation_outcomes_router(self.outcomes))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://private') as client:
            path = f'/api/automation-runs/{self.run.id}/reconcile'
            response = await client.post(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['run']['status'], 'running')
            response = await client.post(path, json={'cancel_unknown_home': 'true'})
            self.assertEqual(response.status_code, 422)
            response = await client.post(path, json={'cancel_unknown_home': True, 'native_outcome': 'not_sent'})
            self.assertEqual(response.status_code, 422)
            previous_actor = self.actor
            self.actor = self.actor.model_copy(update={'roles': ()})
            response = await client.post(path, json={'cancel_unknown_home': True})
            self.assertEqual(response.status_code, 403)
            self.actor = previous_actor
            response = await client.post(path, json={'cancel_unknown_home': True})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['run']['status'], 'cancelled')
        self.assertEqual(self.preflight.store.get(saved.id), saved)

    async def test_exact_input_lookup_survives_large_permanent_home_history_and_legacy_backfill(self):
        await self.queued_run()
        saved = self.disposition(typed=False)
        for index in range(205):
            unrelated = self.original.model_copy(update={'id': f'unrelated-{index}',
                'execution_id': f'other-execution-{index}', 'source': 'web', 'created_at': 200. + index})
            self.preflight.retain_queued(unrelated, organization_id='local', workspace_id='default')
        # Simulate an authoritative old row before the derived index existed.
        prefix = self.preflight.store._home_followup_prefix('local', 'default', saved.execution_id)
        self.sql.record_apply(self.preflight.store.NAMESPACE, upserts={}, deletes=(
            prefix + saved.id, 'metadata/home-followup-index-1.0'))
        self.runs.clock = lambda: 1000.
        self.assertEqual(self.outcomes.reconcile(self.run.id, actor=self.actor,
            cancel_unknown_home=True).status, 'running')
        self.preflight.store.prepare_home_followup_index()
        self.assertEqual(self.outcomes.reconcile(self.run.id, actor=self.actor,
            cancel_unknown_home=True).status, 'cancelled')
        self.assertEqual(self.preflight.store.get(saved.id), saved)

    async def test_index_ready_does_not_hide_writer_between_last_page_and_completion(self):
        await self.queued_run()
        saved = self.disposition(typed=False)
        prefix = self.preflight.store._home_followup_prefix('local', 'default', saved.execution_id)
        self.sql.record_apply(self.preflight.store.NAMESPACE, upserts={}, deletes=(
            prefix + saved.id, 'metadata/home-followup-index-1.0'))
        read = self.sql.record_page_projected
        inserted = False
        def writer_after_page(*args, **kwargs):
            nonlocal inserted
            rows, cursor = read(*args, **kwargs)
            if not inserted:
                inserted = True
                self.preflight.retain_queued(self.original.model_copy(update={'id': 'concurrent-original'}),
                    organization_id='local', workspace_id='default')
            return rows, cursor
        self.sql.record_page_projected = writer_after_page
        self.preflight.store.prepare_home_followup_index()
        self.runs.clock = lambda: 1000.
        self.assertEqual(len(self.preflight.store.for_home_followup(organization_id='local',
            workspace_id='default', execution_id=saved.execution_id)), 2)
        self.assertEqual(self.outcomes.reconcile(self.run.id, actor=self.actor,
            cancel_unknown_home=True).status, 'running')

    async def test_stale_historical_authority_does_not_abort_other_reconciliation(self):
        await self.queued_run()
        self.disposition()
        historical = self.run.model_copy(update={'id': 'historical-missing-definition',
            'automation_id': 'missing-definition',
            'execution_ids': ('home-followup-historical-missing-definition',)})
        def retain_history(state):
            state.runs.insert(0, historical)
            return state
        self.runs.store.update(retain_history)
        await self.followup.recover_pending()
        self.assertEqual(self.current_run().status, 'failed')
        self.assertEqual(self.runs.store.get(historical.id, organization_id='local', workspace_id='default'), historical)
        original_reconcile = self.followup.reconcile_preinput
        def unexpected(run_id, **kw):
            if run_id == historical.id:
                raise RuntimeError('synthetic infrastructure failure')
            return original_reconcile(run_id, **kw)
        self.followup.reconcile_preinput = unexpected
        with self.assertRaisesRegex(RuntimeError, 'synthetic infrastructure failure'):
            await self.followup.recover_pending()

    def register_executive_then_attach_home(self):
        # Real opt-in registration used by application startup; only registry,
        # provider/bridge transport edges come from its existing small fixture.
        registration = registration_fixture.NativeReasoningRegistrationTests(methodName='runTest')
        registration.setUp()
        registration.execution = self.runtime
        self.runtime.procedure_admission = NS(reasoning_preparation=None)
        registration.register()
        self.followup.attach_preinput_observer(self.preflight)
        return registration.components.bridge.observe_admission

    async def test_real_executive_registration_then_home_attach_preserves_both_observers(self):
        await self.queued_run()
        previous = self.register_executive_then_attach_home()
        saved = self.disposition()
        previous.assert_called_once_with(saved)
        self.assertEqual(self.current_run().status, 'failed')
        self.assertEqual(self.preflight.store.get(saved.id), saved)

    async def test_registered_executive_failure_does_not_drop_home_projection(self):
        await self.queued_run()
        previous = self.register_executive_then_attach_home()
        previous.side_effect = RuntimeError('synthetic Executive observation failure')
        saved = self.disposition()
        previous.assert_called_once_with(saved)
        self.assertEqual(self.current_run().status, 'failed')
        self.assertEqual(saved.status, 'blocked')

    async def test_home_projection_failure_does_not_drop_registered_executive(self):
        await self.queued_run()
        previous = self.register_executive_then_attach_home()
        self.followup.observe_preinput_refusal = Mock(side_effect=ValueError('expected Home authority refusal'))
        saved = self.disposition()
        previous.assert_called_once_with(saved)
        self.followup.observe_preinput_refusal.assert_called_once_with(saved)
        self.assertEqual(self.current_run().status, 'running')

    async def test_non_home_disposition_still_reaches_registered_executive(self):
        await self.queued_run()
        previous = self.register_executive_then_attach_home()
        other = self.original.model_copy(update={'id': 'non-home-original',
            'execution_id': 'non-home-execution', 'source': 'web'})
        attempt = self.preflight.retain_queued(other, organization_id='local', workspace_id='default')
        _, claim = self.preflight.claim_queued_admission(attempt.id)
        self.assertTrue(self.preflight.begin_queued_start(attempt.id, claim_id=claim))
        saved = self.preflight.queued_disposition(attempt.id, status='blocked', claim_id=claim,
            preinput_refusal=True, detail={'code': 'synthetic_local_refusal', 'message': 'No native write'})
        previous.assert_called_once_with(saved)
        self.assertEqual(self.current_run().status, 'running')


class AuthenticatedHomeReconciliationTests(unittest.IsolatedAsyncioTestCase):
    """Real identity/session and reconciliation owners; Home/native edges remain fixtures."""
    setUp = home_fixture.HomeFollowupTests.setUp
    publish = home_fixture.HomeFollowupTests.publish
    admit = home_fixture.HomeFollowupTests.admit
    launch = home_fixture.HomeFollowupTests.launch
    queued_run = HomePreinputReconciliationTests.queued_run
    current_run = HomePreinputReconciliationTests.current_run
    disposition = HomePreinputReconciliationTests.disposition

    async def prepare_real_identity(self, *, session=False, typed=False, observe=False):
        from codex_web.services.identity import IdentityService
        from codex_web.storage.identity_state import IdentityStateStore
        from codex_web.identity import AuthenticationAssurance, TenantScope
        self.real_identity = IdentityService(IdentityStateStore(self.sql))
        self.actor = self.real_identity.local_trusted_actor()
        if session:
            self.credentials = self.real_identity.create_session(identity_id=self.actor.identity_id,
                scope=TenantScope(), assurance=AuthenticationAssurance.MFA)
            self.actor = self.real_identity.authenticate_session(self.credentials.session_token,
                csrf_token=self.credentials.csrf_token, require_csrf=True).actor
        self.identity = self.followup.identity = self.execution.identity = self.real_identity
        self.selected['binding']['selected_by'] = self.actor.identity_id
        self.assignment.created_by = self.actor.identity_id
        self.payload['owner_identity_id'] = self.actor.identity_id
        self.publish(publisher=self.actor.identity_id)
        await self.queued_run()
        self.saved = self.disposition(typed=typed, observe=observe)
        self.runs.clock = lambda: 1000.

    async def api_reconcile(self, actor):
        from codex_web.api.automation_outcomes import build_automation_outcomes_router, AutomationReconcileRequest
        from starlette.requests import Request
        self.outcomes.sync_attention = AsyncMock()
        route = build_automation_outcomes_router(self.outcomes).routes[0]
        request = Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': [],
            'state': {'identity_actor': actor}})
        from fastapi import HTTPException
        try:
            result = await route.endpoint(self.run.id, request, AutomationReconcileRequest(cancel_unknown_home=True))
            return {'status_code': 200, **result}
        except HTTPException as error:
            return {'status_code': error.status_code, 'detail': error.detail}

    async def test_real_local_trusted_operator_cancels_legacy_unknown_without_input(self):
        await self.prepare_real_identity()
        result = await self.api_reconcile(self.actor)
        self.assertEqual(result['status_code'], 200, result)
        self.assertEqual(result['run']['status'], 'cancelled')
        self.assertIn('UNKNOWN', result['run']['result_reason'])
        self.assertEqual(self.preflight.store.get(self.saved.id), self.saved)
        self.assertEqual(self.actor.assurance, 'local_trusted')

    async def test_real_mfa_session_operator_cancels_legacy_unknown_without_downgrade(self):
        await self.prepare_real_identity(session=True)
        result = await self.api_reconcile(self.actor)
        self.assertEqual(result['status_code'], 200, result)
        self.assertEqual(result['run']['status'], 'cancelled')
        self.assertEqual(self.preflight.store.get(self.saved.id), self.saved)
        self.assertEqual(self.actor.assurance, 'mfa')
        self.assertEqual(self.actor.session_id, self.credentials.session_id)

    async def test_revoked_session_and_changed_membership_cannot_cancel(self):
        from codex_web.services.identity import AuthorizationError
        await self.prepare_real_identity(session=True)
        self.real_identity.revoke_session(self.credentials.session_id)
        with self.assertRaises(AuthorizationError):
            self.outcomes.reconcile(self.run.id, actor=self.actor, cancel_unknown_home=True)
        self.assertEqual(self.current_run(), self.run)
        self.assertEqual(self.preflight.store.get(self.saved.id), self.saved)

    async def test_changed_current_membership_cannot_cancel(self):
        from codex_web.services.identity import AuthorizationError
        await self.prepare_real_identity()
        state = self.real_identity.store.load()
        membership = next(m for m in state.memberships if m.identity_id == self.actor.identity_id)
        self.real_identity.revoke_membership(membership.id, actor=self.actor)
        with self.assertRaises(AuthorizationError):
            self.outcomes.reconcile(self.run.id, actor=self.actor, cancel_unknown_home=True)
        self.assertEqual(self.current_run(), self.run)

    async def test_primary_membership_is_not_api_authentication_and_native_comparison_stays_strict(self):
        await self.prepare_real_identity()
        primary = self.real_identity.actor_for_identity(self.actor.identity_id, scope=self.actor.tenant)
        result = await self.api_reconcile(primary)
        self.assertEqual(result['status_code'], 403)
        with self.assertRaisesRegex(ValueError, 'current human administrator'):
            self.followup.context(self.run, self.actor, check_controls=False)
        self.assertEqual(self.current_run(), self.run)

    async def test_real_identity_automatic_observer_settles_without_authenticated_request(self):
        await self.prepare_real_identity(typed=True, observe=True)
        self.assertEqual(self.current_run().status, 'failed')
        self.assertEqual(self.current_run().result_code, 'home_followup_preinput_refused')
        self.assertEqual(self.preflight.store.get(self.saved.id), self.saved)
