"""A verified rollout must precede cancellation of its separate contract phase."""
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from codex_web.artifact_evidence import ArtifactCreate, ArtifactDigest, ArtifactType, EvidenceLifecycle, EvidenceResult
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, MembershipRole, PrincipalKind
from codex_web.releases import ReleaseCreate
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.releases import ReleaseService
from codex_web.services.upgrades import UpgradeService, UpgradeConflictError
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.releases import ReleaseStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.upgrades import UpgradeStore
from codex_web.upgrades import UpgradeCompatibilityProfile, UpgradePlanCreate, UpgradeStepCreate, UpgradeUnexecutedCancellationRequest
from tests.test_upgrades import _Definitions, _Workers, _Extensions, _Recovery, _Actions, _Approvals


class ContractCancellationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = SQLiteStateStore(Path(self.temp.name) / 'state.sqlite3')
        self.actor = AuthenticationActor(identity_id='operator', principal_kind=PrincipalKind.HUMAN,
            organization_id='org', workspace_id='ws', roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.LOCAL_TRUSTED)
        self.evidence = ArtifactEvidenceService(ArtifactEvidenceStore(self.state))
        self.releases = ReleaseService(ReleaseStore(self.state), artifacts=self.evidence,
            resources=None, approvals=_Approvals(), action_intents=_Actions())
        self.target = 'b' * 40
        artifact = self.evidence.create_artifact(ArtifactCreate(name='Actual target', artifact_type=ArtifactType.BUILD,
            revision=self.target, digest=ArtifactDigest(value='c' * 64)), actor=self.actor)
        self.release = self.releases.create(ReleaseCreate(name='Target', version=self.target,
            artifact_id=artifact.id, source_revision=self.target, build_id=self.target), actor=self.actor)
        self.now = 1000.0
        self.service = UpgradeService(UpgradeStore(self.state), state_store=self.state, definitions=_Definitions(),
            workers=_Workers(), extensions=_Extensions(), recovery=_Recovery(), releases=self.releases,
            action_intents=_Actions(), approvals=_Approvals(), evidence=self.evidence, clock=lambda: self.now)
        self.service.service_instance_id = 'controller'
        @contextmanager
        def guard():
            yield self.target
        self.service.cancellation_guard = guard

    def plan(self, *, contract=False):
        steps = (UpgradeStepCreate(id='contract', phase='contract', kind='state_schema',
            handler_id='skill-scan-contract/1.0', description='Enable scanner format',
            irreversible=True, reversible=False, requires_backup=True, requires_drain=True),) if contract else ()
        profile = UpgradeCompatibilityProfile(source_app_version='a'*40, target_app_version=self.target,
            supported_control_plane_versions_during_rollout=('a'*40, self.target),
            source_state_schema_version=1, target_state_schema_version=1,
            target_definition_engine_version='1.0', target_extension_host_version='1.0')
        return self.service.create(UpgradePlanCreate(release_id=self.release.id,
            current_app_version='a'*40, target_app_version=self.target, compatibility=profile,
            observed_control_plane_versions=(self.target,), steps=steps), actor=self.actor)

    def baseline(self):
        previous = self.plan()
        previous = self.service.verify_post_upgrade(previous.id, actor=self.actor)
        self.assertEqual(previous.status, 'completed')
        self.now += 10
        return previous

    def cancel(self, plan):
        return self.service.cancel_unexecuted(plan.id, UpgradeUnexecutedCancellationRequest(
            service_instance_id='controller', running_revision=self.target,
            expected_admission_epoch=plan.admission_epoch, reason='Defer the unexecuted scanner contract for independent review'), actor=self.actor)

    def test_prior_verified_binary_allows_only_separate_unexecuted_contract_cancellation(self):
        previous = self.baseline()
        plan = self.service.start_drain(self.plan(contract=True).id, actor=self.actor)
        other = self.service.start_drain(self.plan(contract=True).id, actor=self.actor)
        result = self.cancel(plan)
        self.assertFalse(result['item']['maintenance_mode'])
        self.assertEqual(result['item']['status'], 'failed')
        self.assertFalse(result['item']['irreversible_boundary_crossed'])
        self.assertEqual(self.service.get(previous.id, actor=self.actor), previous)
        self.assertEqual(self.service.get(other.id, actor=self.actor), other)
        self.assertFalse(self.service.worker_assignment_allowed('org', 'ws'))
        evidence = self.evidence.get_evidence(result['cancellation_evidence_id'], actor=self.actor)
        self.assertEqual(evidence.result, EvidenceResult.FAIL)
        self.assertEqual(evidence.metadata['previous_verified_deployment_id'], previous.id)
        self.assertFalse(evidence.metadata['installation_verified'])
        self.assertFalse(evidence.metadata['rollback_performed'])

    def test_missing_later_or_revoked_baseline_cannot_release_maintenance(self):
        plan = self.service.start_drain(self.plan(contract=True).id, actor=self.actor)
        with self.assertRaisesRegex(UpgradeConflictError, 'already installed'):
            self.cancel(plan)
        self.now += 10
        previous = self.baseline()
        with self.assertRaisesRegex(UpgradeConflictError, 'already installed'):
            self.cancel(plan)
        fresh = self.service.start_drain(self.plan(contract=True).id, actor=self.actor)
        self.evidence.store.update(lambda state: state.model_copy(update={'evidence': [
            e.model_copy(update={'lifecycle': EvidenceLifecycle.INVALIDATED}) if e.id == previous.post_upgrade_evidence_id else e
            for e in state.evidence]}))
        with self.assertRaisesRegex(UpgradeConflictError, 'already installed'):
            self.cancel(fresh)
        self.assertTrue(self.service.get(fresh.id, actor=self.actor).maintenance_mode)

    def test_empty_plan_and_different_build_still_refuse_installed_target(self):
        self.baseline()
        with self.assertRaisesRegex(UpgradeConflictError, 'already installed'):
            self.cancel(self.plan())
        artifact = self.evidence.create_artifact(ArtifactCreate(name='Different build', artifact_type='build',
            revision=self.target, digest=ArtifactDigest(value='d'*64)), actor=self.actor)
        self.release = self.releases.create(ReleaseCreate(name='Different build', version=self.target,
            artifact_id=artifact.id, source_revision=self.target, build_id=self.target), actor=self.actor)
        with self.assertRaisesRegex(UpgradeConflictError, 'already installed'):
            self.cancel(self.plan(contract=True))

    def test_evidence_revocation_between_inspection_and_commit_refuses_atomically(self):
        previous = self.baseline()
        plan = self.service.start_drain(self.plan(contract=True).id, actor=self.actor)
        create = self.evidence.create_evidence
        def revoke_after_attempt(payload, **kwargs):
            record = create(payload, **kwargs)
            self.evidence_writer_store().update(lambda state: state.model_copy(update={'evidence': [
                e.model_copy(update={'result': EvidenceResult.FAIL}) if e.id == previous.post_upgrade_evidence_id else e
                for e in state.evidence]}))
            return record
        self.evidence.create_evidence = revoke_after_attempt
        with self.assertRaisesRegex(UpgradeConflictError, 'baseline changed'):
            self.cancel(plan)
        self.assertEqual(self.service.get(plan.id, actor=self.actor), plan)

    def evidence_writer_store(self):
        return self.evidence.store

    def test_expired_nonpassing_foreign_or_misbound_evidence_cannot_release_contract(self):
        previous = self.baseline()
        original = self.evidence.get_evidence(previous.post_upgrade_evidence_id, actor=self.actor)
        for changes in ({'retention_expires_at': self.now - 1}, {'result': EvidenceResult.FAIL},
                        {'organization_id': 'foreign'}, {'source': 'operator-assertion'},
                        {'metadata': {**original.metadata, 'upgrade_plan_id': 'other-plan'}}):
            with self.subTest(changes=changes):
                plan = self.service.start_drain(self.plan(contract=True).id, actor=self.actor)
                self.evidence.store.update(lambda state: state.model_copy(update={'evidence': [
                    original.model_copy(update=changes) if e.id == original.id else e for e in state.evidence]}))
                with self.assertRaisesRegex(UpgradeConflictError, 'already installed'):
                    self.cancel(plan)
                self.assertEqual(self.service.get(plan.id, actor=self.actor), plan)
