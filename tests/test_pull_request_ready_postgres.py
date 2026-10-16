"""Run readiness admission and settlement against real PostgreSQL transactions."""
import os
import asyncio
import threading
import unittest
from unittest.mock import patch

from tests import test_pull_request_ready as ready
from tests import test_autonomy_append_scaling as postgres_fixtures


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'isolated PostgreSQL test DSN required')
class PostgresPullRequestReadyTests(ready.PullRequestReadyTests):
    # Inherits the configured SecurityBoundaryService ALLOW-vs-DENY races,
    # normal/lost-response no-replay checks, and exact admitted-snapshot guards.
    # They execute real PostgreSQL intent-row writes, not SQLite substitutes.
    postgres_pool_max_size = 1

    async def asyncSetUp(self):
        # The shared fixture owns a random schema and a one-connection pool.
        # Settlement reads must retain their transaction's connection; the
        # independent writer below separately verifies namespace-lock custody.
        postgres_fixtures.PostgresAutonomyAppendTests.setUp(self)
        with patch('tests.test_control_plane_broker.SQLiteStateStore', return_value=self.backend):
            await super().asyncSetUp()

    async def test_independent_binding_writer_waits_for_verification_transaction(self):
        from psycopg_pool import ConnectionPool
        from codex_web.storage.postgres_state import PostgresStateStore
        from codex_web.storage.action_providers import ActionProviderStateStore
        from codex_web.services.action_providers import ActionProviderRegistry

        entered, release, writer_entered = (threading.Event() for _ in range(3))
        writer_pid = []
        binding_namespace = self.intents.execution.registry.store.namespace

        class ObservedStore(PostgresStateStore):
            @staticmethod
            def _lock(cursor, namespace):
                if namespace == binding_namespace:
                    cursor.execute('SELECT pg_backend_pid()')
                    writer_pid.append(cursor.fetchone()[0])
                    writer_entered.set()
                PostgresStateStore._lock(cursor, namespace)

        with self.backend._connection() as connection:
            schema = connection.execute('SELECT current_schema()').fetchone()[0]
        pool = ConnectionPool(self.backend.dsn, min_size=1, max_size=2,
                              kwargs={'options': f'-c search_path={schema}'})
        self.addCleanup(pool.close)
        peer = ObservedStore(self.backend.dsn, connection_pool=pool)
        registry = ActionProviderRegistry(ActionProviderStateStore(peer))
        append = self.intents._append_verification

        def gated_append(*args, **kwargs):
            if kwargs['verified']:
                actual_guard = kwargs['guarded']['guard']
                def gated_guard(documents):
                    entered.set()
                    if not release.wait(10):
                        raise AssertionError('verification writer gate was not released')
                    actual_guard(documents)
                kwargs['guarded'] = {**kwargs['guarded'], 'guard': gated_guard}
            return append(*args, **kwargs)

        def waiting():
            with peer._connection() as connection:
                return connection.execute('SELECT wait_event FROM pg_stat_activity WHERE pid = %s',
                                          (writer_pid[-1],)).fetchone()[0] == 'advisory'

        writer = None
        with patch.object(self.intents, '_append_verification', gated_append):
            operation = asyncio.create_task(self.submit('independent-binding-writer'))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 10))
                writer = asyncio.create_task(asyncio.to_thread(registry.set_enabled,
                    self.binding.id, False, actor=self.admin))
                self.assertTrue(await asyncio.to_thread(writer_entered.wait, 10))
                async with asyncio.timeout(10):
                    while not await asyncio.to_thread(waiting):
                        pass
                self.assertFalse(writer.done())
            finally:
                release.set()
                if writer is not None:
                    await writer
                status, _, body = await operation
        self.assertEqual(status, 200, body)
        self.assertEqual(body['item']['status'], 'requires_reconciliation', body)
        history = self.intents.history(body['item']['id'], self.admin)
        self.assertTrue(history['verifications'][-1]['verified'])
        self.assertEqual(sum(method == 'POST' for method, _ in self.calls), 1)
