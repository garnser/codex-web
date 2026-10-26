from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from codex_web.autonomy import AutonomyControl, AutonomyScopedPause
from codex_web.models import ExecutionPreflightAttempt, Project, ProjectCoordinationDispatch, QueuedOperatorAdmission, QueuedTurn
from codex_web.reconciliation_gates import (
    ReconcilerDeclaration, ReconcilerStartupClass, ReconciliationGateStoreState,
    ReconciliationProjectControl,
)
from codex_web.services.reconciliation_gates import ReconciliationGateService
from codex_web.services.runtime import RuntimeService
from codex_web.services.runtime_diagnostics import RuntimeHealthService
from codex_web.services.runtime_work_admission import (
    RuntimeWorkAdmission, actionable_owner_health,
)
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.autonomy import AutonomyStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class HeldWorkReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SQLiteStateStore(Path(self.temp.name) / 'state.sqlite3')
        self.preflights = ExecutionPreflightStore(self.store)
        self.project = Project(id='p', name='Project', path='/private',
                               organization_id='org', workspace_id='ws')
        self.control = AutonomyControl(mode='paused')
        self.enabled = True
        self.queues = {}
        self.owner_projects = {'quinn': ['p']}
        self.stopped_tasks = []
        self.failures = {}
        self.ready = asyncio.Event()
        self.ready.set()
        self.codex = SimpleNamespace(ready=self.ready,
                                     proc=SimpleNamespace(pid=42, poll=lambda: None))
        self.gates = ReconciliationGateService(
            self.store, readiness=self.forbidden_probe, identity=object())
        self.gates.register(ReconcilerDeclaration(
            service_id='bot-runtime',
            startup_class=ReconcilerStartupClass.PROJECT_READINESS_DEPENDENT))

    @staticmethod
    def forbidden_probe(*args):
        raise AssertionError('health must not perform admission/readiness effects')

    def admission(self):
        return RuntimeWorkAdmission(
            control=self.control, runtime_enabled=self.enabled,
            projects=SimpleNamespace(get=lambda key: self.project if key == 'p'
                                     else (_ for _ in ()).throw(KeyError(key))),
            gates=self.gates, preflights=self.preflights)

    def queued(self, source='gitlab', *, ident='q'):
        return QueuedTurn(id=ident, thread_id='thread', project_id='p',
                          message='retained original', created_at=time.time() - 1000,
                          source=source)

    def retain(self, queued, status='reconciliation_required', **changes):
        attempt = ExecutionPreflightAttempt(
            id='preflight-queued-' + queued.id, correlation_id='correlation',
            execution_id='execution', organization_id='org', workspace_id='ws',
            thread_id=queued.thread_id, project_id=queued.project_id,
            message=queued.message, queued_turn_snapshot=queued.model_dump(mode='json'),
            status=status, created_at=1, updated_at=1)
        self.preflights.put(attempt.model_copy(update=changes))
        return attempt

    def owner_health(self):
        return dict(enabled=self.enabled, stoppedTasks=self.stopped_tasks,
            **actionable_owner_health(self.owner_projects, owners=('quinn',),
                admission=self.admission(), binding_for=lambda *args: None,
                thread_active=lambda key: False, queue_depth=lambda key: 0))

    def service(self):
        health = RuntimeHealthService(
            codex=self.codex,
            bot_runtime=SimpleNamespace(fingerprints={}, tasks={}),
            telemetry=SimpleNamespace(snapshot=lambda: {}, recent=lambda limit: []),
            load_bindings=lambda: [SimpleNamespace(thread_id='thread')], terminal_failures=self.failures,
            terminal_recovery_tasks={}, terminal_failure_window_seconds=lambda: 300,
            load_queues=lambda: self.queues, slack_provider_health=lambda: {},
            gitlab_sync_status=lambda: {}, autonomy_health=self.owner_health)
        # Assign the observation seam independently of construction so the
        # unchanged old diagnostics can run this same false-alarm regression.
        health.work_admission = self.admission
        return health

    def refresh(self):
        before = self.store.documents()
        health = self.service()
        snapshot = health.refresh_sync()
        self.assertEqual(self.store.documents(), before, 'diagnostics wrote canonical state')
        return health, snapshot

    def assert_ready(self, health, expected):
        service = RuntimeService(codex=self.codex, runtime_health=health.health)
        if expected:
            self.assertTrue(asyncio.run(service.readyz())['ok'])
        else:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(service.readyz())
            self.assertEqual(raised.exception.status_code, 503)

    def test_control_observation_never_initializes_or_migrates_the_canonical_ledger(self):
        controls = AutonomyStateStore(self.store)
        before = self.store.documents()
        with self.assertRaises(ValueError):
            controls.observe_control()
        self.assertEqual(self.store.documents(), before)
        controls.control()  # Normal canonical initialization belongs to setup.
        self.store.record_apply(controls.namespace, upserts={
            'control': self.control.model_dump(mode='json')})
        before = self.store.documents()
        self.assertEqual(controls.observe_control(), self.control)
        self.assertEqual(self.store.documents(), before)

    def test_global_pause_explains_old_automated_queue_and_owner_without_changes(self):
        original = self.queued()
        self.queues = {'thread': [original]}
        health, snapshot = self.refresh()
        self.assert_ready(health, True)
        self.assertEqual(snapshot['queuedTurns'], 1)
        self.assertEqual(snapshot['heldQueueThreads'], {'thread': 1})
        self.assertEqual(snapshot['heldQueueReasons'], {'thread': ['autonomy_paused']})
        self.assertEqual(snapshot['staleQueueThreads'], {})
        self.assertEqual(snapshot['autonomy']['idleActionableOwners'], [])
        self.assertEqual(self.queues['thread'], [original])

    def test_human_and_unknown_origins_use_canonical_queue_policy_in_mixed_queue(self):
        self.queues = {'thread': [self.queued(), self.queued('queued:steer:web', ident='human')]}
        health, snapshot = self.refresh()
        self.assert_ready(health, False)
        self.assertEqual(snapshot['staleQueueThreads'], {'thread': 1})
        self.assertEqual(snapshot['heldQueueThreads'], {'thread': 1})
        self.assertEqual(snapshot['queuedTurns'], 2)
        self.assertEqual(self.admission().queued(self.queued('unknown-producer'))['state'], 'held')

    def test_runnable_queue_and_idle_owner_remain_unhealthy(self):
        self.control = AutonomyControl()
        self.queues = {'thread': [self.queued()]}
        health, snapshot = self.refresh()
        self.assert_ready(health, False)
        self.assertEqual(snapshot['staleQueueThreads'], {'thread': 1})
        self.assertEqual(snapshot['autonomy']['idleActionableOwners'], ['quinn'])

    def test_project_and_bot_holds_explain_owner_but_do_not_relabel_ordinary_queue(self):
        for kind in ('project', 'bot'):
            with self.subTest(kind=kind):
                self.control = AutonomyControl(scoped_pauses=(AutonomyScopedPause(
                    scope='project', scope_id='p', reason='test hold', created_by='operator'),)
                    if kind == 'project' else ())
                self.store.put(self.gates.NAMESPACE, ReconciliationGateStoreState(controls=[
                    ReconciliationProjectControl(service_id='bot-runtime', project_id='p',
                        organization_id='org', workspace_id='ws', paused=kind == 'bot')
                ]).model_dump(mode='json'))
                health, snapshot = self.refresh()
                self.assert_ready(health, True)
                self.assertEqual(len(snapshot['autonomy']['heldActionableProjects']), 1)
                self.queues = {'thread': [self.queued()]}
                health, snapshot = self.refresh()
                self.assert_ready(health, False)
                self.assertEqual(snapshot['staleQueueThreads'], {'thread': 1})
                self.queues = {}

    def test_expired_identity_and_cross_tenant_pauses_do_not_suppress_owner_stall(self):
        self.control = AutonomyControl(scoped_pauses=(
            AutonomyScopedPause(scope='project', scope_id='p', reason='expired',
                created_by='operator', expires_at=1),
            AutonomyScopedPause(scope='identity', scope_id='someone', reason='identity only',
                created_by='operator'),))
        self.store.put(self.gates.NAMESPACE, ReconciliationGateStoreState(controls=[
            ReconciliationProjectControl(service_id='bot-runtime', project_id='p',
                organization_id='other', workspace_id='ws', paused=True)
        ]).model_dump(mode='json'))
        health, snapshot = self.refresh()
        self.assert_ready(health, False)
        self.assertEqual(snapshot['autonomy']['idleActionableOwners'], ['quinn'])

    def test_retained_original_reconciliation_and_identity_refusal_are_read_only_holds(self):
        self.control = AutonomyControl()
        self.owner_projects = {}
        original = self.queued('web')
        attempt = self.retain(original)
        self.queues = {'thread': [original]}
        health, snapshot = self.refresh()
        self.assert_ready(health, True)
        self.assertEqual(snapshot['heldQueueReasons'], {'thread': ['retained_reconciliation_required']})
        changed = original.model_copy(update={'message': 'different request'})
        self.assertEqual(self.admission().queued(changed)['code'], 'queued_retention_refused')
        self.assertEqual(self.preflights.get(attempt.id), attempt)

    def test_started_operator_original_is_not_eligible_for_another_admission(self):
        self.owner_projects = {}
        original = self.queued('operator-handoff-delivery')
        witness = QueuedOperatorAdmission(claim_id='claim', requested_by='operator',
            requested_at=1, reason='original reviewed admission',
            snapshot_sha256='1' * 64, queue_sha256='2' * 64)
        self.retain(original, status='started', operator_admission=witness, started_at=2)
        self.queues = {'thread': [original]}
        health, snapshot = self.refresh()
        self.assert_ready(health, True)
        self.assertEqual(snapshot['heldQueueReasons'], {'thread': ['retained_started']})
        self.assertEqual(snapshot['unknownQueueAdmissionThreads'], {})

    def test_unobserved_retained_admission_and_observation_overflow_remain_fatal(self):
        self.owner_projects = {}
        original = self.queued()
        self.retain(original, status='admitting')
        self.queues = {'thread': [original]}
        health, snapshot = self.refresh()
        self.assert_ready(health, False)
        self.assertEqual(snapshot['unknownQueueAdmissionThreads'], {'thread': 1})
        self.queues = {'thread': [self.queued(ident=str(i)) for i in range(129)]}
        health, snapshot = self.refresh()
        self.assert_ready(health, False)
        self.assertEqual(snapshot['heldQueueThreads'], {'thread': 128})
        self.assertEqual(snapshot['unknownQueueAdmissionThreads'], {'thread': 1})

    def test_governed_exception_is_unknown_even_when_global_is_paused(self):
        self.owner_projects = {}
        governed = self.queued('project-coordination').model_copy(update={
            'project_coordination_dispatch': ProjectCoordinationDispatch(
                goal_binding_id='binding', assignment_id='assignment', fence=1)})
        self.queues = {'thread': [governed]}
        health, snapshot = self.refresh()
        self.assert_ready(health, False)
        self.assertEqual(snapshot['unknownQueueAdmissionThreads'], {'thread': 1})
        self.assertEqual(snapshot['heldQueueThreads'], {})

    def test_missing_or_failed_observation_is_not_a_hold(self):
        self.queues = {'thread': [self.queued()]}
        health = self.service()
        health.work_admission = None
        snapshot = health.refresh_sync()
        self.assert_ready(health, False)
        self.assertEqual(snapshot['unknownQueueAdmissionThreads'], {'thread': 1})
        health = self.service()
        health.work_admission = self.forbidden_probe
        snapshot = health.refresh_sync()
        self.assert_ready(health, False)
        self.assertFalse(snapshot['ok'])

    def test_pause_does_not_hide_disabled_tasks_terminal_failures_or_codex_failure(self):
        self.queues = {'thread': [self.queued()]}
        self.stopped_tasks = ['owner-work']
        health, snapshot = self.refresh()
        self.assert_ready(health, False)
        self.assertIn('autonomy task stopped: owner-work', snapshot['problems'])
        self.stopped_tasks = []
        self.enabled = False
        health, snapshot = self.refresh()
        self.assert_ready(health, False)
        self.assertIn('autonomy is disabled', snapshot['problems'])
        self.enabled = True
        self.failures['thread'] = [(time.time(), 'known terminal failure')]
        health, snapshot = self.refresh()
        self.assert_ready(health, False)
        self.assertEqual(snapshot['terminalFailureThreads'], 1)
        self.failures.clear()
        self.ready.clear()
        health, snapshot = self.refresh()
        self.assert_ready(health, False)
        self.assertFalse(snapshot['codexReady'])
