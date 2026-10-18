"""Real PostgreSQL canonical context and binding fence transactions."""
import os
import unittest
from unittest.mock import patch
from tests import test_turn_execution_binding as fixtures
from tests import test_read_only_resource_context as context_fixture
from tests.test_recovery_scheduled_postgres import _PostgresFixture


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'),'isolated PostgreSQL DSN required')
class PostgresReadOnlyResourceContextTests(_PostgresFixture,context_fixture.ReadOnlyResourceContextTests):
    def setUp(self):
        with patch.object(fixtures,'SQLiteStateStore',self.state_store_factory):
            super().setUp()
