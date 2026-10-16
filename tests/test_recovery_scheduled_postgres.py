"""Real single-connection PostgreSQL qualification of scheduled recovery."""
import os
import unittest
import uuid

from tests import test_recovery_schedule_admission as admission
from tests import test_recovery_scheduled_work as work
from tests import test_recovery_final_settlement as settlement


class _PostgresFixture:
    def state_store_factory(self, path):
        import psycopg
        from psycopg import sql
        from psycopg_pool import ConnectionPool
        from codex_web.storage.postgres_state import PostgresStateStore

        dsn = os.environ['CODEX_WEB_TEST_POSTGRES_DSN']
        schema = 'recovery_schedule_test_' + uuid.uuid4().hex
        self._test_schema = schema
        with psycopg.connect(dsn) as connection:
            connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        def drop():
            with psycopg.connect(dsn) as connection:
                connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        self.addCleanup(drop)
        pool = ConnectionPool(dsn, min_size=1, max_size=1, timeout=3,
                              kwargs={'options': f'-c search_path={schema}'})
        self.addCleanup(pool.close)
        return PostgresStateStore(dsn, connection_pool=pool)


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'isolated PostgreSQL test DSN required')
class PostgresRecoveryAdmissionTests(_PostgresFixture, admission.RecoveryScheduleAdmissionTests):
    pass


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'isolated PostgreSQL test DSN required')
class PostgresRecoveryWorkTests(_PostgresFixture, work.ScheduledRecoveryWorkTests):
    pass


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'isolated PostgreSQL test DSN required')
class PostgresFinalSettlementTests(_PostgresFixture, settlement.ScheduledFinalSettlementTests):
    def writer_store(self):
        from psycopg_pool import ConnectionPool
        from codex_web.storage.postgres_state import PostgresStateStore
        dsn = os.environ['CODEX_WEB_TEST_POSTGRES_DSN']
        pool = ConnectionPool(dsn, min_size=1, max_size=1, timeout=3,
            kwargs={'options': f'-c search_path={self._test_schema}'})
        self.addCleanup(pool.close)
        return PostgresStateStore(dsn, connection_pool=pool)

    async def test_auxiliary_key_consumer_guard(self):
        self.backup_key = self.key
        settlement.FinalSettlementTests.check_auxiliary_consumer_guard(self)

    async def test_noncurrent_required_version_consumer_guard(self):
        self.backup_key = self.key
        settlement.FinalSettlementTests.check_auxiliary_consumer_guard(self, version=True)
