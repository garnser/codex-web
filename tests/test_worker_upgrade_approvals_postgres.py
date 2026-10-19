"""The real composed agent approval path on an isolated PostgreSQL schema."""
import os
import unittest
from unittest.mock import patch
from tests import test_action_intents as intent_fixtures
from tests import test_control_plane_broker as broker_fixtures
from tests import test_worker_upgrade_approvals as agent
from tests.test_recovery_scheduled_postgres import _PostgresFixture


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'isolated PostgreSQL test DSN required')
class WorkerUpgradeApprovalPostgresTests(_PostgresFixture,agent.WorkerUpgradeApprovalTests):
    async def asyncSetUp(self):
        with patch.object(broker_fixtures,'SQLiteStateStore',self.state_store_factory):
            await super().asyncSetUp()
        self.addCleanup(self.sqlite.close)

    async def test_separate_connection_profile_change_before_commit_denies(self):
        from copy import copy
        from tests import test_recovery_operator_postgres as pg
        from codex_web.storage.agent_profiles import AgentProfileStore
        from codex_web.agent_profiles import AgentProfileUpdate
        writer=copy(self.agent_profiles)
        writer.store=AgentProfileStore(pg.PostgresRecoveryOperatorTests.writer_store(self))
        original=self.sqlite.record_mutate_many
        def change(selected,callback):
            if self.upgrades.store.namespace in selected:
                writer.update('developer',AgentProfileUpdate(description='Separate writer revision',
                    reason='Fixture final boundary race'),actor=self.admin)
            return original(selected,callback)
        with patch.object(self.sqlite,'record_mutate_many',side_effect=change):
            status,_,result=await self.post()
        self.assertEqual(status,403,result)
        self.assertFalse(self.approvals.store.load().requests)
        self.assertIsNone(self.upgrades.get(self.plan.id,actor=self.admin).steps[0].approval_request_id)
