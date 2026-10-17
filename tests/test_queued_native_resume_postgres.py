"""Same resume/CAS contract on independent real PostgreSQL connections."""
import asyncio
import os
import unittest
from unittest.mock import patch

from tests import test_queued_native_start as fixtures
from tests.test_queued_native_resume import ResumeCases
from tests.test_recovery_scheduled_postgres import _PostgresFixture
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from tests.test_execution_preflight import actor


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'isolated PostgreSQL DSN required')
class PostgresQueuedNativeResumeTests(_PostgresFixture, ResumeCases, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with patch.object(fixtures, 'SQLiteStateStore', self.state_store_factory):
            super().setUp()

    async def test_separate_connection_duplicate_reconciliation_is_same_fact(self):
        c=self.prepare();self.capture.observe(c,generation='generation',message=self.negative())
        from psycopg_pool import ConnectionPool
        from codex_web.storage.postgres_state import PostgresStateStore
        pool=ConnectionPool(os.environ['CODEX_WEB_TEST_POSTGRES_DSN'],min_size=1,max_size=1,
            timeout=3,kwargs={'options':f'-c search_path={self._test_schema}'})
        self.addCleanup(pool.close)
        other_store=PostgresStateStore(os.environ['CODEX_WEB_TEST_POSTGRES_DSN'],connection_pool=pool)
        self.addCleanup(other_store.close)
        self.assertIsNot(other_store._pool,self.service.store.store._pool)
        with other_store._read_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute('SELECT current_schema()')
                self.assertEqual(cursor.fetchone()[0],self._test_schema)
        other=ExecutionPreflightService(ExecutionPreflightStore(other_store))
        results=await asyncio.gather(*[asyncio.to_thread(service.reconcile_prestart_refusal,
            self.f.attempt.id,actor=actor(admin=True)) for service in [self.service,other]])
        self.assertTrue(all(x.status=='blocked' for x in results))
        self.assertEqual(results[0].queued_resume_evidence,results[1].queued_resume_evidence)
        self.assertEqual(results[0].queued_turn_snapshot,results[1].queued_turn_snapshot)
        self.assertIsNone(self.saved().queued_start_receipt)
        self.runtime._send.assert_not_called()
