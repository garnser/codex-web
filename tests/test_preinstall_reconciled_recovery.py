"""Private SQLite canonical custody/Evidence/capture integration; no application."""
import copy
import unittest
from unittest.mock import patch

from tests import test_recovery_reconciliation as recovery_fixture
from tests import test_upgrades as upgrade_fixture
from codex_web.preinstall_backup import PreinstallBackupRuntime, qualify_capture
from codex_web.services.recovery_reconciliation import RecoveryReconciliationService
from codex_web.services.upgrades import UpgradePreflightError, UpgradeService
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.upgrades import UpgradeStore
from codex_web.upgrades import UpgradeDeploymentMode, UpgradePlanCreate


class ReconciledRecoveryCaptureTests(unittest.IsolatedAsyncioTestCase):
    setUp = recovery_fixture.RecoveryReconciliationTests.setUp
    tearDown = recovery_fixture.RecoveryReconciliationTests.tearDown
    asyncTearDown = recovery_fixture.RecoveryReconciliationTests.asyncTearDown
    orphan = recovery_fixture.RecoveryReconciliationTests.orphan
    proof = recovery_fixture.RecoveryReconciliationTests.proof

    async def prepare(self):
        firing = await self.orphan(failed=True)
        request = self.proof(firing, outcome='unknown')
        RecoveryReconciliationService(self.service, lambda _: self.actor).reconcile(
            request, actor=self.actor)
        self.clock.value += 1
        older = self.service.create_backup(actor=self.actor)
        self.clock.value += 1
        self.service.verify_restore(older.id, actor=self.actor, publish_evidence=True)
        self.assertTrue(self.service.health(actor=self.actor).recovery_qualified)
        self.clock.value += 1
        latest = self.service.create_backup(actor=self.actor)
        health = self.service.health(actor=self.actor)
        self.assertEqual(health.latest_backup_id, latest.id)
        self.assertFalse(health.recovery_qualified)
        self.assertEqual(health.blockers, ('scheduled_recovery_requires_reconciliation',))
        self.service.start()  # Actual stopped fixture executor, never a remote-owner inference.
        releases = upgrade_fixture._Releases()
        releases.release.build.source_revision = 'b' * 40
        self.upgrades = UpgradeService(UpgradeStore(self.state), state_store=self.state,
            definitions=upgrade_fixture._Definitions(), workers=upgrade_fixture._Workers(),
            extensions=upgrade_fixture._Extensions(), recovery=self.service,
            releases=releases, action_intents=upgrade_fixture._Actions(),
            approvals=upgrade_fixture._Approvals(), evidence=self.evidence, clock=self.clock)
        self.upgrades.service_instance_id = 'installed-fixture'
        profile = upgrade_fixture.UpgradeTests.profile(self,
            deployment_mode=UpgradeDeploymentMode.LOCAL)
        plan = self.upgrades.create(UpgradePlanCreate(release_id='release-target',
            current_app_version='1.0.0', target_app_version='1.1.0', compatibility=profile,
            observed_control_plane_versions=('1.0.0', '1.1.0'), require_drain=True,
            require_recovery_qualification=True, steps=()), actor=self.actor)
        self.upgrades.start_service_drain(plan.id, actor=self.actor,
            service_instance_id='installed-fixture', source_revision='a' * 40,
            target_revision='b' * 40)
        self.plan = self.upgrades.preflight(plan.id, actor=self.actor)
        self.assertEqual(set(self.plan.preflight.blockers), {
            'recovery_not_qualified', 'recovery:scheduled_recovery_requires_reconciliation'})
        return firing, request

    def qualify(self):
        return qualify_capture(self.upgrades, self.plan, actor=self.actor,
            source_revision='a' * 40, target_revision='b' * 40,
            service_instance_id='installed-fixture', now=self.clock())

    def runtime(self):
        self.store = IdentityStateStore(self.state)
        self.events = []
        return PreinstallBackupRuntime(self.upgrades, self, lambda: self.actor,
            lambda: self.upgrades.preflight(self.plan.id, actor=self.actor),
            lambda: None, lambda name, data: self.events.append((name, data)),
            'a' * 40, 'b' * 40, 'installed-fixture', self.clock)

    async def test_real_reconciled_unknown_capture_does_not_qualify_deployment_until_matching_restore(self):
        firing, _ = await self.prepare()
        original = copy.deepcopy(self.service.store.load().scheduled_firings[firing.event_id])
        self.qualify()
        with self.assertRaises(UpgradePreflightError):
            self.upgrades.qualify_local_deployment(self.plan.id, actor=self.actor,
                source_revision='a' * 40, target_revision='b' * 40,
                service_instance_id='installed-fixture',
                preflight_evidence_id=self.plan.preflight.evidence_id)
        self.clock.value += 1
        runtime = self.runtime()
        result = await runtime.capture_once()
        self.assertIsNotNone(result.pre_upgrade_backup_id)
        self.assertEqual([name for name, _ in self.events],
            ['capture-started', 'manifest-created', 'associated'])
        self.assertFalse(self.service.health(actor=self.actor).recovery_qualified)
        self.assertEqual(self.service.store.load().scheduled_firings[firing.event_id], original)
        self.clock.value += 1
        verification = self.service.verify_restore(result.pre_upgrade_backup_id,
            actor=self.actor, publish_evidence=True)
        self.assertEqual(verification.status.value, 'pass')
        self.assertTrue(self.service.health(actor=self.actor).recovery_qualified)

    async def test_current_revoked_expired_invalid_unfenced_and_nonindependent_proof_denies(self):
        _, request = await self.prepare()
        baseline = copy.deepcopy(self.evidence.store.load())
        def change_evidence(**fields):
            def update(state):
                state.evidence = [e.model_copy(update=fields) if e.id == request.evidence_id
                                  else e for e in state.evidence]
                return state
            self.evidence.store.update(update)
        for scenario in ('revoked', 'expired', 'unfenced', 'wrong_effect', 'foreign_evidence', 'nonindependent'):
            with self.subTest(scenario=scenario):
                self.evidence.store.update(lambda _: copy.deepcopy(baseline))
                if scenario == 'revoked':
                    self.evidence.invalidate_evidence(request.evidence_id, actor=self.actor,
                        reason='private fixture current revocation')
                elif scenario == 'expired':
                    change_evidence(retention_expires_at=self.clock() - 1)
                elif scenario == 'foreign_evidence':
                    change_evidence(organization_id='foreign-organization')
                elif scenario in ('unfenced', 'wrong_effect'):
                    evidence = next(e for e in baseline.evidence if e.id == request.evidence_id)
                    change_evidence(metadata={**evidence.metadata,
                        **({'original_owner_fenced': False} if scenario == 'unfenced'
                           else {'effect_id': 'foreign-effect'})})
                else:
                    def not_independent(state):
                        state.verifications = [v.model_copy(update={'independent': False})
                            if v.id == request.verification_id else v for v in state.verifications]
                        return state
                    self.evidence.store.update(not_independent)
                before = set(self.service.store.load().backups)
                with self.assertRaises(UpgradePreflightError):
                    self.qualify()  # Same retained installed preflight; CURRENT proof still checked.
                self.assertEqual(set(self.service.store.load().backups), before)
                self.assertFalse(self.service.health(actor=self.actor).recovery_qualified)

    async def test_unsettled_unknown_running_owner_and_absent_proof_remain_blocked(self):
        firing, request = await self.prepare()
        baseline = copy.deepcopy(self.service.store.load())
        evidence = copy.deepcopy(self.evidence.store.load())
        for scenario in ('unsettled', 'running', 'missing_evidence'):
            with self.subTest(scenario=scenario):
                self.service.store.update(lambda _: copy.deepcopy(baseline))
                self.evidence.store.update(lambda _: copy.deepcopy(evidence))
                if scenario == 'unsettled':
                    self.service.store.update(lambda state: state.model_copy(update={'reconciliations': {}}))
                elif scenario == 'running':
                    def running(state):
                        state.scheduled_firings[firing.event_id] = firing
                        return state
                    self.service.store.update(running)
                else:
                    self.evidence.store.update(lambda state: state.model_copy(update={
                        'evidence': [e for e in state.evidence if e.id != request.evidence_id]}))
                with self.assertRaises(UpgradePreflightError):
                    self.qualify()

    async def test_capture_rejects_unreleased_or_foreign_record_without_pending_unknown(self):
        firing, _ = await self.prepare()
        baseline = copy.deepcopy(self.service.store.load())
        for scenario in ('not_released', 'foreign_record'):
            with self.subTest(scenario=scenario):
                state = copy.deepcopy(baseline)
                # Fault-injected decoded snapshot: Literal[True] normally also
                # rejects this at storage decoding. The capture consumer still
                # requires explicit release for ALL records, not just UNKNOWNs.
                state.scheduled_firings[firing.event_id] = firing.model_copy(update={
                    'status': 'succeeded'})
                record = state.reconciliations[firing.event_id]
                if scenario == 'not_released':
                    record = record.model_copy(update={'custody_released': False})
                else:
                    record = record.model_copy(update={'original': record.original.model_copy(
                        update={'organization_id': 'foreign-organization'})})
                state.reconciliations[firing.event_id] = record
                with patch.object(self.service.store, 'load', return_value=state):
                    with self.assertRaises(UpgradePreflightError):
                        self.qualify()

    async def test_postcapture_proof_revocation_preserves_manifest_without_association(self):
        _, request = await self.prepare()
        runtime = self.runtime()
        original = self.service.create_backup
        captured = []
        def revoke_after_capture(**kwargs):
            manifest = original(**kwargs)
            captured.append(manifest.id)
            self.evidence.invalidate_evidence(request.evidence_id, actor=self.actor,
                reason='private fixture revocation after genuine capture')
            return manifest
        with patch.object(self.service, 'create_backup', side_effect=revoke_after_capture):
            with self.assertRaises(UpgradePreflightError):
                await runtime.capture_once()
        self.assertEqual(len(captured), 1)
        self.assertIn(captured[0], self.service.store.load().backups)
        self.assertIsNone(self.upgrades.get(self.plan.id, actor=self.actor).pre_upgrade_backup_id)
        self.assertEqual([name for name, _ in self.events], ['capture-started', 'manifest-created'])
