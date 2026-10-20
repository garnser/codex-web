"""Canonical Upgrade engine boundary precedes actual isolated index activation."""
import importlib.util
import hashlib
import json
from pathlib import Path
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from tests import test_upgrades as fixtures
from codex_web.action_intents import ActionIntentState
from codex_web.identity import AuthenticationAssurance
from codex_web.services.upgrades import PUBLICATION_EVENT_CONTRACT_HANDLER, UpgradeConflictError
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.upgrades import UpgradePlanCreate, UpgradeStepCreate, UpgradeStepExecute


class PublicationIndexUpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.UpgradeTests();await self.fixture.asyncSetUp()
        self.addAsyncCleanup(self.fixture.asyncTearDown)
        f=self.fixture
        # A preexisting (even empty) document is not a genuinely new database.
        f.state.put('action_intents',ActionIntentState().model_dump(mode='json'))
        self.intents=ActionIntentStore(f.state);self.intents.load()
        f.actions.store=self.intents
        self.service=f.service
        self.service.install_publication_event_contract()
        self.service.service_instance_id='test-instance'
        f.actor=f.actor.model_copy(update={'assurance':AuthenticationAssurance.LOCAL_TRUSTED})
        f.releases.release.build.source_revision='b'*40
        f.recovery.verify_restore=Mock(return_value=NS(status='pass',id='actual-fixture-restore'))

    async def plan(self):
        f=self.fixture
        plan=self.service.create(UpgradePlanCreate(release_id='release-target',
            current_app_version='1.0.0',target_app_version='1.1.0',
            compatibility=f.profile(deployment_mode='local'),observed_control_plane_versions=('1.1.0',),
            require_drain=True,steps=(UpgradeStepCreate(id='publication-index',phase='contract',kind='state_schema',
                handler_id=PUBLICATION_EVENT_CONTRACT_HANDLER,description='Enable verified publication lookup',
                irreversible=True,reversible=False,requires_backup=True,requires_drain=True),)),actor=f.actor)
        plan=self.service.start_service_drain(plan.id,actor=f.actor,service_instance_id='test-instance',
            source_revision='a'*40,target_revision='b'*40)
        plan=self.service.capture_pre_upgrade_backup(plan.id,actor=f.actor)
        plan=self.service.preflight(plan.id,actor=f.actor)
        self.assertTrue(plan.preflight.satisfied,plan.preflight.blockers)
        return await self.service.request_step_approval(plan.id,'publication-index',actor=f.actor)

    async def execute(self,plan):
        return await self.service.execute_step(plan.id,'publication-index',
            UpgradeStepExecute(idempotency_key='once-qualified-contract'),actor=self.fixture.actor)

    async def test_real_engine_records_approval_boundary_before_physical_activation_and_blocks_old_rollback(self):
        f=self.fixture;prior=f.create_plan();plan=await self.plan()
        self.assertEqual(f.state.record_get(self.intents.records_namespace,'meta'),{'schema_version':'1.3'})
        passed=await self.execute(plan)
        self.assertTrue(f.approvals.consumed)
        self.assertTrue(passed.irreversible_boundary_crossed)
        self.assertFalse(passed.rollback_available)
        self.assertFalse(self.service.get(prior.id,actor=f.actor).rollback_available)
        self.assertFalse(self.intents.publication_index_ready())
        self.assertEqual(f.state.record_get(self.intents.records_namespace,'meta'),{'schema_version':'1.3'})
        with self.assertRaisesRegex(UpgradeConflictError,'rollback'):
            self.service.mark_rolled_back(prior.id,actor=f.actor,reason='source only')
        completed=self.service.verify_post_upgrade(plan.id,actor=f.actor)
        self.assertEqual(completed.status,'completed')
        self.assertFalse(completed.maintenance_mode)
        self.assertTrue(self.intents.publication_index_ready())
        self.assertEqual(f.state.record_get(self.intents.records_namespace,'meta'),{'schema_version':'1.4'})
        newer=f.create_plan();self.assertFalse(newer.rollback_available)
        # Byte-exact original reader/writer rejects the actual new metadata.
        path=Path(__file__).parent/'fixtures/publication_index/action_intents_1_3.py'
        provenance=json.loads((path.parent/'provenance.json').read_text())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),provenance['sha256'])
        spec=importlib.util.spec_from_file_location('frozen_action_records_1_3',path)
        old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
        revision=f.state.namespace_revision(self.intents.records_namespace)
        with self.assertRaisesRegex(ValueError,'unsupported action intent records schema'):
            old.ActionIntentStore(f.state).load()
        self.assertEqual(f.state.namespace_revision(self.intents.records_namespace),revision)

    async def test_partial_activation_cannot_pass_or_release_maintenance_and_verify_resumes(self):
        f=self.fixture;plan=await self.plan();await self.execute(plan)
        real=f.state.record_mutate
        def interrupt_after_actual_fence(namespace,keys,update):
            result=real(namespace,keys,update)
            if namespace==self.intents.records_namespace and tuple(keys)==('meta',):
                raise RuntimeError('isolated failure after actual metadata commit before backfill')
            return result
        with patch.object(f.state,'record_mutate',side_effect=interrupt_after_actual_fence):
            failed=self.service.verify_post_upgrade(plan.id,actor=f.actor)
        self.assertEqual(failed.status,'failed')
        self.assertTrue(failed.maintenance_mode)
        self.assertTrue(failed.irreversible_boundary_crossed)
        self.assertFalse(failed.rollback_available)
        self.assertFalse(self.intents.publication_index_ready())
        passed=self.service.verify_post_upgrade(plan.id,actor=f.actor)
        self.assertEqual(passed.status,'completed')
        self.assertTrue(self.intents.publication_index_ready())

    async def test_unapproved_step_stale_pid_and_unverified_backup_never_activate(self):
        f=self.fixture;plan=await self.plan()
        f.approvals.status='pending'
        with self.assertRaises(UpgradeConflictError):await self.execute(plan)
        self.assertFalse(self.intents.publication_index_ready())
        f.approvals.status='approved';self.service.service_instance_id='foreign-instance'
        failed=await self.execute(plan)
        self.assertEqual(failed.steps[0].status,'failed')
        self.assertFalse(failed.irreversible_boundary_crossed)
        self.service.service_instance_id='test-instance';f.recovery.verify_restore.return_value=NS(status='fail',id='bad')
        failed=await self.execute(plan)
        self.assertFalse(failed.irreversible_boundary_crossed)
        self.assertEqual(f.state.record_get(self.intents.records_namespace,'meta'),{'schema_version':'1.3'})


if __name__=='__main__':unittest.main()
