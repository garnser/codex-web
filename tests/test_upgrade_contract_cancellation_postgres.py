"""Real separate-connection qualification of contract baseline cancellation."""
import os
import unittest
from unittest.mock import patch
from tests import test_upgrade_contract_cancellation as tests
from tests.test_recovery_scheduled_postgres import _PostgresFixture


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'isolated PostgreSQL test DSN required')
class PostgresContractCancellationTests(_PostgresFixture, tests.ContractCancellationTests):
    def setUp(self):
        with patch.object(tests, 'SQLiteStateStore', self.state_store_factory):
            super().setUp()
        self.addCleanup(self.state.close)

    def evidence_writer_store(self):
        from psycopg_pool import ConnectionPool
        from codex_web.storage.postgres_state import PostgresStateStore
        from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
        dsn = os.environ['CODEX_WEB_TEST_POSTGRES_DSN']
        pool = ConnectionPool(dsn, min_size=1, max_size=1, timeout=3,
            kwargs={'options': '-c search_path=' + self._test_schema})
        self.addCleanup(pool.close)
        store = PostgresStateStore(dsn, connection_pool=pool)
        self.addCleanup(store.close)
        return ArtifactEvidenceStore(store)
