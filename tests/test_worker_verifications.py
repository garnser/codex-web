"""Synthetic, private-free composition of the existing assignment and Evidence services.

Only workspace transport is fake. Authority, identity, profiles, worker leases,
Evidence, verification independence and transactional persistence are canonical.
"""
import hashlib
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

from codex_web.authority import AUTHORITY_ROLE_CATALOG_ID, AUTHORITY_ROLE_CATALOG_KIND
from codex_web.agent_profiles import AgentProfileCreate, AgentProfileExecutionBinding, AgentProfileUpdate
from codex_web.artifact_evidence import ArtifactCreate, EvidenceCreate, VerificationCreate
from codex_web.crypto import KeyPurpose, ManagedKeyCreate
from codex_web.execution_workers import (AssignmentCancelRequest, ExecutionAssignmentCreate,
    AssignmentClaimRequest, AssignmentStartRequest, WorkerCapability)
from codex_web.execution_workspaces import ExecutionWorkspaceAcquire
from codex_web.key_backends import LocalFileKeyBackend
from codex_web.models import Project, WorkItemState
from codex_web.recovery import RecoveryPolicy
from codex_web.resources import RepositoryExecutionScope
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.canonical_events import CanonicalEventBus, CanonicalEventIngestionService
from codex_web.services.crypto_keys import CryptoKeyService
from codex_web.services.control_plane_broker import AssignmentBoundControlPlaneBroker
from codex_web.services.execution_workspaces import ExecutionWorkspaceService
from codex_web.services.procedure_discovery import BrokerProcedureAdmissionService, ProcedureDiscoveryService
from codex_web.services.recovery import RecoveryService, LocalBackupDestination
from codex_web.services.recovery_reconciliation import PROOF_CONTRACT, fingerprint
from codex_web.services.scheduler import SchedulerService
from codex_web.services.skills import SkillService
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.crypto_keys import CryptoKeyStore
from codex_web.storage.execution_workspaces import ExecutionWorkspaceStateStore
from codex_web.storage.recovery import RecoveryStore
from codex_web.storage.runtime_state import ModelMapRepository
from codex_web.storage.scheduler import SchedulerStore
from tests import test_administrative_broker_runtime as admin_fixtures
from tests.test_execution_workspaces import _FakeBackend


class WorkerVerificationTests(unittest.IsolatedAsyncioTestCase):
    _assignment = staticmethod(admin_fixtures.AdministrativeBrokerRuntimeTests._assignment)
    _publish_worker_authority = admin_fixtures.AdministrativeBrokerRuntimeTests._publish_worker_authority
    _request = admin_fixtures.AdministrativeBrokerRuntimeTests._request
    _publish_profile = admin_fixtures.AdministrativeBrokerRuntimeTests._publish_profile

    def verification_resources(self, root):
        return (self.target.id,)

    async def asyncSetUp(self):
        await admin_fixtures.AdministrativeBrokerRuntimeTests.asyncSetUp(self)
        # unittest does not call asyncTearDown when asyncSetUp fails. Register
        # cleanup now, including the broker that a later setup step replaces.
        close = getattr(self.sqlite, 'close', None)
        if close is not None:
            self.addCleanup(close)
        self.addCleanup(self.temp.cleanup)
        self.addAsyncCleanup(self._stop_services)
        await self.broker.stop()
        self.workers.cancel_bootstrap(self.assignment.id, AssignmentCancelRequest(
            expected_fence=self.assignment.fence, reason='Synthetic fixture changes work package'), actor=self.admin)
        root = Path(self.temp.name)
        # The Work Item lives in the real keyed transactional repository. Glue
        # exposes that same bound reader to the existing broker state machine.
        self.items = ModelMapRepository(self.sqlite, namespace='work_item_states',
            legacy_path=root/'never-created-mirror.json', model=WorkItemState)
        now = time.time()
        verification_resources = self.verification_resources(root)
        item = WorkItemState(ref=self.ref, organization_id='local', workspace_id='default',
            project_id='project-a', project_path='group/app', resource_ids=list(verification_resources),
            current_owner='developer', current_stage='implementation_active',
            last_meaningful_update_at=now, updated_at=now, created_at=now)
        self.states[self.ref] = item
        self.items.put(self.ref, item)
        self.work_items.state_machine._work_item_state = self.items.get
        self.work_items.state_machine.dependencies = SimpleNamespace(get_state=self.items.get)
        project = Project(id='project-a', name='Synthetic verification', path=str(root/'repo'))
        Path(project.path).mkdir()
        self.workspaces = ExecutionWorkspaceService(ExecutionWorkspaceStateStore(self.sqlite),
            _FakeBackend(root/'workspaces'), self.resources, lambda _:project)
        workspace = self.workspaces.acquire(ExecutionWorkspaceAcquire(work_item_ref=self.ref,
            execution_id='verification-execution', project_id='project-a', resource_ids=verification_resources,
            repository_resource_id=self.target.id), actor=self.admin)
        self.workers.workspaces=self.workspaces
        profile = self.agent_profiles.create(AgentProfileCreate(profile_id='developer',
            name='Synthetic Developer',role_id='james',authority_role_id='local-admin',
            execution_profile_id='repository-write'),actor=self.admin)
        _, definition = self.profiles.resolve('repository-write', project_id='project-a')
        pin = AgentProfileExecutionBinding(profile_id=profile.profile_id, profile_revision=profile.revision,
            profile_record_id=profile.record_id, role_id=profile.role_id, authority_role_id=profile.authority_role_id,
            execution_profile_id=profile.execution_profile_id, authority_definition_ref=profile.authority_definition_ref)
        assignment = self.workers.create_assignment(ExecutionAssignmentCreate(
            work_item_ref=self.ref, execution_id=workspace.execution_id, project_id='project-a',
            resource_ids=verification_resources, execution_workspace_id=workspace.id,
            repository_scope=RepositoryExecutionScope(organization_id='local',workspace_id='default',
                project_id='project-a',writable_repository_ids=(self.target.id,),source='work_item'),
            execution_contract_version='thread-bootstrap/1.0', required_capabilities=(WorkerCapability.COMMAND_EXECUTION,),
            network=self.assignment.network, limits=self.assignment.limits,
            sandbox='workspace-write',approval_policy='on-request',execution_profile_id='repository-write',
            execution_profile_definition=definition,agent_profile=pin),actor=self.admin)
        claimed=self.workers.claim(self.worker.id,AssignmentClaimRequest(),actor=self.worker_actor,assignment_id=assignment.id)
        self.assignment=self.workers.start(self.worker.id,claimed.id,AssignmentStartRequest(
            fence=claimed.fence,lease_token=claimed.lease.lease_token),actor=self.worker_actor)
        self.current_assignment=self.assignment
        self.broker_response_timeout=5
        self.evidence=ArtifactEvidenceService(ArtifactEvidenceStore(self.sqlite),resources=self.resources)
        self.intents.artifact_evidence=self.evidence
        self.service.execution_workers=self.workers
        self.service.execution_workspaces=self.workspaces
        self.skills=SkillService(self.registry)
        self.service.procedure_admission=BrokerProcedureAdmissionService(
            ProcedureDiscoveryService(self.skills,self.resources), self.workers,self.admin)
        self.broker=AssignmentBoundControlPlaneBroker(self.service,assignment=self.assignment,
            worker_id=self.worker.id,worker_actor=self.worker_actor,fence=self.assignment.fence,
            validator=lambda:self.workers.assignment_for_execution(self.assignment.execution_id,actor=self.admin))
        await self.broker.start()
        # Establish a genuine durable orphan through the existing scheduler and
        # backup pipeline, not by inserting a fabricated successful receipt.
        self.now=time.time()-120
        clock=lambda:self.now
        crypto=CryptoKeyService(CryptoKeyStore(self.sqlite),{'local':LocalFileKeyBackend(root/'keys')})
        key=crypto.create_key(ManagedKeyCreate(purpose=KeyPurpose.BACKUP,backend_type='local'),actor=self.admin)
        ingestion=CanonicalEventIngestionService(CanonicalEventBus(CanonicalEventStore(self.sqlite)))
        self.scheduler=SchedulerService(SchedulerStore(self.sqlite),ingestion,clock=clock,owner_id='fixture-scheduler')
        self.recovery=RecoveryService(RecoveryStore(self.sqlite),state_store=self.sqlite,crypto=crypto,
            evidence=self.evidence,scheduler=self.scheduler,canonical_events=ingestion,
            service_actor=self.admin,destinations=(LocalBackupDestination(root/'backups'),),clock=clock)
        self.recovery.configure(RecoveryPolicy(backup_key_id=key.id,destination_id='local',
            backup_interval_seconds=60,restore_verification_interval_seconds=300,
            retention_count=5,require_audit_integrity=False),actor=self.admin)
        self.now+=61
        with patch.object(self.recovery._scheduled_admission,'finish',side_effect=OSError('fixture settlement unavailable')):
            with self.assertRaisesRegex(OSError,'^fixture settlement unavailable$'):
                await self.scheduler.run_due()
        state=self.recovery.store.load()
        self.firing=next(iter(state.scheduled_firings.values()))
        self.assertEqual(self.firing.status,'running')
        schedule=self.scheduler.store.load().schedules[self.firing.schedule_id]
        metadata={'contract':PROOF_CONTRACT,'event_id':self.firing.event_id,
            'owner_id':self.firing.owner_id,'effect_id':self.firing.effect_id,
            'firing_sha256':fingerprint(self.firing),'schedule_sha256':fingerprint(schedule),
            'policy_fingerprint':state.policy.fingerprint(),'state_sha256':fingerprint(state),
            'writers_quiescent':True,'effects_quiescent':True,'retention_settled':True,
            'effect_outcome':'unknown','all_writer_incarnations_accounted':True,'original_owner_fenced':True,
            'writer_inventory_sha256':'a'*64,'writer_fence_proof_sha256':'b'*64}
        self.original=self.evidence.create_evidence(EvidenceCreate(work_item_ref=self.ref,
            project_id='project-a',resource_ids=(self.target.id,),evidence_type='runtime_result',result='pass',
            observed_at=time.time(),metadata=metadata),actor=self.admin)
        self.request={'work_item_ref':self.ref,'evidence_ids':[self.original.id],
            'method':PROOF_CONTRACT,'result':'verified','source':'sha256:'+hashlib.sha256(b'actual synthetic B review').hexdigest()}
        self.original_state=self.recovery.store.load()

    async def _stop_services(self):
        if hasattr(self,'recovery'):
            await self.recovery.stop()
        await self.broker.stop()

    async def post(self, request=None):
        return await self._request('POST','/api/verifications',payload=request or self.request)

    async def test_actual_worker_verifier_requester_authority_and_read_only_lost_response(self):
        status,_,body=await self.post()
        self.assertEqual(status,200,body)
        record=body['item']
        self.assertEqual(record['verifier_identity_id'],self.worker_actor.identity_id)
        self.assertEqual(body['attribution']['authorizer_identity_id'],self.admin.identity_id)
        self.assertTrue(record['independent'])
        self.assertEqual(record['evidence_ids'],[self.original.id])
        # Simulated observer loss uses read-only reconciliation; no POST retry.
        query='/api/verifications?work_item_ref='+quote(self.ref,safe='')+'&source='+quote(self.request['source'],safe='')
        for _ in range(2):
            status,_,found=await self._request('GET',query)
            self.assertEqual(status,200,found)
            self.assertEqual(found['outcome'],'unique')
            self.assertEqual(found['items'][0]['id'],record['id'])
        self.assertEqual(len(self.evidence.store.load().verifications),1)
        self.assertEqual(self.recovery.store.load(),self.original_state)
        self.assertEqual(self.evidence.get_evidence(self.original.id,actor=self.admin),self.original)

    async def test_identity_override_and_foreign_evidence_refused(self):
        for field,value in (('verifier_identity_id',self.admin.identity_id),('independent',True)):
            status,_,_=await self.post(dict(self.request,**{field:value}))
            self.assertEqual(status,403)
        foreign=self.evidence.create_evidence(EvidenceCreate(evidence_type='runtime_result',result='pass'),actor=self.worker_actor)
        status,_,_=await self.post(dict(self.request,evidence_ids=[foreign.id]))
        self.assertEqual(status,403)
        self.assertEqual(self.evidence.store.load().verifications,[])

    async def test_current_profile_revoked_after_preflight_no_append(self):
        fixture = self
        admission = self.service.procedure_admission
        class RevokeAfterPreflight(BrokerProcedureAdmissionService):
            async def __call__(self, **kwargs):
                result = await super().__call__(**kwargs)
                fixture.agent_profiles.update('developer',AgentProfileUpdate(
                    description='Changed current pin', reason='Synthetic current role change'),actor=fixture.admin)
                return result
        # Keep the real current inspector/read set as well as recorded preflight.
        self.service.procedure_admission=RevokeAfterPreflight(
            admission.discovery, admission.workers, admission.control_actor)
        status,_,body=await self.post()
        self.assertEqual(status,403,body)
        self.assertIn('Pinned execution/Agent Profile no longer effective',body['error']['message'])
        self.assertEqual(self.evidence.store.load().verifications,[])

    async def test_revocation_at_final_append_rolls_back(self):
        original=self.evidence.create_verification
        def revoke(payload,*,actor,_commit=None):
            def guarded(updater):
                self.agent_profiles.update('developer',AgentProfileUpdate(description='Revoked before append',
                    reason='Synthetic final boundary race'),actor=self.admin)
                return _commit(updater)
            return original(payload,actor=actor,_commit=guarded)
        with patch.object(self.evidence,'create_verification',side_effect=revoke):
            status,_,_=await self.post()
        self.assertEqual(status,403)
        self.assertEqual(self.evidence.store.load().verifications,[])

    async def test_original_record_visibility_and_current_snapshot_not_optional(self):
        status,_,body=await self._request('GET','/api/evidence/'+self.original.id)
        self.assertEqual(status,200,body)
        self.assertEqual(body['item']['producer_identity_id'],self.admin.identity_id)
        policy=self.original_state.policy.model_copy(update={'retention_count':6})
        self.recovery.configure(policy,actor=self.admin)
        status,_,_=await self.post()
        self.assertEqual(status,403)
        self.assertEqual(self.evidence.store.load().verifications,[])

    async def test_expired_assignment_and_ambiguous_lookup_not_success(self):
        self.assertEqual((await self.post())[0],200)
        self.assertEqual((await self.post())[0],200)
        query='/api/verifications?work_item_ref='+quote(self.ref,safe='')+'&source='+quote(self.request['source'],safe='')
        status,_,body=await self._request('GET',query)
        self.assertEqual(status,200,body)
        self.assertEqual(body['outcome'],'unknown')
        self.assertEqual(len(body['items']),2)
        # Actual current lease mutation, not an actor/assertion override.
        def expire(state):
            state.assignments=[a.model_copy(update={'lease':a.lease.model_copy(update={'expires_at':0})})
                if a.id==self.assignment.id else a for a in state.assignments]
            return state
        self.workers.store.update(expire)
        status,_,_=await self._request('GET',query)
        self.assertEqual(status,403)

    async def test_requester_authority_and_actual_worker_membership_are_both_required(self):
        # No B operational grant is added: exact API capability belongs to A.
        # Removing B's actual membership still denies canonical attribution.
        state=self.identity.store.load()
        membership=next(m for m in state.memberships if m.identity_id==self.worker_actor.identity_id
            and m.workspace_id==self.worker_actor.workspace_id)
        self.identity.revoke_membership(membership.id,actor=self.admin)
        status,_,_=await self.post()
        self.assertEqual(status,403)
        self.assertEqual(self.evidence.store.load().verifications,[])

    async def test_authority_revoked_at_append_denies_without_root_fallback(self):
        original=self.evidence.create_verification
        def revoke(payload,*,actor,_commit=None):
            def guarded(updater):
                authority=self.registry.resolve(definition_id=AUTHORITY_ROLE_CATALOG_ID,kind=AUTHORITY_ROLE_CATALOG_KIND)
                self.registry.quarantine(authority.record_id,actor=self.admin.identity_id,
                    reason='Synthetic authority revocation before append')
                return _commit(updater)
            return original(payload,actor=actor,_commit=guarded)
        with patch.object(self.evidence,'create_verification',side_effect=revoke):
            status,_,_=await self.post()
        self.assertNotEqual(status,200)
        self.assertEqual(self.evidence.store.load().verifications,[])

    async def test_current_work_item_owner_change_denies_old_scope(self):
        self.items.put(self.ref,self.items.get(self.ref).model_copy(update={'current_owner':'another-developer'}))
        status,_,_=await self.post()
        self.assertEqual(status,403)
        self.assertEqual(self.evidence.store.load().verifications,[])

    async def test_response_loss_after_append_reconciles_same_record_without_second_write(self):
        original=self.evidence.create_verification
        def commit_then_lose(*args,**kwargs):
            original(*args,**kwargs)
            raise OSError('Synthetic response lost after durable Verification append')
        with patch.object(self.evidence,'create_verification',side_effect=commit_then_lose):
            status,_,_=await self.post()
        self.assertEqual(status,500)
        persisted=self.evidence.store.load().verifications
        self.assertEqual(len(persisted),1)
        query='/api/verifications?work_item_ref='+quote(self.ref,safe='')+'&source='+quote(self.request['source'],safe='')
        status,_,body=await self._request('GET',query)
        self.assertEqual(status,200,body)
        self.assertEqual(body['outcome'],'unique')
        self.assertEqual(body['items'][0]['id'],persisted[0].id)
        self.assertEqual(len(self.evidence.store.load().verifications),1)
        self.assertEqual(self.recovery.store.load(),self.original_state)

    async def test_reference_invalidated_at_append_is_not_stale_verification(self):
        original=self.evidence.create_verification
        def invalidate(payload,*,actor,_commit=None):
            def guarded(updater):
                self.evidence.invalidate_evidence(self.original.id,'Synthetic invalidation before append',actor=self.admin)
                return _commit(updater)
            return original(payload,actor=actor,_commit=guarded)
        with patch.object(self.evidence,'create_verification',side_effect=invalidate):
            status,_,_=await self.post()
        self.assertNotEqual(status,200)
        self.assertEqual(self.evidence.store.load().verifications,[])

    async def test_failure_before_append_is_unknown_readback_not_success_or_retry(self):
        with patch.object(self.evidence,'create_verification',side_effect=OSError('Synthetic pre-commit failure')):
            status,_,_=await self.post()
        self.assertEqual(status,500)
        query='/api/verifications?work_item_ref='+quote(self.ref,safe='')+'&source='+quote(self.request['source'],safe='')
        status,_,body=await self._request('GET',query)
        self.assertEqual(status,200,body)
        self.assertEqual(body['outcome'],'unknown')
        self.assertEqual(body['items'],[])
        self.assertEqual(self.evidence.store.load().verifications,[])
        self.assertEqual(self.recovery.store.load(),self.original_state)

    async def test_omitting_b_produced_linked_artifact_cannot_manufacture_independence(self):
        artifact=self.evidence.create_artifact(ArtifactCreate(artifact_type='report',name='Synthetic worker review',
            work_item_ref=self.ref,project_id='project-a',resource_ids=(self.target.id,)),actor=self.worker_actor)
        proof=self.evidence.create_evidence(EvidenceCreate(evidence_type='runtime_result',result='pass',
            work_item_ref=self.ref,project_id='project-a',resource_ids=(self.target.id,),
            artifact_ids=(artifact.id,),metadata=self.original.metadata),actor=self.admin)
        status,_,_=await self.post(dict(self.request,evidence_ids=[proof.id],artifact_ids=[]))
        self.assertEqual(status,403)
        self.assertEqual(self.evidence.store.load().verifications,[])
        # The underlying canonical service likewise derives (never overrides)
        # independence for the actual producer/verifier identity.
        direct=self.evidence.create_verification(VerificationCreate(evidence_ids=(proof.id,),
            artifact_ids=(artifact.id,),method=PROOF_CONTRACT,result='rejected'),actor=self.worker_actor)
        self.assertFalse(direct.independent)

    async def test_schedule_tenant_id_must_match_actual_verifier_tenant(self):
        # Synthetic corruption at the canonical storage boundary. Even a valid
        # referenced ID cannot make a foreign-tenant schedule consumable.
        def move(state):
            schedule=state.schedules[self.firing.schedule_id]
            state.schedules[self.firing.schedule_id]=schedule.model_copy(update={'tenant_id':'foreign-tenant'})
            return state
        self.scheduler.store._update(move)
        schedule=self.scheduler.store.get(self.firing.schedule_id)
        self.assertEqual(schedule.tenant_id,'foreign-tenant')
        metadata=dict(self.original.metadata,schedule_sha256=fingerprint(schedule))
        proof=self.evidence.create_evidence(EvidenceCreate(work_item_ref=self.ref,
            project_id='project-a',resource_ids=(self.target.id,),evidence_type='runtime_result',result='pass',
            observed_at=time.time(),metadata=metadata),actor=self.admin)
        # All current snapshot bindings match. Only the schedule's tenant is
        # foreign, so stale-proof rejection cannot satisfy this regression.
        recovery=self.recovery.store.load()
        self.assertEqual(proof.metadata['schedule_sha256'],fingerprint(schedule))
        self.assertEqual(proof.metadata['firing_sha256'],fingerprint(recovery.scheduled_firings[self.firing.event_id]))
        self.assertEqual(proof.metadata['state_sha256'],fingerprint(recovery))
        self.assertEqual(proof.metadata['policy_fingerprint'],recovery.policy.fingerprint())
        status,_,body=await self.post(dict(self.request,evidence_ids=[proof.id]))
        self.assertEqual(status,403,body)
        self.assertIn('Original Recovery firing/schedule/policy unavailable in current tenant',body['error']['message'])
        self.assertEqual(self.evidence.store.load().verifications,[])
        self.assertEqual(self.evidence.get_evidence(self.original.id,actor=self.admin),self.original)
