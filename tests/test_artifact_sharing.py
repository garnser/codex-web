"""Private real authority/Registry/governance/content/Work owners, no live sharing.

Only the existing runtime fixture's native/session transport edges are controlled.
Source capture records are created through canonical writers with its closed
provenance contract; #2006 owns actual repository capture qualification.
"""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex_web.artifact_evidence import ArtifactCreate, EvidenceCreate, sha256_digest
from codex_web.authority import AUTHORITY_ROLE_CATALOG_ID, AUTHORITY_ROLE_CATALOG_KIND
from codex_web.definitions import DefinitionContext, DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.models import ExecutionPreflightAttempt
from codex_web.resources import ResourceCreate, ResourceAlias, RepositoryExecutionScope, RepositoryExecutionTarget
from codex_web.agent_profiles import AgentProfileCreate, AgentProfileExecutionBinding
from codex_web.execution_workers import ExecutionAssignmentCreate, ExecutionWorkerRegister, AssignmentClaimRequest, AssignmentStartRequest, AssignmentCompleteRequest, AssignmentCancelRequest
from codex_web.execution_workspaces import ExecutionWorkspaceAcquire, ExecutionWorkspaceRelease
from codex_web.services.control_plane_broker import ControlPlaneBrokerDeniedError
from codex_web.services.artifact_content import ArtifactContentRegistry, ArtifactContentService
from codex_web.services.data_governance import DataGovernanceService
from codex_web.services.local_artifact_content import LocalArtifactContentStore
from codex_web.storage.data_governance import DataGovernanceStore
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.services.execution_preflight import ExecutionPreflightService
from tests import test_worker_verifications as fixtures

AVAILABLE = importlib.util.find_spec('codex_web.services.worker_artifact_sharing') is not None


class GovernedArtifactSharingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = f = fixtures.WorkerVerificationTests()
        f.addCleanup,f.addAsyncCleanup = self.addCleanup,self.addAsyncCleanup
        f.verification_resources = lambda root:self.prepare_resources()
        await f.asyncSetUp()
        registry = ArtifactContentRegistry()
        registry.register(LocalArtifactContentStore(Path(f.temp.name)/'shared-content'))
        f.evidence.content = ArtifactContentService(registry)
        f.evidence.governance = DataGovernanceService(DataGovernanceStore(f.sqlite))
        f.evidence.governance.store.load()
        f.evidence.work_item_host = SimpleNamespace(_load_work_item_states=f.items.load)
        self.target_work = f.items.get(f.ref).model_copy(update={'ref':'group/recipient!33',
            'resource_ids':[self.target.id],'current_owner':'quinn'})
        f.items.put(self.target_work.ref,self.target_work)
        preflights = ExecutionPreflightStore(f.sqlite)
        preflights.prepare_work_item_index()
        f.service.execution_preflights = ExecutionPreflightService(preflights)
        self.preflights = preflights
        self.capture_source()
        self.payload = dict(method='governed-artifact-sharing.v1',request_id='1'*32,
            expected_fence=f.assignment.fence,source_evidence_id=self.proof.id,target_work_item_ref=self.target_work.ref)
        self.before_assignment = f.assignment.model_copy(deep=True)
        if AVAILABLE:
            from codex_web.services.artifact_sharing import ArtifactSharingPolicyService
            from codex_web.services.worker_artifact_sharing import WorkerArtifactSharingAdapter, OPERATION
            self.policies = ArtifactSharingPolicyService(f.registry,f.resources)
            f.service.artifact_sharing_policies = self.policies
            self.policy = self.publish_policy()
            self.adapter = WorkerArtifactSharingAdapter(f.service,policies=self.policies)
            self.resolved = SimpleNamespace(operation=OPERATION,target_ref=None,query={'repository_resource_id':[f.target.id]})
        self.initial = f.evidence.store.load()

    def capture_source(self):
        f = self.f
        raw = b'canonical component map\n'
        self.body = json.dumps({'method':'repository-source-capture.v1','commit_sha':'a'*40,
            'files':[{'path':'docs/component-map.md','blob_sha':hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest(),
                'sha256':hashlib.sha256(raw).hexdigest(),'size_bytes':len(raw),'content_base64':base64.b64encode(raw).decode()}]},
            sort_keys=True,separators=(',',':')).encode()
        self.capture_key = f'{f.assignment.id}:{f.assignment.fence}:' + '2'*32
        common = dict(project_id=f.assignment.project_id,work_item_ref=f.ref,
            execution_id=f.assignment.execution_id,execution_workspace_id=f.assignment.execution_workspace_id,
            resource_ids=(f.target.id,),provider='repository-source-capture.v1',source='worker-repository-source-capture',external_id=self.capture_key,
            metadata={'assignment_id':f.assignment.id,'assignment_fence':f.assignment.fence,
                'worker_id':f.worker.id,'requester_identity_id':f.admin.identity_id,
                'request_sha256':'c'*64,'commit_sha':'a'*40,'result':'source_info'})
        self.artifact = f.evidence.create_artifact(ArtifactCreate(**common,artifact_type='report',
            name='Canonical source capture fixture',revision='a'*40,digest=sha256_digest(self.body),
            classification='confidential'),actor=f.worker_actor,idempotency_key=self.capture_key)
        self.artifact = f.evidence.attach_artifact_content(self.artifact.id,(self.body,),actor=f.worker_actor,
            media_type='application/json',expected_sha256=hashlib.sha256(self.body).hexdigest())
        self.proof = f.evidence.create_evidence(EvidenceCreate(**common,evidence_type='artifact_verification',
            result='info',artifact_ids=(self.artifact.id,),digest=sha256_digest(self.body)),actor=f.worker_actor,idempotency_key=self.capture_key)

    def prepare_resources(self):
        f = self.f
        self.target = f.resources.create(ResourceCreate(name='Receiving repository',resource_type='repository'),actor=f.admin)
        self.publish_authority()
        return (f.target.id,)

    def publish_authority(self, *, allow=True, target=True):
        f = self.f
        active = f.registry.resolve(definition_id=AUTHORITY_ROLE_CATALOG_ID,kind=AUTHORITY_ROLE_CATALOG_KIND)
        payload = json.loads(json.dumps(active.payload))
        payload['roles'] = [r for r in payload['roles'] if r['id'] != 'sharing-worker']
        resources = [f.target.id] + ([self.target.id] if target else [])
        grants = [{'id':'sharing-worker-'+cap.replace('.','-'),'capability':cap,'level':'execute',
            'project_ids':['project-a'],'resource_ids':resources} for cap in
            (['api.artifacts.share','api.artifacts.write'] if allow else ['api.artifacts.write'])]
        payload['roles'].append({'id':'sharing-worker','name':'Controlled data sharing fixture',
            'description':'Private fixture grants scoped source-to-target governed INFO delivery','grants':grants})
        payload['bindings'] = [b for b in payload.get('bindings',[]) if b['id'] != 'sharing-worker-binding']
        payload['bindings'].append({'id':'sharing-worker-binding','role_id':'sharing-worker','subject_kind':'identity',
            'subject_id':f.worker_actor.identity_id,'organization_id':'local','workspace_id':'default','project_ids':['project-a']})
        draft=f.registry.create_draft(DefinitionDraftCreate(definition_id=AUTHORITY_ROLE_CATALOG_ID,
            kind=AUTHORITY_ROLE_CATALOG_KIND,definition_schema_version='1.0',payload=payload,actor='private-publisher'))
        f.registry.approve_publication(draft.record_id,actor='private-reviewer',reference='private-scope-approval',reason='Controlled canonical fixture grants')
        f.registry.publish(draft.record_id,DefinitionPublishRequest(actor='private-publisher',expected_active_revision=active.revision))

    def publish_policy(self, *, target=True, max_bytes=262144):
        from codex_web.artifact_sharing_policy import KIND, POLICY_ID
        f = self.f
        payload = {'schema_version':'1.0','organization_id':'local','workspace_id':'default','project_id':'project-a',
            'rules':[{'id':'source-to-receiver','source_resource_ids':[f.target.id],
                'target_resource_ids':[self.target.id if target else f.target.id],'max_content_bytes':max_bytes}]}
        draft=f.registry.create_draft(DefinitionDraftCreate(definition_id=POLICY_ID,kind=KIND,
            definition_schema_version='1.0',scope_type='project',scope_id='project-a',payload=payload,actor='private-publisher'))
        f.registry.approve_publication(draft.record_id,actor='private-reviewer',reference='private-directional-rule',reason='Controlled canonical sharing rule')
        return f.registry.publish(draft.record_id,DefinitionPublishRequest(actor='private-publisher'))

    async def share(self, payload=None):
        if not AVAILABLE:
            # OLD uses the existing real broker transport and ordinary unknown
            # operation refusal, never a missing constructor/method setup error.
            status,_,response = await self.f._request('POST','/api/artifacts/shares?repository_resource_id='+self.f.target.id,payload=payload or self.payload)
            self.assertEqual(status,200,response)
            return response
        return self.adapter.share(self.f.assignment,self.f.worker_actor,self.resolved,
            payload or self.payload,self.f.broker._validate_current)

    async def test_approved_source_worker_delivers_governed_info_to_exact_target_work(self):
        # Qualified #2006 transport: only the sandbox capture observation is
        # controlled; real runtime custody, broker, writer and receipt are used.
        from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime
        captured=json.loads(self.body)
        calls=[]
        def capture(assignment,**options):
            calls.append(assignment.id)
            options['poll_hook']()
            return json.loads(json.dumps(captured))
        f=self.f
        f.service.local_execution_worker=LocalExecutionWorkerRuntime(f.workers,f.workspaces,
            SimpleNamespace(capture_repository_source=capture),worker=f.worker,
            worker_actor=f.worker_actor,control_actor=f.admin,artifact_evidence=f.evidence)
        item=captured['files'][0]
        request={'method':'repository-source-capture.v1','request_id':'4'*32,
            'expected_fence':f.assignment.fence,'work_item_ref':f.ref,
            'repository_resource_id':f.target.id,'commit_sha':captured['commit_sha'],
            'files':[{key:item[key] for key in ('path','blob_sha','sha256')}]}
        capture_status,_,capture_result=await f._request('POST',
            '/api/repository-source-captures?repository_resource_id='+f.target.id,payload=request)
        self.assertEqual(capture_status,200,capture_result)
        self.assertEqual(calls,[f.assignment.id])
        self.artifact=f.evidence.get_artifact(capture_result['artifact_id'],actor=f.worker_actor)
        self.proof=f.evidence.get_evidence(capture_result['evidence_id'],actor=f.worker_actor)
        self.assertEqual(self.proof.digest,self.artifact.digest)
        self.assertEqual(self.proof.result,'info')
        self.payload['source_evidence_id']=self.proof.id
        status,_,result = await self.f._request('POST','/api/artifacts/shares?repository_resource_id='+self.f.target.id,payload=self.payload)
        self.assertEqual(status,200,result)
        derived = self.f.evidence.get_artifact(result['artifact_id'],actor=self.f.worker_actor)
        proof = self.f.evidence.get_evidence(result['evidence_id'],actor=self.f.worker_actor)
        self.assertEqual(derived.work_item_ref,self.target_work.ref)
        self.assertEqual(derived.resource_ids,(self.target.id,))
        self.assertEqual(derived.producer_identity_id,self.f.worker_actor.identity_id)
        self.assertEqual(proof.result,'info')
        self.assertEqual(proof.artifact_ids,(derived.id,))
        self.assertFalse(result['independent_qa_or_acceptance'])
        self.assertFalse(result['repository_execution_scope_changed'])
        current=self.f.broker._validate_current()
        # Real broker admission appends discovery provenance; it cannot mutate
        # custody, authority, lease, Profile, scope, checkpoint or other state.
        observed={'procedure_discoveries','updated_at'}
        self.assertEqual(current.model_dump(exclude=observed),self.before_assignment.model_dump(exclude=observed))
        prefix=len(self.before_assignment.procedure_discoveries)
        self.assertEqual(current.procedure_discoveries[:prefix],self.before_assignment.procedure_discoveries)
        discoveries=current.procedure_discoveries[prefix:]
        self.assertEqual(tuple(d.operation for d in discoveries),('repository.source.capture','verification.artifact.share'))
        for discovery in discoveries:
            self.assertTrue(discovery.admitted)
            self.assertEqual(discovery.outcome,'no_match')
            self.assertEqual(discovery.selected,())
            self.assertEqual(discovery.blockers,())
            self.assertEqual((discovery.actor_identity_id,discovery.organization_id,discovery.workspace_id,
                discovery.project_id,discovery.resource_ids,discovery.policy_source),
                (f.admin.identity_id,f.assignment.organization_id,f.assignment.workspace_id,
                 f.assignment.project_id,(f.target.id,),'policy:assignment-control-plane'))
            self.assertRegex(discovery.context_checksum,r'^[0-9a-f]{64}$')
            self.assertGreaterEqual(discovery.evaluated_at,self.before_assignment.started_at)
            self.assertLessEqual(discovery.evaluated_at,current.updated_at)
        self.assertGreaterEqual(current.updated_at,self.before_assignment.updated_at)
        self.assertEqual(self.f.evidence.get_artifact(self.artifact.id,actor=self.f.worker_actor),self.artifact)
        pointer, chunks = self.f.evidence.open_artifact_content(derived.id,actor=self.f.worker_actor)
        self.assertEqual(b''.join(chunks),self.body)
        self.assertEqual(pointer.size_bytes,len(self.body))
        await self.receiving_worker_reads(result['artifact_id'])
        governance = self.f.evidence.governance.store.load()
        record = next(r for r in governance.records if r.id==derived.governance_record_id)
        parent=next(r for r in governance.records if r.id==self.artifact.governance_record_id)
        self.assertEqual(record.classification,parent.classification)
        self.assertIn(self.artifact.governance_record_id,record.source_record_ids)
        self.assertIn(self.proof.governance_record_id,record.source_record_ids)

    async def receiving_worker_reads(self, artifact_id):
        from codex_web.services.worker_verifications import WorkerVerificationAdapter, OPERATIONS
        f = self.f
        receiver = f.identity.bootstrap_service_actor(identity_id='sharing-recipient-worker',name='Private recipient',
            scope=f.scope,service_scopes=('execution-worker:run','action-intent:worker','secret:use'))
        registered=f.workers.register(ExecutionWorkerRegister(service_identity_id=receiver.identity_id,
            pool='private-receiver',version='1.0',capabilities=f.worker.capabilities,
            supported_execution_contract_versions=('thread-bootstrap/1.0',),max_concurrency=1),actor=f.admin)
        workspace=f.workspaces.acquire(ExecutionWorkspaceAcquire(work_item_ref=self.target_work.ref,
            execution_id='private-recipient-execution',project_id='project-a',resource_ids=(self.target.id,),
            repository_resource_id=self.target.id),actor=f.admin)
        profile=f.agent_profiles.create(AgentProfileCreate(profile_id='quinn-sharing-recipient',name='Private receiver',
            role_id='quinn',authority_role_id='local-admin',execution_profile_id='repository-write'),actor=f.admin)
        pin=AgentProfileExecutionBinding(profile_id=profile.profile_id,profile_revision=profile.revision,
            profile_record_id=profile.record_id,role_id=profile.role_id,authority_role_id=profile.authority_role_id,
            execution_profile_id=profile.execution_profile_id,authority_definition_ref=profile.authority_definition_ref)
        _,definition=f.profiles.resolve('repository-write',project_id='project-a')
        assignment=f.workers.create_assignment(ExecutionAssignmentCreate(work_item_ref=self.target_work.ref,
            execution_id=workspace.execution_id,project_id='project-a',resource_ids=(self.target.id,),
            execution_workspace_id=workspace.id,repository_scope=RepositoryExecutionScope(organization_id='local',
                workspace_id='default',project_id='project-a',writable_repository_ids=(self.target.id,),source='work_item'),
            execution_contract_version='thread-bootstrap/1.0',required_capabilities=f.assignment.required_capabilities,
            sandbox='workspace-write',approval_policy='on-request',execution_profile_id='repository-write',
            execution_profile_definition=definition,agent_profile=pin),actor=f.admin)
        claimed=f.workers.claim(registered.id,AssignmentClaimRequest(),actor=receiver,assignment_id=assignment.id)
        actual=f.workers.start(registered.id,claimed.id,AssignmentStartRequest(fence=claimed.fence,
            lease_token=claimed.lease.lease_token),actor=receiver)
        read=SimpleNamespace(operation=next(o for o in OPERATIONS if o.id=='verification.content.read'),
            target_ref=artifact_id,query={})
        result=WorkerVerificationAdapter(f.service).read(actual,receiver,read,
            lambda:f.workers.assignment_for_execution(actual.execution_id,actor=f.admin))
        self.receiver = (actual,receiver,read)
        self.assertTrue(result['complete'])
        self.assertEqual(base64.b64decode(result['content_base64']),self.body)
        self.assertEqual(actual.resource_ids,(self.target.id,))
        self.assertNotIn(f.target.id,actual.resource_ids)

    async def test_receiving_reader_rechecks_source_revocation_and_current_policy(self):
        result=await self.share()
        await self.receiving_worker_reads(result['artifact_id'])
        from codex_web.services.worker_verifications import WorkerVerificationAdapter
        actual,receiver,read=self.receiver
        def consume():
            return WorkerVerificationAdapter(self.f.service).read(actual,receiver,read,
                lambda:self.f.workers.assignment_for_execution(actual.execution_id,actor=self.f.admin))
        self.f.evidence.invalidate_evidence(self.proof.id,'Private source revoked after delivery',actor=self.f.worker_actor)
        with self.assertRaisesRegex(ControlPlaneBrokerDeniedError,'source or original digest'): consume()
        self.assertFalse(result['independent_qa_or_acceptance'])

    async def test_receiving_reader_refuses_parent_context_denial_and_policy_supersession(self):
        result=await self.share()
        await self.receiving_worker_reads(result['artifact_id'])
        from codex_web.services.worker_verifications import WorkerVerificationAdapter
        actual,receiver,read=self.receiver
        def consume():
            return WorkerVerificationAdapter(self.f.service).read(actual,receiver,read,
                lambda:self.f.workers.assignment_for_execution(actual.execution_id,actor=self.f.admin))
        original=self.f.evidence.governance.store.load()
        def deny(state):
            next(r for r in state.records if r.id==self.artifact.governance_record_id).deny_model_context=True
            return state
        self.f.evidence.governance.store.update(deny)
        with self.assertRaisesRegex(ControlPlaneBrokerDeniedError,'governance denied'): consume()
        self.f.evidence.governance.store.update(lambda _:original)
        self.publish_policy()  # Creation reference is provenance, not permanent invalidation.
        self.assertTrue(consume()['complete'])
        self.publish_policy(target=False)
        with self.assertRaisesRegex(ControlPlaneBrokerDeniedError,'sharing direction'): consume()

    async def fresh_read_source(self, *, bootstrap=False):
        """Canonical fresh READ lease/assignment; unrelated context has no sharing grant."""
        from copy import deepcopy
        from codex_web.services.control_plane_broker import AssignmentBoundControlPlaneBroker
        f=self.f
        await f.broker.stop()
        f.workers.complete(f.worker.id,f.assignment.id,AssignmentCompleteRequest(
            fence=f.assignment.fence,lease_token=f.assignment.lease.lease_token,succeeded=True),actor=f.worker_actor)
        f.workspaces.release(f.assignment.execution_workspace_id,ExecutionWorkspaceRelease(reason='Private READ re-admission'),actor=f.admin)
        other_path=Path(f.temp.name)/'unrelated-read-source'
        other_path.mkdir()
        other=f.resources.create(ResourceCreate(name='Unrelated READ context',resource_type='repository',
            aliases=[ResourceAlias(namespace='filesystem',value=str(other_path))]),actor=f.admin)
        active=f.profiles.record(organization_id='local',workspace_id='default',project_id='project-a')
        payload=deepcopy(active.payload)
        profile=deepcopy(next(p for p in payload['profiles'] if p['id']=='repository-write'))
        profile.update(id='sharing-read-source',repository_access='mutable' if bootstrap else 'read-only',
            control_plane_operations=['verification.artifact.share'])
        payload['profiles'].append(profile)
        # Agent Profile creation resolves a reusable tenant/global template;
        # actual assignment admission separately pins the Project override.
        # Publish both through the real owner, never a resolver/cache bypass.
        template_active=f.profiles.record(organization_id='local',workspace_id='default')
        template_payload=deepcopy(template_active.payload)
        template_payload['profiles'].append(deepcopy(profile))
        template_draft=f.registry.create_draft(DefinitionDraftCreate(definition_id=template_active.definition_id,
            kind=template_active.kind,definition_schema_version=template_active.definition_schema_version,
            scope_type=template_active.scope_type,scope_id=template_active.scope_id,
            payload=template_payload,actor='private-publisher'))
        f.registry.approve_publication(template_draft.record_id,actor='private-reviewer',
            reference='private-reusable-profile',reason='Controlled resource-free execution template')
        f.registry.publish(template_draft.record_id,DefinitionPublishRequest(actor='private-publisher',
            expected_active_revision=template_active.revision))
        draft=f.registry.create_draft(DefinitionDraftCreate(definition_id=active.definition_id,
            kind=active.kind,definition_schema_version=active.definition_schema_version,
            scope_type=active.scope_type,scope_id=active.scope_id,payload=payload,actor='private-publisher'))
        f.registry.approve_publication(draft.record_id,actor='private-reviewer',reference='private-read-profile',reason='Controlled READ source fixture')
        f.registry.publish(draft.record_id,DefinitionPublishRequest(actor='private-publisher',expected_active_revision=active.revision))
        template,template_ref=f.profiles.resolve('sharing-read-source',organization_id='local',workspace_id='default')
        scoped,scoped_ref=f.profiles.resolve('sharing-read-source',organization_id='local',workspace_id='default',project_id='project-a')
        self.assertEqual(template,scoped)
        self.assertNotEqual(template_ref.record_id,scoped_ref.record_id)
        profile=f.agent_profiles.create(AgentProfileCreate(profile_id='sharing-read-source',name='Private READ source',
            role_id='james',authority_role_id='local-admin',execution_profile_id='sharing-read-source'),actor=f.admin)
        work=f.items.get(f.ref).model_copy(update={'current_owner':'james'})
        f.items.put(f.ref,work)
        pin=AgentProfileExecutionBinding(profile_id=profile.profile_id,profile_revision=profile.revision,
            profile_record_id=profile.record_id,role_id=profile.role_id,authority_role_id=profile.authority_role_id,
            execution_profile_id=profile.execution_profile_id,authority_definition_ref=profile.authority_definition_ref)
        subject={'kind':'thread_bootstrap','ref':'private-sharing-bootstrap'} if bootstrap else None
        context=None
        scope_ids=(f.target.id,other.id)
        workspace=f.workspaces.acquire(ExecutionWorkspaceAcquire(subject=subject,
            work_item_ref=None if bootstrap else f.ref,resource_context=context,
            execution_id='private-read-sharing-source',project_id='project-a',resource_ids=scope_ids,
            repository_resource_id=f.target.id,read_only_repository_ids=(other.id,) if bootstrap else scope_ids,
            lease_mode='write' if bootstrap else 'read'),actor=f.admin)
        target=RepositoryExecutionTarget(organization_id='local',workspace_id='default',project_id='project-a',
            mutable_repository_id=f.target.id if bootstrap else None,
            read_only_primary_repository_id=None if bootstrap else f.target.id,
            read_only_repository_ids=(other.id,) if bootstrap else scope_ids,source='explicit')
        _,definition=f.profiles.resolve('sharing-read-source',project_id='project-a')
        assignment=f.workers.create_assignment(ExecutionAssignmentCreate(subject=subject,
            work_item_ref=None if bootstrap else f.ref,resource_context=context,
            execution_id=workspace.execution_id,project_id='project-a',resource_ids=scope_ids,
            execution_workspace_id=workspace.id,repository_target=target,
            repository_scope=RepositoryExecutionScope.from_target(target),
            execution_contract_version='thread-bootstrap/1.0',required_capabilities=f.assignment.required_capabilities,
            network=f.assignment.network,limits=f.assignment.limits,sandbox='workspace-write' if bootstrap else 'read-only',approval_policy='on-request',
            execution_profile_id='sharing-read-source',execution_profile_definition=definition,agent_profile=pin),actor=f.admin)
        claimed=f.workers.claim(f.worker.id,AssignmentClaimRequest(),actor=f.worker_actor,assignment_id=assignment.id)
        f.assignment=f.workers.start(f.worker.id,claimed.id,AssignmentStartRequest(fence=claimed.fence,
            lease_token=claimed.lease.lease_token),actor=f.worker_actor)
        f.current_assignment=f.assignment
        if bootstrap:
            f.workspaces.authorize_publication_recovery(workspace.id,work_item_ref=f.ref,
                repository_resource_id=f.target.id,assignment_id=f.assignment.id,assignment_fence=f.assignment.fence,
                source_execution_id=f.assignment.execution_id,source_subject=f.assignment.subject,
                assignment_deadline_at=f.assignment.deadline_at,actor=f.admin)
        f.broker=AssignmentBoundControlPlaneBroker(f.service,assignment=f.assignment,
            worker_id=f.worker.id,worker_actor=f.worker_actor,fence=f.assignment.fence,
            validator=lambda:f.workers.assignment_for_execution(f.assignment.execution_id,actor=f.admin))
        await f.broker.start()
        self.capture_source()
        self.payload.update(expected_fence=f.assignment.fence,source_evidence_id=self.proof.id)
        return other

    async def test_read_source_with_unrelated_context_uses_only_exact_source_sharing_scope(self):
        other=await self.fresh_read_source()
        status,_,result=await self.f._request('POST','/api/artifacts/shares?repository_resource_id='+self.f.target.id,payload=self.payload)
        self.assertEqual(status,200,result)
        self.assertEqual(self.f.assignment.repository_scope.writable_repository_ids,())
        self.assertIn(other.id,self.f.assignment.repository_scope.read_only_repository_ids)
        before=self.f.evidence.store.load()
        bad=SimpleNamespace(operation=self.resolved.operation,target_ref=None,query={'repository_resource_id':[other.id]})
        with self.assertRaises(ControlPlaneBrokerDeniedError):
            self.adapter.share(self.f.assignment,self.f.worker_actor,bad,
                {**self.payload,'request_id':'3'*32},self.f.broker._validate_current)
        self.assertEqual(self.f.evidence.store.load(),before)

    async def test_sender_bootstrap_retirement_preserves_durable_receiving_read(self):
        await self.fresh_read_source(bootstrap=True)
        result=await self.share()
        await self.receiving_worker_reads(result['artifact_id'])
        f=self.f
        retired=f.workers.cancel_bootstrap(f.assignment.id,AssignmentCancelRequest(
            expected_fence=f.assignment.fence,reason='Private normal producer retirement'),actor=f.admin)
        self.assertEqual(retired.status,'cancelled')
        self.assertEqual(retired.fence,f.assignment.fence+1)
        self.assertIsNone(retired.lease)
        from codex_web.services.worker_verifications import WorkerVerificationAdapter
        actual,receiver,read=self.receiver
        self.assertTrue(WorkerVerificationAdapter(f.service).read(actual,receiver,read,
            lambda:f.workers.assignment_for_execution(actual.execution_id,actor=f.admin))['complete'])

    async def test_sender_completion_preserves_durable_receiving_read(self):
        result=await self.share()
        await self.receiving_worker_reads(result['artifact_id'])
        f=self.f
        retired=f.workers.complete(f.worker.id,f.assignment.id,AssignmentCompleteRequest(
            fence=f.assignment.fence,lease_token=f.assignment.lease.lease_token,succeeded=True),actor=f.worker_actor)
        self.assertEqual(retired.status,'succeeded')
        self.assertIsNone(retired.lease)
        from codex_web.services.worker_verifications import WorkerVerificationAdapter
        actual,receiver,read=self.receiver
        self.assertTrue(WorkerVerificationAdapter(f.service).read(actual,receiver,read,
            lambda:f.workers.assignment_for_execution(actual.execution_id,actor=f.admin))['complete'])

    async def test_registered_descriptor_and_same_application_policy_owner(self):
        import ast
        from codex_web.services.control_plane_broker import ControlPlaneBrokerService
        operation=next(o for o in ControlPlaneBrokerService.operations() if o.id=='verification.artifact.share')
        descriptor=ControlPlaneBrokerService.public_operation(operation)
        self.assertIn('repository_resource_id',descriptor['query_parameters'])
        self.assertIn('source+target',descriptor['authority'])
        self.assertEqual(descriptor['request_body']['additionalProperties'],False)
        tree=ast.parse((Path(__file__).resolve().parents[1]/'codex_web/application.py').read_text())
        owners=[n for n in ast.walk(tree) if isinstance(n,ast.Call)
            and isinstance(n.func,ast.Name) and n.func.id=='ArtifactSharingPolicyService']
        brokers=[n for n in ast.walk(tree) if isinstance(n,ast.Call)
            and isinstance(n.func,ast.Name) and n.func.id=='ControlPlaneBrokerService']
        self.assertEqual(len(owners),1)
        self.assertEqual([a.id for a in owners[0].args],['definition_registry_service','resource_catalog_service'])
        self.assertEqual(len(brokers),1)
        supplied=next(k.value for k in brokers[0].keywords if k.arg=='artifact_sharing_policies')
        self.assertEqual(supplied.id,'artifact_sharing_policy_service')

    async def test_policy_broadening_requires_actual_independent_publication_receipt(self):
        from codex_web.services.definitions import DefinitionApprovalRequiredError
        original=self.f.registry.get_record(self.policy.record_id)
        payload=json.loads(json.dumps(original.payload))
        payload['rules'][0]['target_resource_ids'].append(self.f.target.id)
        draft=self.f.registry.create_draft(DefinitionDraftCreate(definition_id=original.definition_id,
            kind=original.kind,definition_schema_version='1.0',scope_type='project',scope_id='project-a',
            payload=payload,actor='private-publisher'))
        with self.assertRaises(DefinitionApprovalRequiredError):
            self.f.registry.publish(draft.record_id,DefinitionPublishRequest(actor='private-publisher'))
        self.assertEqual(self.f.registry.resolve(definition_id=original.definition_id,kind=original.kind,
            context=DefinitionContext(project_id='project-a')),original)
        self.assertEqual(self.f.evidence.store.load(),self.initial)

    async def test_query_or_actor_overrides_are_refused_without_writes(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            await self.share({**self.payload,'actor_identity_id':self.f.admin.identity_id})
        for query in ({},{'repository_resource_id':[self.f.target.id,self.target.id]},
                {'repository_resource_id':[self.f.target.id],'target_resource_id':[self.target.id]}):
            with self.subTest(query=query):
                resolved=SimpleNamespace(operation=self.resolved.operation,target_ref=None,query=query)
                with self.assertRaises(ControlPlaneBrokerDeniedError):
                    self.adapter.share(self.f.assignment,self.f.worker_actor,resolved,self.payload,self.f.broker._validate_current)
        self.assertEqual(self.f.evidence.store.load(),self.initial)

    async def test_missing_canonical_target_work_is_typed_refusal_without_writes(self):
        with self.assertRaisesRegex(ControlPlaneBrokerDeniedError,'Canonical sharing Work'):
            await self.share({**self.payload,'target_work_item_ref':'group/missing!99'})
        self.assertEqual(self.f.evidence.store.load(),self.initial)

    async def test_identical_request_returns_original_ids_and_changed_target_refuses(self):
        first = await self.share()
        again = await self.share()
        self.assertTrue(again['deduplicated'])
        self.assertEqual(first['artifact_id'],again['artifact_id'])
        self.assertEqual(first['evidence_id'],again['evidence_id'])
        before = self.f.evidence.store.load()
        other = self.target_work.model_copy(update={'ref':'group/recipient!34'})
        self.f.items.put(other.ref,other)
        with self.assertRaises(ControlPlaneBrokerDeniedError):
            await self.share({**self.payload,'target_work_item_ref':other.ref})
        self.assertEqual(self.f.evidence.store.load(),before)

    async def test_missing_policy_owner_and_quarantined_policy_fail_closed(self):
        policies=self.adapter.policies
        self.adapter.policies=None
        with self.assertRaisesRegex(ControlPlaneBrokerDeniedError,'policy/session'): await self.share()
        self.adapter.policies=policies
        self.f.registry.quarantine(self.policy.record_id,actor=self.f.admin.identity_id,reason='Private policy revoked')
        with self.assertRaisesRegex(ControlPlaneBrokerDeniedError,'policy is unavailable'): await self.share()
        self.assertEqual(self.f.evidence.store.load(),self.initial)

    async def test_missing_directional_policy_or_missing_target_authority_never_writes(self):
        self.publish_policy(target=False)
        with self.assertRaises(ControlPlaneBrokerDeniedError): await self.share()
        self.assertEqual(self.f.evidence.store.load(),self.initial)
        self.publish_policy()
        self.publish_authority(target=False)
        with self.assertRaises(ControlPlaneBrokerDeniedError): await self.share()
        self.assertEqual(self.f.evidence.store.load(),self.initial)
        self.publish_authority(allow=False)
        with self.assertRaises(ControlPlaneBrokerDeniedError): await self.share()
        self.assertEqual(self.f.evidence.store.load(),self.initial)

    async def test_target_work_scope_drift_and_foreign_project_refuse(self):
        for change in ({'project_id':'foreign-project'},{'resource_ids':[self.f.target.id,self.target.id]}):
            with self.subTest(change=change):
                self.f.items.put(self.target_work.ref,self.target_work.model_copy(update=change))
                with self.assertRaises(ControlPlaneBrokerDeniedError): await self.share()
                self.assertEqual(self.f.evidence.store.load(),self.initial)
        self.f.items.put(self.target_work.ref,self.target_work)

    async def test_source_governance_denial_and_expiry_refuse(self):
        original = self.f.evidence.governance.store.load()
        def deny(state):
            next(r for r in state.records if r.id==self.artifact.governance_record_id).deny_model_context=True
            return state
        self.f.evidence.governance.store.update(deny)
        with self.assertRaises(ControlPlaneBrokerDeniedError): await self.share()
        self.assertEqual(self.f.evidence.store.load(),self.initial)
        self.f.evidence.governance.store.update(lambda _:original)
        def expire(state):
            index=next(i for i,r in enumerate(state.artifacts) if r.id==self.artifact.id)
            state.artifacts[index]=state.artifacts[index].model_copy(update={'retention_expires_at':0})
            return state
        self.f.evidence.store.update(expire)
        before=self.f.evidence.store.load()
        with self.assertRaises(ControlPlaneBrokerDeniedError): await self.share()
        self.assertEqual(self.f.evidence.store.load(),before)

    async def test_replaced_source_digest_under_same_ids_refuses(self):
        def alter(state):
            index=next(i for i,r in enumerate(state.artifacts) if r.id==self.artifact.id)
            state.artifacts[index]=state.artifacts[index].model_copy(update={'digest':sha256_digest(b'changed')})
            return state
        self.f.evidence.store.update(alter)
        before=self.f.evidence.store.load()
        with self.assertRaises(ControlPlaneBrokerDeniedError): await self.share()
        self.assertEqual(self.f.evidence.store.load(),before)

    async def test_current_policy_and_target_rechecked_after_content_io(self):
        original=self.f.evidence.open_artifact_content
        def opened(*args,**kwargs):
            pointer,stream=original(*args,**kwargs)
            def changed():
                yield from stream
                self.publish_policy(target=False)
            return pointer,changed()
        with patch.object(self.f.evidence,'open_artifact_content',side_effect=opened):
            with self.assertRaises(ControlPlaneBrokerDeniedError): await self.share()
        self.assertEqual(self.f.evidence.store.load(),self.initial)

    async def test_changed_fence_and_nonproducer_never_share(self):
        with self.assertRaises(ControlPlaneBrokerDeniedError): await self.share({**self.payload,'expected_fence':self.f.assignment.fence+1})
        def alter(state):
            index=next(i for i,r in enumerate(state.evidence) if r.id==self.proof.id)
            state.evidence[index]=state.evidence[index].model_copy(update={'producer_identity_id':self.f.admin.identity_id})
            return state
        self.f.evidence.store.update(alter)
        before=self.f.evidence.store.load()
        with self.assertRaises(ControlPlaneBrokerDeniedError): await self.share()
        self.assertEqual(self.f.evidence.store.load(),before)

    async def test_atomic_governance_failure_has_no_partial_accepted_records(self):
        register=self.f.evidence.governance.register_domain_record
        calls=[]
        def fail_second(*args,**kwargs):
            calls.append(1)
            if len(calls)==2: raise RuntimeError('private INFO governance boundary failure')
            return register(*args,**kwargs)
        before=self.f.evidence.governance.store.load()
        with patch.object(self.f.evidence.governance,'register_domain_record',side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError,'private INFO governance'): await self.share()
        self.assertEqual(self.f.evidence.store.load(),self.initial)
        self.assertEqual(self.f.evidence.governance.store.load(),before)
        result=await self.share()  # identical original identity after known atomic refusal
        self.assertFalse(result['deduplicated'])

    async def test_current_work_unknown_original_refuses_without_consuming_it(self):
        self.preflights.put(ExecutionPreflightAttempt(id='sharing-unknown',
            correlation_id='sharing-unknown',execution_id='original-unresolved',organization_id='local',
            workspace_id='default',project_id='project-a',thread_id='original-native-thread',message='Private retained original',
            queued_turn_snapshot={'work_item_ref':self.target_work.ref},status='reconciliation_required',created_at=1,updated_at=1))
        before=self.preflights.get('sharing-unknown')
        with self.assertRaisesRegex(ControlPlaneBrokerDeniedError,'Unresolved native original'): await self.share()
        self.assertEqual(self.f.evidence.store.load(),self.initial)
        self.assertEqual(self.preflights.get('sharing-unknown'),before)

    async def test_expired_current_lease_refuses_without_source_or_target_mutation(self):
        def expire(state):
            state.assignments=[a.model_copy(update={'lease':a.lease.model_copy(update={'expires_at':0})})
                if a.id==self.f.assignment.id else a for a in state.assignments]
            return state
        self.f.workers.store.update_assignment(self.f.worker.id,self.f.assignment.id,expire)
        with self.assertRaisesRegex(ControlPlaneBrokerDeniedError,'lease'): await self.share()
        self.assertEqual(self.f.evidence.store.load(),self.initial)

    async def test_profile_revocation_during_content_observation_refuses(self):
        from codex_web.agent_profiles import AgentProfileUpdate
        original=self.f.evidence.open_artifact_content
        def opened(*args,**kwargs):
            pointer,stream=original(*args,**kwargs)
            def changed():
                yield from stream
                self.f.agent_profiles.update('developer',AgentProfileUpdate(description='Private changed revision',
                    reason='Current Profile changed across content I/O'),actor=self.f.admin)
            return pointer,changed()
        with patch.object(self.f.evidence,'open_artifact_content',side_effect=opened):
            with self.assertRaises(ControlPlaneBrokerDeniedError): await self.share()
        self.assertEqual(self.f.evidence.store.load(),self.initial)

    async def test_operator_derivative_route_still_requires_administrator(self):
        with self.assertRaisesRegex(RuntimeError,'administrator'):
            self.f.evidence.create_derived_artifact(ArtifactCreate(project_id='project-a',
                work_item_ref=self.target_work.ref,resource_ids=(self.target.id,),artifact_type='report',
                name='Cannot use operator route',digest=sha256_digest(self.body)),actor=self.f.worker_actor,
                source_evidence_ids=(self.proof.id,),idempotency_key='unapproved-operator')
        self.assertEqual(self.f.evidence.store.load(),self.initial)
        low=self.f.admin.model_copy(update={'assurance':'primary'})
        with self.assertRaisesRegex(RuntimeError,'assurance'):
            self.f.evidence.create_derived_artifact(ArtifactCreate(project_id='project-a',
                work_item_ref=self.target_work.ref,resource_ids=(self.target.id,),artifact_type='report',
                name='Cannot bypass operator MFA',digest=sha256_digest(self.body)),actor=low,
                source_evidence_ids=(self.proof.id,),idempotency_key='unapproved-low-assurance')
        self.assertEqual(self.f.evidence.store.load(),self.initial)
