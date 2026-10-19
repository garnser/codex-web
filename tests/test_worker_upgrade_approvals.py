"""Real authenticated broker, Upgrade, identity and Approval services; fixture data only."""
import unittest
from unittest.mock import patch

from codex_web.approval_requests import ApprovalConsumeRequest, ApprovalDecisionSubmit
from codex_web.artifact_evidence import ArtifactCreate
from codex_web.execution_workers import AssignmentCompleteRequest
from codex_web.releases import ReleaseCreate
from codex_web.services.approval_requests import ApprovalRequestService, ApprovalEligibilityError
from codex_web.services.releases import ReleaseService
from codex_web.services.upgrades import UpgradeService
from codex_web.storage.approval_requests import ApprovalRequestStore
from codex_web.storage.releases import ReleaseStore
from codex_web.storage.upgrades import UpgradeStore
from codex_web.upgrades import UpgradeCompatibilityProfile, UpgradePlanCreate, UpgradeStepCreate
from tests import test_worker_verifications as fixtures
from tests.test_upgrades import _Extensions


class WorkerUpgradeApprovalTests(unittest.IsolatedAsyncioTestCase):
    _assignment = fixtures.WorkerVerificationTests.__dict__['_assignment']
    _publish_worker_authority = fixtures.WorkerVerificationTests._publish_worker_authority
    _request = fixtures.WorkerVerificationTests._request
    _publish_profile = fixtures.WorkerVerificationTests._publish_profile
    verification_resources = fixtures.WorkerVerificationTests.verification_resources
    _stop_services = fixtures.WorkerVerificationTests._stop_services

    async def asyncSetUp(self):
        await fixtures.WorkerVerificationTests.asyncSetUp(self)
        self.approvals = ApprovalRequestService(ApprovalRequestStore(self.sqlite),
            self.identity, self.scheduler.canonical_events, scheduler=self.scheduler)
        self.releases = ReleaseService(ReleaseStore(self.sqlite), artifacts=self.evidence,
            resources=self.resources, approvals=self.approvals, action_intents=self.intents)
        artifact = self.evidence.create_artifact(ArtifactCreate(name='Fixture build',
            artifact_type='build',project_id='project-a',revision='b'*40,
            digest={'algorithm':'sha256','value':'c'*64}),actor=self.admin)
        release = self.releases.create(ReleaseCreate(project_id='project-a',name='Fixture release',
            version='b'*40,artifact_id=artifact.id,source_revision='b'*40,build_id='b'*40),actor=self.admin)
        self.upgrades = UpgradeService(UpgradeStore(self.sqlite),state_store=self.sqlite,
            definitions=self.registry,workers=self.workers,extensions=_Extensions(),recovery=self.recovery,
            releases=self.releases,action_intents=self.intents,approvals=self.approvals,
            evidence=self.evidence,identity=self.identity)
        self.service.upgrades = self.upgrades
        self.plan = self.upgrades.create(UpgradePlanCreate(project_id='project-a',release_id=release.id,
            current_app_version='a'*40,target_app_version='b'*40,
            compatibility=UpgradeCompatibilityProfile(source_app_version='a'*40,target_app_version='b'*40,
                supported_control_plane_versions_during_rollout=('a'*40,'b'*40),
                source_state_schema_version=1,target_state_schema_version=1,
                target_definition_engine_version='1.0',target_extension_host_version='1.0'),
            observed_control_plane_versions=('a'*40,),steps=(UpgradeStepCreate(id='contract',phase='contract',
                kind='state_schema',handler_id='fixture/1.0',description='Fixture boundary',irreversible=True,
                reversible=False,requires_backup=True),),require_recovery_qualification=False),actor=self.admin)
        self.plan=self.upgrades.capture_pre_upgrade_backup(self.plan.id,actor=self.admin)
        self.target_binding=self.upgrades._approval_target(self.plan,self.plan.steps[0],actor=self.admin)
        self.payload={'plan_id':self.plan.id,'step_id':'contract',
            'expected_target_fingerprint':self.target_binding.fingerprint()}

    async def post(self, payload=None):
        return await self._request('POST','/api/agent-upgrade-approvals',payload=payload or self.payload)

    async def test_machine_request_single_human_decision_and_exact_consumption(self):
        status,_,result=await self.post()
        self.assertEqual(status,200,result)
        request=result['approval_request']
        self.assertEqual(request['requester_identity_id'],self.worker_actor.identity_id)
        self.assertEqual(request['requester_principal_kind'],'service')
        self.assertEqual(result['original_authorizer'],self.admin.identity_id)
        self.assertFalse(result['step_executed'])
        self.assertTrue(request['audit_refs'])
        self.assertFalse(request['requirement']['allow_self_approval'])
        decided=await self.approvals.decide(request['id'],ApprovalDecisionSubmit(outcome='approve',idempotency_key='fixture-decision'),actor=self.admin)
        self.assertEqual(decided.status,'approved')
        consumed=await self.approvals.consume(request['id'],ApprovalConsumeRequest(target=self.target_binding,
            idempotency_key='fixture-consumption',resulting_operation_reference='fixture-boundary'),actor=self.admin)
        self.assertEqual(consumed.status,'consumed')
        self.assertEqual(self.upgrades.get(self.plan.id,actor=self.admin).steps[0].approval_request_id,request['id'])

    async def test_repeated_request_observes_same_record_without_competing_approval(self):
        one=await self.post();two=await self.post()
        self.assertEqual(one[0],200,one[2]);self.assertEqual(two[0],200,two[2])
        self.assertEqual(one[2]['approval_request']['id'],two[2]['approval_request']['id'])
        self.assertEqual(len(self.approvals.store.load().requests),1)

    async def test_human_request_is_not_relabelled_and_self_approval_still_refused(self):
        plan=await self.upgrades.request_step_approval(self.plan.id,'contract',actor=self.admin)
        status,_,result=await self.post()
        self.assertEqual(status,403,result)
        with self.assertRaises(ApprovalEligibilityError):
            await self.approvals.decide(plan.steps[0].approval_request_id,ApprovalDecisionSubmit(outcome='approve',idempotency_key='fixture-decision'),actor=self.admin)
        self.assertEqual(len(self.approvals.store.load().requests),1)

    async def test_identity_override_and_target_drift_refused(self):
        for extra in ({'requester_identity_id':self.admin.identity_id},
                      {'expected_target_fingerprint':'0'*64}):
            status,_,result=await self.post({**self.payload,**extra})
            self.assertEqual(status,403,result)
        self.assertFalse(self.approvals.store.load().requests)

    async def test_workspace_lease_subject_and_tenant_must_match(self):
        store = self.service.execution_workspaces.store
        original = store.lease
        for changes in ({'organization_id': 'foreign-org'}, {'subject': None}):
            with patch.object(store, 'lease', side_effect=lambda key: original(key).model_copy(update=changes)):
                status, _, result = await self.post()
            self.assertEqual(status, 403, result)
        self.assertFalse(self.approvals.store.load().requests)
        self.assertIsNone(self.upgrades.get(self.plan.id, actor=self.admin).steps[0].approval_request_id)

    async def test_final_assignment_revocation_rolls_back_request_and_attachment(self):
        original=self.sqlite.record_mutate_many
        def revoke(selected, callback):
            if self.upgrades.store.namespace in selected:
                self.workers.complete(self.worker.id,self.assignment.id,AssignmentCompleteRequest(
                    fence=self.assignment.fence,lease_token=self.assignment.lease.lease_token,
                    succeeded=False,failure_code='cancelled',failure_message='Fixture revocation'),actor=self.worker_actor)
            return original(selected,callback)
        with patch.object(self.sqlite,'record_mutate_many',side_effect=revoke):
            status,_,result=await self.post()
        self.assertEqual(status,403,result)
        self.assertFalse(self.approvals.store.load().requests)
        self.assertIsNone(self.upgrades.get(self.plan.id,actor=self.admin).steps[0].approval_request_id)

    async def test_foreign_project_and_attempted_step_refused(self):
        for changes in ({'project_id':'foreign-project'},
                        {'steps':(self.plan.steps[0].model_copy(update={'attempts':1}),)}):
            self.upgrades._update(self.plan.id,self.admin,lambda p:p.model_copy(update=changes))
            status,_,result=await self.post()
            self.assertEqual(status,403,result)
            self.upgrades._update(self.plan.id,self.admin,lambda _:self.plan)
        self.assertFalse(self.approvals.store.load().requests)

    async def test_concurrent_plan_change_at_commit_creates_no_orphan(self):
        original=self.sqlite.record_mutate_many
        def change(selected, callback):
            if self.upgrades.store.namespace in selected:
                self.upgrades._update(self.plan.id,self.admin,
                    lambda p:p.model_copy(update={'updated_at':p.updated_at+1}))
            return original(selected,callback)
        with patch.object(self.sqlite,'record_mutate_many',side_effect=change):
            status,_,result=await self.post()
        self.assertEqual(status,403,result)
        self.assertFalse(self.approvals.store.load().requests)
        self.assertIsNone(self.upgrades.get(self.plan.id,actor=self.admin).steps[0].approval_request_id)

    async def test_lost_response_after_atomic_attachment_is_observed_on_repeat(self):
        with patch.object(self.approvals,'_emit',side_effect=RuntimeError('Fixture lost response')):
            status,_,_=await self.post()
        self.assertEqual(status,500)
        attached=self.upgrades.get(self.plan.id,actor=self.admin).steps[0].approval_request_id
        self.assertIsNotNone(attached)
        status,_,body=await self.post()
        self.assertEqual(status,200,body)
        self.assertEqual(body['approval_request']['id'],attached)
        self.assertEqual(len(self.approvals.store.load().requests),1)
