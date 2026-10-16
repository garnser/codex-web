"""Production-backend operator API composition; isolated schema, real connections only."""
import asyncio
import os
import unittest
from unittest.mock import patch

from tests import test_action_intents as intent_fixtures
from tests import test_recovery_operator_api as api
from tests.test_recovery_scheduled_postgres import _PostgresFixture
from codex_web.storage.recovery import RecoveryStore


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'isolated PostgreSQL test DSN required')
class PostgresRecoveryOperatorTests(_PostgresFixture, api.RecoveryOperatorApiTests):
    async def asyncSetUp(self):
        # Reuse actual service construction, substituting only its StateStore
        # factory. Each test owns its schema/pool; never use a production DSN.
        with patch.object(intent_fixtures, 'SQLiteStateStore', self.state_store_factory):
            await super().asyncSetUp()
        self.addCleanup(self.sqlite.close)

    def writer_store(self):
        from psycopg_pool import ConnectionPool
        from codex_web.storage.postgres_state import PostgresStateStore
        pool = ConnectionPool(os.environ['CODEX_WEB_TEST_POSTGRES_DSN'], min_size=1, max_size=1,
            timeout=3, kwargs={'options': '-c search_path=' + self._test_schema})
        self.addCleanup(pool.close)
        store = PostgresStateStore(os.environ['CODEX_WEB_TEST_POSTGRES_DSN'], connection_pool=pool)
        self.addCleanup(store.close)
        return store

    async def test_separate_late_writer_invalidates_exact_preview_without_overwrite(self):
        async with self.client() as client:
            preview = await client.get(self.url('/reconciliation-preview'))
            self.assertEqual(preview.status_code, 200, preview.text)
            other = RecoveryStore(self.writer_store())
            def late(state):
                row = state.scheduled_firings[self.payload.event_id]
                state.scheduled_firings[row.event_id] = row.model_copy(update={'error_code': 'late-original-writer'})
                return state
            other.update(late)
            current = other.load()
            response = await client.post(self.url(), json=self.body())
            self.assertLess(response.status_code, 500, response.text)
            if response.status_code == 200:
                self.assertNotEqual(response.json()['item']['status'], 'succeeded')
            self.assertEqual(other.load(), current)
            self.assertEqual(current.schema_version, '1.2')
            self.assertFalse(current.reconciliations)

    async def test_separate_schedule_cas_refusal_rolls_back_first_activation(self):
        from codex_web.storage.scheduler import SchedulerStore
        async with self.client() as client:
            other = SchedulerStore(self.writer_store())
            firing = self.recovery.store.load().scheduled_firings[self.payload.event_id]
            other.pause(firing.schedule_id, actor_id=self.actor.identity_id)
            before = self.recovery.store.load()
            response = await client.post(self.url(), json=self.body())
            self.assertLess(response.status_code, 500, response.text)
            if response.status_code == 200:
                self.assertNotEqual(response.json()['item']['status'], 'succeeded')
            # Recovery apply runs before schedule CAS in the shared transaction;
            # refusal must leave no1.3 write or partial domain receipt.
            self.assertEqual(self.recovery.store.load(), before)
            self.assertEqual(before.schema_version, '1.2')

    async def test_concurrent_same_key_requests_use_one_intent_and_one_domain_commit(self):
        async with self.client() as client:
            with patch.object(self.operator.provider.reconcile, 'reconcile', wraps=self.operator.provider.reconcile.reconcile) as engine:
                replies = await asyncio.wait_for(asyncio.gather(
                    client.post(self.url(), json=self.body()),
                    client.post(self.url(), json=self.body())), timeout=15)
            for reply in replies:
                self.assertEqual(reply.status_code, 200, reply.text)
            self.assertEqual(replies[0].json()['item']['id'], replies[1].json()['item']['id'])
            self.assertEqual(engine.call_count, 1)
            self.assertEqual(len(self.recovery.store.load().reconciliations), 1)


from tests import test_recovery_manual_api as manual


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'isolated PostgreSQL test DSN required')
class PostgresManualRecoveryTests(_PostgresFixture, manual.ManualRecoveryApiTests):
    async def asyncSetUp(self):
        with patch.object(intent_fixtures, 'SQLiteStateStore', self.state_store_factory):
            await manual.ManualRecoveryApiTests.asyncSetUp(self)
        self.addCleanup(self.sqlite.close)
