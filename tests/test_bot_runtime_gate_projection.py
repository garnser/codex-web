"""Bot transport telemetry and canonical Project eligibility stay distinct."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from codex_web.models import BotConnection
from codex_web.services.bot_runtime_telemetry import BotRuntimeTelemetry
from codex_web.services.bots import BotService


class BotRuntimeGateProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.connection = BotConnection(id='fixture', provider='slack', name='Fixture',
                                        project_id='project-a', created_at=1, updated_at=1)
        self.rows = [self.connection]
        self.telemetry = BotRuntimeTelemetry(events_file=Path(self.temp.name) / 'events.jsonl')
        self.reason = None
        self.calls = []
        def gate(project):
            self.calls.append(project)
            if isinstance(self.reason, Exception):
                raise self.reason
            return self.reason
        self.service = BotService(
            connections=SimpleNamespace(load_connections=lambda: self.rows),
            bindings=SimpleNamespace(load_bindings=lambda: []),
            telemetry=self.telemetry,
            runtime=SimpleNamespace(tasks={}, gate_reason=gate),
            load_gitlab_routing_settings=lambda: SimpleNamespace(enabled=False, projects={}))

    def status(self):
        return self.service.status()['runtimeStatus'][0]

    def test_connected_ping_does_not_present_historical_pause_as_current_gate(self):
        with patch('codex_web.services.bot_runtime_telemetry.time.time', return_value=10):
            self.telemetry.set_status(self.connection, 'paused', gateReason='operator_paused')
        with patch('codex_web.services.bot_runtime_telemetry.time.time', return_value=20):
            self.telemetry.set_status(self.connection, 'connected', lastPingAt=20)
        before = self.telemetry.snapshot()
        result = self.status()
        self.assertIsNone(result['gateReason'])
        self.assertTrue(result['gateEligible'])
        self.assertEqual(result['status'], 'connected')
        self.assertEqual(result['lastObservedGateReason'], 'operator_paused')
        self.assertEqual(result['lastObservedGateReasonAt'], 10)
        self.assertEqual(self.telemetry.snapshot(), before)

    def test_connected_transport_does_not_clear_a_current_project_pause(self):
        self.reason = 'operator_paused'
        self.telemetry.set_status(self.connection, 'connected', lastPingAt=20)
        result = self.status()
        self.assertFalse(result['gateEligible'])
        self.assertEqual(result['gateReason'], 'operator_paused')
        self.assertEqual(result['status'], 'connected')
        self.assertEqual(result['projectId'], 'project-a')

    def test_legacy_pause_timestamp_is_unavailable_not_latest_ping_time(self):
        self.telemetry.status['fixture'] = {'connectionId': 'fixture', 'status': 'connected',
                                           'gateReason': 'operator_paused', 'updatedAt': 999}
        result = self.status()
        self.assertIsNone(result['lastObservedGateReasonAt'])
        self.assertIsNone(result['gateReason'])

    def test_failed_gate_lookup_is_not_eligible(self):
        self.reason = RuntimeError('fixture failure')
        self.telemetry.set_status(self.connection, 'connected')
        result = self.status()
        self.assertFalse(result['gateEligible'])
        self.assertEqual(result['gateReason'], 'bot_runtime_gate_unavailable')
        self.assertEqual(result['gateReasonSource'], 'unavailable')

    def test_removed_connection_is_not_eligible_or_resolved_as_another_project(self):
        self.telemetry.set_status(self.connection, 'connected')
        self.rows = []
        result = self.status()
        self.assertFalse(result['gateEligible'])
        self.assertEqual(result['gateReason'], 'bot_runtime_connection_unavailable')
        self.assertEqual(result['gateReasonSource'], 'unavailable')
        self.assertEqual(self.calls, [])

    def test_each_status_read_rechecks_canonical_gate_without_mutating_telemetry(self):
        self.telemetry.set_status(self.connection, 'connected')
        self.assertTrue(self.status()['gateEligible'])
        self.reason = 'operator_paused'
        self.assertFalse(self.status()['gateEligible'])
        self.assertEqual(self.calls, ['project-a', 'project-a'])

    def test_shared_project_gate_is_resolved_once_per_snapshot(self):
        other = self.connection.model_copy(update={'id': 'fixture-two'})
        self.rows.append(other)
        for connection in self.rows:
            self.telemetry.set_status(connection, 'connected')
        self.assertEqual(len(self.service.status()['runtimeStatus']), 2)
        self.assertEqual(self.calls, ['project-a'])

    def test_actual_canonical_pause_then_resume_updates_current_projection(self):
        from codex_web.identity import AuthenticationActor, AuthenticationAssurance, MembershipRole, PrincipalKind
        from codex_web.reconciliation_gates import ReconcilerDeclaration, ReconcilerStartupClass
        from codex_web.services.bot_runtime_gate import project_bot_runtime_gate
        from codex_web.services.reconciliation_gates import ReconciliationGateService
        from codex_web.storage.sqlite_state import SQLiteStateStore
        actor = AuthenticationActor(identity_id='admin-a', principal_kind=PrincipalKind.HUMAN,
                                    organization_id='org-a', workspace_id='ws-a',
                                    roles=(MembershipRole.ADMIN,), assurance=AuthenticationAssurance.MFA)
        gates = ReconciliationGateService(SQLiteStateStore(Path(self.temp.name) / 'gate.sqlite3'),
                                         readiness=lambda *_: {}, identity=object())
        gates.register(ReconcilerDeclaration(service_id='bot-runtime',
                       startup_class=ReconcilerStartupClass.PRE_READINESS_BOUNDED))
        connections = SimpleNamespace(runtime_actor=lambda _: actor)
        self.service.runtime.gate_reason = project_bot_runtime_gate(gates, connections)
        gates.pause('bot-runtime', 'project-a', actor=actor, reason='fixture pause')
        self.telemetry.set_status(self.connection, 'paused', gateReason='operator_paused')
        self.telemetry.set_status(self.connection, 'connected', lastPingAt=20)
        self.assertFalse(self.status()['gateEligible'])
        gates.resume('bot-runtime', 'project-a', actor=actor)
        current = self.status()
        self.assertTrue(current['gateEligible'])
        self.assertIsNone(current['gateReason'])
        self.assertEqual(current['lastObservedGateReason'], 'operator_paused')
        self.assertEqual(current['status'], 'connected')


class BotRuntimeStatusRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_gate_status_projection_runs_outside_event_loop_thread(self):
        import threading
        import httpx
        from fastapi import FastAPI
        from codex_web.api.bots import build_bots_router
        event_loop_thread = threading.get_ident()
        observed = []
        def status():
            observed.append(threading.get_ident())
            return {'runtimeStatus': []}
        app = FastAPI()
        app.include_router(build_bots_router(SimpleNamespace(status=status)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://fixture') as client:
            response = await client.get('/api/bots')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'runtimeStatus': []})
        self.assertEqual(len(observed), 1)
        self.assertNotEqual(observed[0], event_loop_thread)
