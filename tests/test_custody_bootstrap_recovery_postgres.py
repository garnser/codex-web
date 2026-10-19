"""Real PostgreSQL transactions for retained custody/bootstrap publication."""
import os
import unittest
from unittest.mock import patch
from tests.test_recovery_scheduled_postgres import _PostgresFixture
from tests import test_custody_bootstrap_recovery as cases
from tests import test_queued_bootstrap_recovery as reservations


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'isolated PostgreSQL test DSN required')
class PostgresCustodyBootstrapRecoveryTests(_PostgresFixture, cases.CustodyBootstrapRecoveryTests):
    def setUp(self):
        with patch.object(reservations, 'SQLiteStateStore', self.state_store_factory):
            super().setUp()
