import unittest
from types import SimpleNamespace as N
from unittest.mock import Mock

import httpx
from fastapi import FastAPI

from codex_web.api.upgrades import build_upgrades_router
from codex_web.artifact_evidence import EvidenceLifecycle, EvidenceResult, EvidenceType
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, MembershipRole, PrincipalKind
from codex_web.services.local_installation_inspector import LocalInstallationInspector
from codex_web.services.upgrades import UpgradeError
from codex_web.upgrades import UpgradeStatus


class LocalInstallationInspectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.actor = AuthenticationActor(identity_id='admin', principal_kind=PrincipalKind.HUMAN,
            organization_id='org', workspace_id='ws', roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA)
        self.plan = N(id='plan', release_id='release', current_app_version='a'*40,
            target_app_version='b'*40, service_instance_id='instance', status=UpgradeStatus.READY,
            maintenance_mode=True, post_upgrade_evidence_id=None, preflight=None,
            admission_epoch=1, completed_at=None, irreversible_boundary_crossed=False,
            definition_migrations=(), steps=(), compatibility=N(deployment_mode=N(value='local')))
        self.service = Mock()
        self.service.service_instance_id = 'current-instance'
        self.service.get.return_value = self.plan
        self.service.releases.get.return_value = N(id='release', build=N(artifact_id='artifact'))
        self.service.evidence.list_evidence.return_value = []
        self.inspector = LocalInstallationInspector(self.service)

    def report(self, **changes):
        metadata = dict(upgrade_plan_id='plan', installer_report_version=1,
            phase='objects_verified', source_revision='a'*40, target_revision='b'*40,
            service_instance_id='instance', archive_sha256='c'*64, fetched=True,
            repository_source='https://github.com/org/repo', raw_log='never-show-this-secret')
        metadata.update(changes)
        return N(id='reported', metadata=metadata, provider='local-installer', source='plan',
            producer_identity_id='reporting-actor', observed_at=100, artifact_ids=('artifact',))

    def test_cancellation_evidence_does_not_claim_installation(self):
        self.plan.status = UpgradeStatus.FAILED
        self.plan.completed_at = 100
        self.plan.maintenance_mode = False
        self.plan.admission_epoch = 2
        cancellation = N(id='cancel',provider='codex-web',source='upgrade-unexecuted-cancellation',
            evidence_type=EvidenceType.POLICY_EVALUATION,result=EvidenceResult.FAIL,
            metadata={'upgrade_plan_id':'plan','source_app_version':'a'*40,
                      'target_app_version':'b'*40,'admission_epoch':1,'reason':'Superseded before installation'})
        self.service.evidence.list_evidence.return_value = [cancellation]
        result = self.inspector.inspect('plan', actor=self.actor)
        self.assertFalse(result['installation_verified'])
        self.assertFalse(result['cancellation_request_available'])
        self.assertEqual(result['cancellation']['href'],'/api/evidence/cancel')
        cancellation.metadata['admission_epoch'] = 0
        self.assertIsNone(self.inspector.inspect('plan', actor=self.actor)['cancellation'])

    def test_reported_success_never_completes_canonical_verification(self):
        self.service.evidence.list_evidence.return_value = [self.report(phase='installed')]
        result = self.inspector.inspect('plan', actor=self.actor)
        self.assertFalse(result['installation_verified'])
        self.assertFalse(result['diagnostics_grant_authority'])
        self.assertEqual(result['process']['status'], 'unknown')
        self.assertTrue(result['reported_diagnostics'][0]['reported_only'])
        self.assertNotIn('never-show-this-secret', str(result))

    def test_blocked_scanner_cancellation_has_separate_label_and_failed_step_reference(self):
        self.plan.status = UpgradeStatus.FAILED
        self.plan.steps = (N(attempts=1),)
        self.service._blocked_scan_contract_proof.return_value = 'engine-failure'
        result = self.inspector.inspect('plan',actor=self.actor)
        self.assertTrue(result['cancellation_request_available'])
        self.assertEqual(result['cancellation_kind'],'blocked_scanner_activation')
        self.assertEqual(result['blocked_scan_contract_evidence_href'],'/api/evidence/engine-failure')
        self.service._blocked_scan_contract_proof.return_value = None
        self.assertFalse(self.inspector.inspect('plan',actor=self.actor)['cancellation_request_available'])

    def test_completed_plan_requires_correct_valid_post_evidence(self):
        self.plan.status = UpgradeStatus.COMPLETED
        self.assertFalse(self.inspector.inspect('plan', actor=self.actor)['installation_verified'])
        self.plan.post_upgrade_evidence_id = 'verified'
        evidence = N(id='verified', lifecycle=EvidenceLifecycle.VALID,
            evidence_type=EvidenceType.POLICY_EVALUATION,
            result=EvidenceResult.PASS, provider='codex-web', source='upgrade-post-verification',
            metadata={'upgrade_plan_id':'plan','source_app_version':'a'*40,'target_app_version':'b'*40})
        self.service.evidence.get_evidence.return_value = evidence
        self.assertTrue(self.inspector.inspect('plan', actor=self.actor)['installation_verified'])
        evidence.metadata['target_app_version'] = 'd'*40
        self.assertFalse(self.inspector.inspect('plan', actor=self.actor)['installation_verified'])
        evidence.metadata['target_app_version'] = 'b'*40
        evidence.lifecycle = EvidenceLifecycle.INVALIDATED
        self.assertFalse(self.inspector.inspect('plan', actor=self.actor)['installation_verified'])

    def test_reports_are_filtered_sanitized_and_stale_instances_are_visible(self):
        self.service.evidence.list_evidence.return_value = [self.report(upgrade_plan_id='foreign'),
            self.report(target_revision='d'*40), self.report(service_instance_id='old',
                repository_source='https://user:secret@example.com/repo',
                failure_code='fetch_failed', phase='failed')]
        result = self.inspector.inspect('plan', actor=self.actor)
        self.assertEqual(len(result['reported_diagnostics']), 1)
        report = result['reported_diagnostics'][0]
        self.assertTrue(report['stale_instance'])
        self.assertEqual(report['failure_code'], 'fetch_failed')
        self.assertNotIn('repository_source', report)
        self.assertNotIn('secret', str(result))

    def test_process_handles_are_observed_without_trusting_reported_pids(self):
        from codex_web.services.control_plane_broker import ControlPlaneBrokerService
        broker=ControlPlaneBrokerService.__new__(ControlPlaneBrokerService)
        broker.service_instance_id='instance'
        broker._local_installers={}
        self.plan.deployment_source_revision='a'*40
        self.plan.deployment_target_revision='b'*40
        self.assertEqual(broker.local_installation_process(self.plan)['status'],'unknown')
        process=Mock(pid=42);process.poll.return_value=None
        broker._local_installers['plan']=(process,'instance','a'*40,'b'*40)
        self.assertEqual(broker.local_installation_process(self.plan)['status'],'running')
        process.poll.return_value=0
        self.assertEqual(broker.local_installation_process(self.plan)['status'],'exited')
        self.plan.service_instance_id='stale'
        self.assertEqual(broker.local_installation_process(self.plan)['status'],'unknown')

    async def get(self):
        app = FastAPI()
        @app.middleware('http')
        async def identity(request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)
        app.include_router(build_upgrades_router(self.service))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            return await client.get('/api/upgrades/plan/local-installation')

    async def test_api_requires_admin_mfa_or_upgrade_admin_service(self):
        self.assertEqual((await self.get()).status_code, 200)
        for change in [{'roles':(MembershipRole.MEMBER,)},
                       {'assurance':AuthenticationAssurance.PRIMARY},
                       {'principal_kind':PrincipalKind.SERVICE,'service_scopes':('upgrade:read',)}]:
            original=self.actor
            self.actor=self.actor.model_copy(update=change)
            self.assertEqual((await self.get()).status_code, 403)
            self.actor=original

    async def test_foreign_plan_is_hidden_before_reading_reports(self):
        self.service.get.side_effect=UpgradeError('upgrade plan not found')
        self.assertEqual((await self.get()).status_code, 404)
        self.service.evidence.list_evidence.assert_not_called()
