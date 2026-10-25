"""Closed request-local diagnostics preserve canonical read owners and failures."""
import asyncio
import json
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.identity import TenantScope
from codex_web.observability import JsonFormatter, correlated
from codex_web import work_graph_telemetry as telemetry
from codex_web.storage.postgres_state import PostgresStateStore
from codex_web.services.work_graph import WorkGraphService
from codex_web.storage.work_graph import WorkGraphStore
from codex_web.executive_roles import ExecutiveActivationCreate
from tests import test_executive_management as canonical


class ConnectionOwner:
    def __init__(self, connection, suppress=False):
        self.connection, self.suppress = connection, suppress
        self.entered = 0
        self.exits = []

    def __enter__(self):
        self.entered += 1
        return self.connection

    def __exit__(self, kind, error, traceback):
        self.exits.append((kind, error, traceback))
        return self.suppress


class WorkGraphReadTimingTests(unittest.TestCase):
    scope = TenantScope(organization_id='local', workspace_id='default')

    def test_connection_owner_keeps_exception_identity_and_suppression(self):
        for suppress in (False, True):
            with self.subTest(suppress=suppress):
                owner = ConnectionOwner(object(), suppress)
                failure = TimeoutError('PRIVATE-SQL-CONTENT')
                if suppress:
                    with telemetry.graph_read_timings(self.scope):
                        with telemetry.graph_read_connection(lambda: owner):
                            raise failure
                else:
                    with self.assertRaises(TimeoutError) as caught:
                        with telemetry.graph_read_timings(self.scope):
                            with telemetry.graph_read_connection(lambda: owner):
                                raise failure
                    self.assertIs(caught.exception, failure)
                self.assertEqual(owner.entered, 1)
                self.assertEqual(len(owner.exits), 1)
                self.assertIs(owner.exits[0][0], TimeoutError)
                self.assertIs(owner.exits[0][1], failure)
                self.assertIsNotNone(owner.exits[0][2])
                self.assertIsNone(telemetry._scope.get())

    def test_connection_acquisition_failure_and_logging_failure_preserve_owner(self):
        failure = TimeoutError('PRIVATE-CREDENTIAL')
        owner = ConnectionOwner(object())
        with patch('codex_web.observability.log_event', side_effect=asyncio.CancelledError):
            with self.assertRaises(TimeoutError) as caught:
                with telemetry.graph_read_timings(self.scope):
                    with telemetry.graph_read_connection(lambda: owner):
                        raise failure
            self.assertIs(caught.exception, failure)
            self.assertIs(owner.exits[0][1], failure)
            with self.assertRaises(TimeoutError) as acquisition:
                with telemetry.graph_read_timings(self.scope):
                    with telemetry.graph_read_connection(lambda: (_ for _ in ()).throw(failure)):
                        self.fail('unreachable')
            self.assertIs(acquisition.exception, failure)
        self.assertIsNone(telemetry._scope.get())

    def test_actual_projected_page_refusal_keeps_savepoint_and_legacy_fallback(self):
        class LegacyJSON(ValueError):
            sqlstate = '22P02'
        failure = LegacyJSON('PRIVATE-SQL-PAYLOAD')
        cursor_owner = ConnectionOwner(SimpleNamespace(execute=lambda *_: (_ for _ in ()).throw(failure)))
        savepoint = ConnectionOwner(None)
        connection = SimpleNamespace(cursor=lambda: cursor_owner, transaction=lambda: savepoint)
        lease = ConnectionOwner(connection)
        backend = PostgresStateStore.__new__(PostgresStateStore)
        backend._read_connection = lambda: lease
        backend._record_collection_exists_in_cursor = lambda *_: True
        with patch.object(backend, 'record_page', return_value=({'item': {'title': 'PRIVATE-ROW', 'notes': ['discard']}}, None)) as fallback:
            with telemetry.graph_read_timings(self.scope):
                with patch('codex_web.observability.log_event', side_effect=asyncio.CancelledError):
                    result = backend.record_page_projected('PRIVATE-NAMESPACE', fields=('title',))
        self.assertEqual(result, ({'item': {'title': 'PRIVATE-ROW'}}, None))
        fallback.assert_called_once_with('PRIVATE-NAMESPACE', key_prefix=None, after=None, limit=100)
        self.assertIs(savepoint.exits[0][1], failure)
        self.assertIs(lease.exits[0][1], failure)
        self.assertIsNone(telemetry._scope.get())

    def test_actual_projected_page_has_closed_execute_fetch_decode_stages(self):
        from codex_web.storage.state_store import state_record_storage_key
        calls = []
        cursor = SimpleNamespace(execute=lambda *args: calls.append(args),
            fetchall=lambda: [(state_record_storage_key('PRIVATE-NAMESPACE', 'PRIVATE-ID'), '{"title":"PRIVATE-ROW"}')])
        lease = ConnectionOwner(SimpleNamespace(cursor=lambda: ConnectionOwner(cursor), transaction=nullcontext))
        backend = PostgresStateStore.__new__(PostgresStateStore)
        backend._read_connection = lambda: lease
        backend._record_collection_exists_in_cursor = lambda *_: True
        with correlated(correlation_id='graph-read-private'):
            with self.assertLogs(telemetry._logger, level='INFO') as captured:
                with telemetry.graph_read_timings(self.scope):
                    result = backend.record_page_projected('PRIVATE-NAMESPACE', fields=('title',))
        self.assertEqual(result, ({'PRIVATE-ID': {'title': 'PRIVATE-ROW'}}, None))
        self.assertEqual(len(calls), 1)
        rows = [record.structured for record in captured.records]
        self.assertEqual([row['stage'] for row in rows], ['postgres.connection.acquire',
            'postgres.collection.exists', 'postgres.page.execute', 'postgres.page.fetch', 'postgres.page.decode'])
        for record, row in zip(captured.records, rows):
            self.assertEqual(set(row), {'event','correlation_id','stage','outcome','duration_seconds','tenant_id','workspace_id'})
            self.assertEqual(row['event'], 'work_graph.read.stage')
            self.assertEqual(row['correlation_id'], 'graph-read-private')
            self.assertGreaterEqual(row['duration_seconds'], 0)
            self.assertEqual(row['outcome'], 'completed')
            self.assertIsNone(record.exc_info)
            self.assertNotIn('PRIVATE-', JsonFormatter().format(record))
        self.assertEqual(lease.exits[0][:2], (None, None))
        self.assertIsNone(telemetry._scope.get())
        with patch('codex_web.observability.log_event') as emit:
            backend.record_page_projected('PRIVATE-NAMESPACE', fields=('title',))
            emit.assert_not_called()


class ExecutiveWorkGraphTimingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = canonical.ExecutiveManagementTests()
        await self.f.asyncSetUp()
        self.addAsyncCleanup(self.f.asyncTearDown)
        self.graph = WorkGraphService(WorkGraphStore(self.f.service.store.store), lambda: {})
        self.f.service.work_graph = self.graph
        self.payload = ExecutiveActivationCreate(subject='Current priorities', request='Review current project graph.',
            project_id='project-a', requested_role_ids=('cpo',))

    async def test_real_executive_graph_scope_is_inactive_for_other_snapshot_callers(self):
        with self.assertLogs(telemetry._logger, level='INFO') as captured:
            prepared = self.f.service.prepare(self.payload, actor=self.f.actor)
        self.assertEqual(prepared.context.work_graphs[0]['progress']['total'], 0)
        self.assertEqual({record.structured['stage'] for record in captured.records},
                         {'snapshot', 'snapshot.items', 'snapshot.edges'})
        self.assertIsNone(telemetry._scope.get())
        with patch('codex_web.observability.log_event') as emit:
            self.graph.snapshot('project-a', scope=self.f.actor.tenant)
            emit.assert_not_called()

    async def test_real_executive_refusal_resets_scope_without_exception_text(self):
        failure = TimeoutError('PRIVATE-TIMEOUT')
        with patch.object(self.graph, '_project_items', side_effect=failure):
            with self.assertLogs(telemetry._logger, level='INFO') as captured:
                with self.assertRaises(RuntimeError) as caught:
                    self.f.service.prepare(self.payload, actor=self.f.actor)
        self.assertIs(caught.exception.__cause__, failure)
        self.assertEqual({record.structured['stage'] for record in captured.records}, {'snapshot', 'snapshot.items'})
        self.assertTrue(all(record.structured['outcome']=='refused' for record in captured.records))
        self.assertNotIn('PRIVATE-TIMEOUT', json.dumps([r.structured for r in captured.records]))
        self.assertIsNone(telemetry._scope.get())
