"""Run real recovery/control transactions against the isolated PostgreSQL CI schema.

Reuse the existing real database factory; storage/locks are never mocked.
Absent local test DSN is explicitly unqualified, not a PostgreSQL PASS.
"""
import os
import unittest
from tests import test_project_delegation_recovery as recovery
from tests import test_agent_session_custody_postgres as postgres


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'isolated PostgreSQL test DSN required')
class PostgresProjectDelegationRecoveryTests(recovery.ProjectDelegationRecoveryTests):
    state_store_factory = postgres.PostgresSessionCustodyTests.state_store_factory
