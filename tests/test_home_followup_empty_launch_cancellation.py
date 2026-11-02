"""Canonical empty-launch cancellation; no application, native send or live fault.

Identity, Definitions, Run, bootstrap/worker custody, queue and preflight owners
use a private SQLite store. Host/native/controller edges are existing fixtures.
"""
from __future__ import annotations

import asyncio
import copy
import threading
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from codex_web.execution_workers import AssignmentCancelRequest
from codex_web.models import QueuedTurn
from codex_web.services.automation_definitions import install_automation_definitions, AutomationEventTriggerService
from codex_web.services.automation_outcomes import AutomationOutcomeReconciliationService
from codex_web.services.automation_runs import AutomationRunService
from codex_web.services.canonical_events import CanonicalEventBus
from codex_web.services.home_operator_followup import HomeOperatorFollowup, SOURCE
from codex_web.services.identity import AuthorizationError
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.storage.automation_runs import AutomationRunStore, AutomationRunNotFoundError
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.turn_queue import TurnQueueRepository
from tests import test_home_operator_followup as automation_fixture
from tests import test_home_operator_host as host_fixture


class EmptyHomeLaunchCancellationTests(unittest.IsolatedAsyncioTestCase):
    publish = automation_fixture.HomeFollowupTests.publish
    admit = automation_fixture.HomeFollowupTests.admit

    async def asyncSetUp(self):
        # Borrow setup, not TestCase inheritance/collection or detached cleanup.
        self.env = host_fixture.HomeOperatorHostTests(methodName='runTest')
        self.env.addCleanup = self.addCleanup
        self.env.addAsyncCleanup = self.addAsyncCleanup
        await self.env.asyncSetUp()
        env = self.env
        self.sql, self.actor, self.identity = env.f.sqlite, env.actor, env.f.identity
        self.registry = env.registry
        self.now = time.time()
        project = env.projects.get('home', self.actor.tenant)
        selected = project.home_operator
        env.projects.repository.update_home_operator(organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id, expected_revision=selected.revision,
            binding=selected.model_copy(update={'revision': selected.revision + 1,
                'agent_profile_id': 'home-operator', 'agent_profile_revision': 1}))

        def selection(_actor):
            binding = env.projects.get('home', self.actor.tenant).home_operator
            return {'status': 'configured', 'binding': binding.model_dump(),
                'thread': {'id': 'operator-thread', 'execution_profile_id': 'orchestration-only'}}

        self.definitions = install_automation_definitions(self.registry)
        self.runs = AutomationRunService(AutomationRunStore(self.sql), self.definitions,
            clock=lambda: self.now)
        self.events = CanonicalEventStore(self.sql)
        self.triggers = AutomationEventTriggerService(self.registry, CanonicalEventBus(self.events))
        self.preflight = ExecutionPreflightService(ExecutionPreflightStore(self.sql), clock=lambda: self.now)
        self.preflight.store.prepare_home_followup_index()
        self.queue = TurnQueueRepository(self.sql, env.root / 'private-empty-launch-queue.json')
        self.queue.get('operator-thread')
        self.sql.record_replace('active_turns', {})
        self.turns = NS(preflight=self.preflight, execution=NS(thread_is_active=lambda _thread: False),
            start=AsyncMock(), interrupt=AsyncMock())
        self.followup = HomeOperatorFollowup(runs=self.runs,
            operator=NS(status=selection, agent_profiles=env.profiles),
            identity=self.identity, events=self.events, turns=self.turns,
            controller=NS(store=NS(control=lambda: NS(mode='active', dry_run=False, simulation=False)),
                _scoped_pause_reason=lambda *a, **k: None, _exclusive_goal_scope_reason=lambda *a: None),
            authority=NS(evaluate=lambda *a, **k: NS(outcome='allow')), event_triggers=self.triggers,
            workers=env.f.workers, bootstrap_bindings=env.bindings)
        binding = selection(self.actor)['binding']
        self.payload = {'name': 'Existing Home completion follow-up', 'lifecycle': 'enabled',
            'trigger': {'type': 'canonical_event', 'event_type': 'ci.pipeline'},
            'target': {'kind': 'home_operator', 'id': 'home-operator', 'home_thread_id': 'operator-thread',
                'home_binding_revision': binding['revision'], 'agent_profile_revision': 1},
            'instructions': 'Inspect the completion once; preserve all uncertain originals.',
            'owner_identity_id': self.actor.identity_id, 'work_item_policy': 'reuse_only',
            'budget': {'max_input_tokens': 1000, 'max_output_tokens': 500, 'max_duration_seconds': 60},
            'retry': {'max_attempts': 1}}
        self.publish(publisher=self.actor.identity_id)
        run = self.admit().run
        claimed, self.run = self.followup._claim(run, self.followup._custody('operator-thread', self.actor))
        self.assertTrue(claimed)
        self.now += 61
        self.outcomes = AutomationOutcomeReconciliationService(self.runs, workers=NS())
        self.outcomes.home_operator_reconciler = self.followup.reconcile_preinput
        # An unrelated retained UNKNOWN original must survive a successful repair.
        old = self.original('old-unknown', execution_id='old-human-execution', source='web')
        self.old_attempt = self.preflight.retain_queued(old, organization_id='local', workspace_id='default')
        _, claim = self.preflight.claim_queued_admission(self.old_attempt.id)
        self.assertTrue(self.preflight.begin_queued_start(self.old_attempt.id, claim_id=claim))
        self.old_attempt = self.preflight.store.get(self.old_attempt.id)

    def original(self, identifier='late-original', *, execution_id=None, source=None):
        return QueuedTurn(id=identifier, thread_id='operator-thread', project_id='home', created_at=self.now,
            message=self.followup.message(self.run, self.definitions.resolve_reference(self.run.definition_ref,
                organization_id='local', workspace_id='default', project_id=self.run.project_id)[0]),
            source=source or SOURCE + self.run.id, execution_id=execution_id or self.run.execution_ids[0],
            execution_profile_id='orchestration-only', agent_profile_id='home-operator',
            agent_profile_revision=1, agent_profile_actor_id=self.actor.identity_id)

    def current(self):
        return self.runs.store.get(self.run.id, organization_id='local', workspace_id='default')

    def cancel(self, actor=None):
        return self.outcomes.reconcile(self.run.id, actor=actor or self.actor, cancel_unknown_home=True)

    def assert_cancelled_without_effects(self, result, before_preflight):
        self.assertEqual(result.status, 'cancelled')
        self.assertEqual(result.result_code, 'home_followup_scheduling_cancelled_unknown')
        self.assertIn('UNKNOWN', result.result_reason)
        self.assertIsNone(result.home_terminal)
        self.assertIsNone(result.home_interrupt_attempted_at)
        self.assertEqual(self.sql.record_items(self.preflight.store.NAMESPACE), before_preflight)
        self.assertEqual(self.preflight.store.get(self.old_attempt.id), self.old_attempt)
        self.turns.start.assert_not_called()
        self.turns.interrupt.assert_not_called()
        self.assertEqual(self.queue.get('operator-thread'), [])

    async def test_expired_empty_run_live_idle_cancels_scheduling_only(self):
        before = copy.deepcopy(self.sql.record_items(self.preflight.store.NAMESPACE))
        result = self.cancel()
        self.assert_cancelled_without_effects(result, before)
        self.assertEqual(self.env.current().status, 'running')
        self.assertEqual(self.cancel(), result)  # Terminal accounting is idempotent, not a resend.

    async def test_normal_fenced_cancelled_custody_needs_no_warmup(self):
        a = self.env.current()
        retired = self.env.f.workers.cancel_bootstrap(a.id,
            AssignmentCancelRequest(expected_fence=a.fence, reason='Private idle preservation fixture'), actor=self.actor)
        self.assertEqual(retired.fence, a.fence + 1)
        self.assertIsNone(retired.lease)
        before = copy.deepcopy(self.sql.record_items(self.preflight.store.NAMESPACE))
        self.assert_cancelled_without_effects(self.cancel(), before)

    async def test_actual_expired_worker_lease_lost_custody_needs_no_warmup(self):
        a = self.env.current()
        lost = self.env.f.workers.recover_expired(actor=self.actor, now=a.lease.expires_at + 1)
        self.assertIn(a.id, lost)
        self.assertEqual(self.env.current().status, 'lost')
        self.assertIsNone(self.env.current().lease)
        before = copy.deepcopy(self.sql.record_items(self.preflight.store.NAMESPACE))
        self.assert_cancelled_without_effects(self.cancel(), before)

    async def test_unexpired_and_scope_only_automatic_calls_leave_original_running(self):
        self.assertEqual(self.followup.reconcile_preinput(self.run.id, scope=self.actor.tenant), self.run)
        with self.assertRaisesRegex(ValueError, 'Explicit operator authority'):
            self.followup.reconcile_preinput(self.run.id, scope=self.actor.tenant, cancel_unknown=True)
        self.now = self.run.started_at + 59
        with self.assertRaisesRegex(ValueError, 'expired Home grant'):
            self.cancel()
        self.assertEqual(self.current(), self.run)

    async def test_nonadmin_foreign_tenant_and_revoked_membership_refuse(self):
        for actor, refusal in (
                (self.actor.model_copy(update={'roles': ()}), AuthorizationError),
                (self.actor.model_copy(update={'workspace_id': 'foreign'}), AutomationRunNotFoundError)):
            with self.subTest(actor=actor.workspace_id, roles=actor.roles):
                with self.assertRaises(refusal):
                    self.cancel(actor)
                self.assertEqual(self.current(), self.run)
        membership = next(m for m in self.identity.store.load().memberships if m.identity_id == self.actor.identity_id)
        self.identity.revoke_membership(membership.id, actor=self.actor)
        with self.assertRaises(AuthorizationError):
            self.cancel()
        self.assertEqual(self.current(), self.run)

    async def test_changed_current_definition_cannot_cancel_historical_run(self):
        self.payload['name'] = 'New separately published revision'
        self.publish(publisher=self.actor.identity_id)
        with self.assertRaisesRegex(ValueError, 'current.*Automation|Current.*Automation'):
            self.cancel()
        self.assertEqual(self.current(), self.run)

    async def test_missing_partial_or_malformed_index_is_not_empty_proof(self):
        marker = 'metadata/home-followup-index-1.0'
        for value in (None, {'schema_version': '1.0', 'complete': False}, {'complete': True}):
            with self.subTest(marker=value):
                self.sql.record_apply(self.preflight.store.NAMESPACE,
                    upserts={marker: value} if value is not None else {}, deletes=(marker,) if value is None else ())
                self.assertEqual(self.cancel(), self.run)
                self.assertEqual(self.current(), self.run)

    async def test_malformed_queue_or_active_marker_never_means_idle(self):
        for raw in ({'not': 'a queue'}, [{'id': 'incomplete'}]):
            with self.subTest(raw=raw):
                self.sql.record_update('turn_queues', 'operator-thread', lambda _old: raw, default=None)
                with self.assertRaises(ValueError):
                    self.cancel()
                self.assertEqual(self.current(), self.run)
        self.sql.record_update('turn_queues', 'operator-thread', lambda _old: None, default=None)
        self.sql.record_update('active_turns', 'operator-thread',
            lambda _old: {'opaque_active_identity': True}, default=None)
        with self.assertRaisesRegex(ValueError, 'activity is unresolved'):
            self.cancel()
        self.assertEqual(self.current(), self.run)

    async def test_matching_lazy_queue_without_preflight_is_preserved(self):
        original = self.original()
        self.queue.update('operator-thread', lambda rows: [*rows, original])
        self.assertEqual(self.preflight.store.for_home_followup(organization_id='local',
            workspace_id='default', execution_id=self.run.execution_ids[0]), [])
        with self.assertRaisesRegex(ValueError, 'durable queued input'):
            self.cancel()
        self.assertEqual(self.queue.get('operator-thread'), [original])
        self.assertEqual(self.current(), self.run)

    async def test_current_fence_change_refuses_without_cancellation(self):
        original = self.followup._empty_launch_guard
        def changed(actor, selected):
            guard = original(actor, selected)
            self.env.mutate_assignment(fence=self.env.current().fence + 1)
            return guard
        self.followup._empty_launch_guard = changed
        with self.assertRaisesRegex(ValueError, 'custody changed'):
            self.cancel()
        self.assertEqual(self.current(), self.run)

    async def test_current_wrong_profile_or_execution_definition_never_grants_repair(self):
        original = self.env.current()
        changes = (
            {'agent_profile': original.agent_profile.model_copy(update={'profile_revision': 99})},
            {'execution_profile_definition': original.execution_profile_definition.model_copy(update={'revision': 99})},
        )
        for change in changes:
            with self.subTest(change=tuple(change)):
                self.env.mutate_assignment(**change)
                with self.assertRaises(ValueError):
                    self.cancel()
                self.assertEqual(self.current(), self.run)
                self.env.mutate_assignment(agent_profile=original.agent_profile,
                    execution_profile_definition=original.execution_profile_definition)

    async def test_preflight_begin_and_late_started_state_winning_refuse(self):
        original = self.original()
        attempt = self.preflight.retain_queued(original, organization_id='local', workspace_id='default')
        _, claim = self.preflight.claim_queued_admission(attempt.id)
        self.assertTrue(self.preflight.begin_queued_start(attempt.id, claim_id=claim))
        self.assertEqual(self.cancel(), self.run)
        started = self.preflight.queued_disposition(attempt.id, status='started', claim_id=claim)
        self.assertEqual(started.status, 'started')
        self.assertEqual(self.cancel(), self.run)
        self.assertEqual(self.preflight.store.get(attempt.id), started)

    async def writer_race(self, *, lazy_queue):
        entered, release, attempted, finished = (threading.Event() for _ in range(4))
        read = self.preflight.store.for_home_followup
        def fenced_read(**kwargs):
            result = read(**kwargs)
            entered.set()
            if not release.wait(5):
                raise AssertionError('Private cancellation barrier not released')
            return result
        self.preflight.store.for_home_followup = fenced_read
        original = self.original()
        cancel = asyncio.create_task(asyncio.to_thread(self.cancel))
        self.assertTrue(await asyncio.to_thread(entered.wait, 5))
        def writer():
            attempted.set()
            if lazy_queue:
                self.queue.update('operator-thread', lambda rows: [*rows, original])
            else:
                attempt = self.preflight.retain_queued(original, organization_id='local', workspace_id='default')
                _, claim = self.preflight.claim_queued_admission(attempt.id)
                self.assertTrue(self.preflight.begin_queued_start(attempt.id, claim_id=claim))
            finished.set()
        late = asyncio.create_task(asyncio.to_thread(writer))
        self.assertTrue(await asyncio.to_thread(attempted.wait, 5))
        self.assertFalse(finished.is_set())
        release.set()
        result, _ = await asyncio.gather(cancel, late)
        self.assertEqual(result.status, 'cancelled')
        self.assertTrue(finished.is_set())
        # Late evidence remains intact, but current Run authority cannot send.
        # Isolate the terminal-Run denial from the independent duration guard.
        self.now = self.run.started_at + 1
        with self.assertRaisesRegex(ValueError, 'event run or current assignment'):
            self.followup.authorize_input(source=original.source, assignment=self.env.current(),
                actor=self.identity.actor_for_identity(self.actor.identity_id, scope=self.actor.tenant), project=NS(id='home'))
        self.assertEqual(self.current(), result)
        self.turns.start.assert_not_called()
        self.turns.interrupt.assert_not_called()

    async def test_cancel_namespace_fence_precedes_late_preflight_begin(self):
        await self.writer_race(lazy_queue=False)

    async def test_cancel_namespace_fence_precedes_late_lazy_queue_insert(self):
        await self.writer_race(lazy_queue=True)
