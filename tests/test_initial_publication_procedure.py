"""Real private GitLab ActionIntents, provider verification and durable steps.

Only the GitLab transport/push edge is controlled. Workspace attestation uses a
private committed Git checkout, and canonical intent/receipt/Evidence owners run.
No native/model/live HTTP or publication is exercised by this fixture.
"""
import copy
import json
from pathlib import Path
import subprocess
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from codex_web.action_intents import ActionIntentCreate, ActionIntentClaimRequest, ActionDecisionSnapshot
from codex_web.artifact_evidence import ArtifactCreate, sha256_digest
from codex_web.definitions import DefinitionReference
from codex_web.execution_workers import ExecutionAssignmentCreate, AssignmentClaimRequest, AssignmentStartRequest, AssignmentCompleteRequest
from codex_web.procedure_execution import ProcedureCheckpoint
from codex_web.resources import ResourceUpdate
from codex_web.services.code_host_action_contract import CODE_HOST_BRANCH_PUBLISH_ACTION_ID as PUBLISH
from codex_web.services.code_host_action_contract import CODE_HOST_PULL_REQUEST_OBSERVE_READINESS_ACTION_ID as OBSERVE
from codex_web.services.procedure_execution import ProcedureExecutionError
from codex_web.services.procedure_provider_results import CodeHostProcedureResultAdapter
from tests import test_procedure_code_host_results as fixtures
from tests import test_premerge_readiness_observation as read_fixture


def field(name, kind):
    return {'name':name, 'type':kind, 'description':'Canonical publication or exact-head observation fact; not QA consent'}


def initial_contract(resource, owner, provider_instance):
    publish_inputs = [field(n, 'string') for n in ('execution_workspace_id','branch','head_revision','expected_remote_revision')]
    outputs = [field(n, k) for n,k in (('expected_head_sha','string'), ('published_branch','string'),
        ('published_execution_workspace_id','string'), ('expected_remote_absent','boolean'))]
    observed = [field(n,k) for n,k in [('pull_request_number','integer'),('head_sha','string'),
        ('observation_succeeded','boolean'),('readiness','string'),('independent_review','string'),
        ('exact_head_ci','string'),('merge_policy_snapshot','string'),('blockers_json','string'),
        ('pull_request_state','string'),('draft','boolean'),('mergeable','string'),('merge_state','string'),
        ('observed_at','number'),('expires_at','number')]]
    def step(name, kind, action, inputs, outputs, capability, level):
        return {'id':name,'kind':kind,'description':'One canonical publication or read-only exact-head verification',
            'inputs':inputs,'outputs':outputs,'preconditions':[],
            'expected_evidence':[{'name':n,'kind':k,'description':'Original canonical proof'}
                for n,k in [('receipt','provider_receipt'),('verification','verification')]],
            'timeout_seconds':30,'max_attempts':1,'retry_backoff_seconds':0,
            'failure':{'outcome':'escalate','escalation_owner_identity_id':owner,
                'attention_reason':'Preserve original and reconcile UNKNOWN; no replay or QA inference'},
            'permissions':[{'capability':capability,'level':level,'resource_selector':'repository'}],
            'action':{'provider_type':'gitlab','provider_instance':provider_instance,'action_id':action,
                'provider_contract_version':'1.2','resource_selector':'repository'}}
    publish = step('publish','mutation',PUBLISH,publish_inputs,outputs,'repository.branch.publish','execute')
    publish['impact']={'resource_selector':'repository','change':'configuration','description':'One remote branch compare-and-swap',
        'max_unavailable_seconds':0,'reversible':False}
    publish['verification_step_id']='observe-published-head'
    observe = step('observe-published-head','verification',OBSERVE,
        [field('pull_request_number','integer'),field('expected_head_sha','string')],observed,
        'repository.read.metadata','read')
    observe['preconditions']=[{'source':'step','name':'publish','predicate':'succeeded'}]
    return {'schema_version':'1.4','owner_identity_id':owner,'task_triggers':['workspace-initial-publication'],
        'operation_triggers':['repository.branch.publish'],
        'resource_selectors':[{'id':'repository','resource_types':['repository'],'resource_ids':[resource]}],
        'environments':['development'],'inputs':[*publish_inputs,field('pull_request_number','integer')],
        'reviewed_at':1,'review_due_at':4102444800,'expires_at':4102444801,
        'required_provider_capabilities':[],'required_worker_capabilities':[],
        'conflict_group':'workspace-initial-publication','precedence':100,'max_duration_seconds':120,
        'steps':[publish,observe]}


class InitialPublicationProcedureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = f = fixtures.ProcedureCodeHostResultTests()
        await f.asyncSetUp()
        self.addAsyncCleanup(f.asyncTearDown)
        workers = f.intents.execution_workers
        old = workers.store.assignment(f.assignment.id)
        workers.complete(old.assigned_worker_id, old.id, AssignmentCompleteRequest(
            lease_token=old.lease.lease_token,fence=old.fence,succeeded=False,
            failure_code='private_fixture_superseded'),actor=f.worker)
        f.repository = f.resources.update(f.repository.id, ResourceUpdate(
            operational_context={'environment':'development'}), actor=f.actor)
        # Use one canonical workspace owner for assignment validation and provider
        # attestation; the borrowed provider fixture only supplies typed seed rows.
        from codex_web.execution_workspace_backend import LocalGitWorkspaceBackend
        from codex_web.execution_workspaces import ExecutionWorkspaceState
        from codex_web.models import Project
        from codex_web.services.execution_workspaces import ExecutionWorkspaceService
        from codex_web.storage.execution_workspaces import ExecutionWorkspaceStateStore
        seed = f.provider.contract.workspaces.store.load()
        workspace = seed.workspaces[0]
        store = ExecutionWorkspaceStateStore(f.sqlite)
        store.update(lambda _: ExecutionWorkspaceState(
            workspaces=seed.workspaces, leases=seed.leases))
        project = Project(id=workspace.project_id, name='Private publication',
            path=workspace.path, organization_id=f.actor.organization_id,
            workspace_id=f.actor.workspace_id)
        def project_lookup(project_id):
            self.assertEqual(project_id, project.id)
            return project
        workspaces = ExecutionWorkspaceService(store,
            LocalGitWorkspaceBackend(f.provider.contract.workspaces.backend.root),
            f.resources, project_lookup)
        f.provider.contract.workspaces = workers.workspaces = workspaces
        self.assertEqual(workspaces.get(workspace.id, f.actor), workspace)
        self.base_head = f.head
        path = Path(workspace.path)
        (path/'delivery.txt').write_text('reviewed correction\n')
        subprocess.run(['git','add','delivery.txt'],cwd=path,check=True,capture_output=True)
        subprocess.run(['git','commit','-m','Private governed correction'],cwd=path,check=True,capture_output=True)
        f.head = subprocess.check_output(['git','rev-parse','HEAD'],cwd=path,text=True).strip()
        f.client.branches[f.branch] = self.base_head
        assignment = workers.create_assignment(ExecutionAssignmentCreate(work_item_ref='WI-1',
            execution_id=workspace.execution_id,project_id=workspace.project_id,
            resource_ids=(f.repository.id,),execution_workspace_id=workspace.id,
            repository_scope={'organization_id':f.actor.organization_id,'workspace_id':f.actor.workspace_id,
                'project_id':workspace.project_id,'writable_repository_ids':[f.repository.id],'source':'explicit'},
            required_capabilities=('command_execution',),execution_contract_version='1.0',
            secret_refs=(f.binding.credential_ref,)),actor=f.actor)
        claimed = workers.claim(old.assigned_worker_id,AssignmentClaimRequest(lease_seconds=300),
            actor=f.worker,assignment_id=assignment.id)
        self.assignment = f.assignment = workers.start(old.assigned_worker_id,assignment.id,
            AssignmentStartRequest(fence=claimed.fence,lease_token=claimed.lease.lease_token),actor=f.worker)
        # Canonical keyed Work records back both attribution and Evidence guards;
        # this fixture never requests a Work success/owner/stage transition.
        from codex_web.models import WorkItemState
        from codex_web.storage.runtime_state import ModelMapRepository
        items = ModelMapRepository(f.sqlite, namespace='work_item_states',
            legacy_path=Path(f.temp.name)/'private-publication-work.json', model=WorkItemState)
        now = time.time()
        work = WorkItemState(ref=workspace.work_item_ref,
            organization_id=workspace.organization_id, workspace_id=workspace.workspace_id,
            project_id=workspace.project_id, resource_ids=list(workspace.resource_ids),
            project_path=f.provider.contract.locator(f._request(PUBLISH, {})),
            current_owner=f.actor.identity_id, current_stage='implementation_active',
            artifact_state='branch', created_at=now, updated_at=now, last_meaningful_update_at=now)
        items.put(work.ref, work)
        host = SimpleNamespace(_load_work_item_states=items.load,
            _get_work_item_state_record=items.get, _save_work_item_states=items.save)
        f.intents.work_item_host = f.evidence.work_item_host = host
        self.work_items = items
        self.assertEqual(items.get(work.ref), work)
        self.workers = workers
        f.runner.workers = workers
        # This initial pure boundary fixture has no active admission mediator.
        # prepare_durable below composes the real current published step path.
        pin = ProcedureCheckpoint(definition=DefinitionReference(definition_id='private-publication',kind='agent.skill',
            revision=1,record_id='private-publication',checksum='c'*64,definition_schema_version='1.0'),
            actor_identity_id=f.actor.identity_id,input_snapshot_id='unused',input_snapshot_checksum='d'*64,
            resource_context_checksum='private',started_at=1,deadline_at=4102444800,steps={})
        f.assignment = self.assignment.model_copy(update={'procedure_checkpoint':pin})
        f.step = SimpleNamespace(id='publish',worker=None,judgment=None,
            action=SimpleNamespace(provider_type='gitlab',provider_instance='gitlab.example',action_id=PUBLISH,provider_contract_version='1.2'),
            outputs=[SimpleNamespace(name=n,type=k,required=True) for n,k in [('expected_head_sha','string'),
                ('published_branch','string'),('published_execution_workspace_id','string'),('expected_remote_absent','boolean')]],
            expected_evidence=[SimpleNamespace(name='receipt',kind='provider_receipt'),SimpleNamespace(name='verification',kind='verification')])
        self.pushes = 0
        publisher = f.provider.branch_publisher
        async def publish(*args):
            self.pushes += 1
            await publisher(*args)
            if hasattr(f,'mr'):
                f.mr['sha'] = args[3]
        f.provider.branch_publisher = publish

    async def original(self):
        f = self.f
        request = f._request(PUBLISH,{'execution_workspace_id':'workspace-a','branch':f.branch,
            'head_revision':f.head,'expected_remote_revision':self.base_head})
        request.project_id = self.assignment.project_id
        request.idempotency_key = f.runner._step_origin(f.assignment,'publish',intent=True)
        intent = f.intents.create(ActionIntentCreate(binding_id=f.binding.id,
            execution_id=self.assignment.execution_id,work_item_ref=self.assignment.work_item_ref,request=request,
            policy_decision=ActionDecisionSnapshot(outcome='allow',source='policy:private-canonical-publication')),
            actor=f.actor)
        f.intents.claim(ActionIntentClaimRequest(worker_id='private-publication-worker'),intent_id=intent.id,actor=f.worker)
        intent = await f.intents.execute_claimed(intent.id,'private-publication-worker',actor=f.worker)
        self.assertEqual(intent.status,'succeeded',intent.last_error)
        history = f.intents.store.load(intent_id=intent.id)
        receipt = next(r for r in history.receipts if r.id==intent.last_receipt_id)
        self.assertTrue(history.verifications[-1].verified)
        self.assertTrue(history.verifications[-1].provider_verification.verified)
        return intent,receipt,history

    async def test_original_branch_receipt_enters_bounded_procedure_envelope(self):
        f = self.f
        intent,receipt,history = await self.original()
        raw = copy.deepcopy(receipt.result.output)
        result = f.project(intent,receipt,history)
        self.assertEqual(set(result),{'outputs','evidence'},'Actual branch publication receipt lacks procedure result projection')
        self.assertEqual(result['outputs'],{'expected_head_sha':f.head,'published_branch':f.branch,
            'published_execution_workspace_id':'workspace-a','expected_remote_absent':False})
        self.assertEqual(set(result['evidence']),{'receipt','verification'})
        for name,eid in result['evidence'].items():
            proof=f.evidence.get_evidence(eid,actor=f.actor)
            self.assertEqual(proof.source,'action-intent:'+intent.id)
            self.assertEqual(proof.metadata['receipt_id'],receipt.id)
            self.assertEqual(proof.execution_id,self.assignment.execution_id)
            if name=='verification':self.assertEqual(proof.metadata['verification_id'],intent.last_verification_id)
        self.assertEqual(receipt.result.output,raw)
        self.assertEqual(self.pushes,1)
        self.assertEqual(f.published[5],self.base_head)
        self.assertEqual(f.project(intent,receipt,history),result)
        self.assertEqual(self.pushes,1)

    async def test_missing_or_failed_original_verification_cannot_be_projected(self):
        f=self.f;intent,receipt,history=await self.original()
        for verifications in ([],[v.model_copy(update={'verified':False}) for v in history.verifications]):
            with self.subTest(present=bool(verifications)):
                with self.assertRaisesRegex(ProcedureExecutionError,'verification_missing'):
                    f.project(intent,receipt,history.model_copy(update={'verifications':verifications}))
        self.assertEqual(self.pushes,1)

    async def test_receipt_head_branch_workspace_scope_and_absence_are_exact(self):
        f=self.f;intent,receipt,history=await self.original()
        for update in ({'head_revision':'e'*40},{'branch':'other-branch'},{'execution_workspace_id':'other'},
                {'resource_id':'other'},{'expected_remote_absent':True},{'expected_remote_absent':0},
                {'existing_change_request_number':33}):
            with self.subTest(update=tuple(update)):
                changed=receipt.model_copy(deep=True)
                changed.result=changed.result.model_copy(update={'output':{**changed.result.output,**update}})
                with self.assertRaisesRegex(ProcedureExecutionError,'projection_invalid'):
                    f.project(intent,changed,history)
        self.assertEqual(self.pushes,1)

    async def test_missing_branch_evidence_or_wrong_external_head_refuses(self):
        f=self.f;intent,receipt,history=await self.original()
        for update in ({'evidence':()},{'external_id':'f'*40}):
            with self.subTest(update=tuple(update)):
                changed=receipt.model_copy(deep=True)
                changed.result=changed.result.model_copy(update=update)
                with self.assertRaisesRegex(ProcedureExecutionError,'projection_invalid'):
                    f.project(intent,changed,history)
        self.assertEqual(self.pushes,1)

    async def test_retired_workspace_does_not_reinterpret_immutable_receipt(self):
        f=self.f;intent,receipt,history=await self.original()
        from codex_web.execution_workspaces import ExecutionWorkspaceRelease
        workspaces=f.provider.contract.workspaces
        workspaces.release('workspace-a', ExecutionWorkspaceRelease(reason='Private receipt remains immutable'),
            actor=f.actor, preserve_files=True)
        self.assertEqual(workspaces.get('workspace-a', f.actor).status, 'released')
        self.assertIsNotNone(workspaces.store.lease('lease-a').released_at)
        result=f.project(intent,receipt,history)
        self.assertEqual(result['outputs']['expected_head_sha'],f.head)
        self.assertEqual(self.pushes,1)

    async def prepare_durable(self, *, include_recovery=False):
        from codex_web.services.definitions import DefinitionRegistryService
        from codex_web.storage.definition_registry import DefinitionRegistryStore
        from codex_web.services.skills import SkillService
        from codex_web.services.skill_scanners import SkillScannerRegistry
        from codex_web.services.skill_security import SkillSecurityService
        from codex_web.storage.skill_security import SkillSecurityStore
        from codex_web.services.procedure_discovery import ProcedureDiscoveryService, ProcedureExecutionCapabilityService
        from codex_web.services.canonical_events import CanonicalEventBus, CanonicalEventIngestionService
        from codex_web.storage.canonical_events import CanonicalEventStore
        from codex_web.services.approval_requests import ApprovalRequestService
        from codex_web.storage.approval_requests import ApprovalRequestStore
        from codex_web.services.attention import AttentionService
        from codex_web.storage.attention import AttentionStore
        from codex_web.skills import SkillCreate,SkillPublish
        from codex_web.integrations.gitlab_client import GitLabClient
        from tests.test_skill_security import FakeScanner
        f=self.f
        registry=DefinitionRegistryService(DefinitionRegistryStore(f.sqlite))
        skills=SkillService(registry)
        skills.bind_procedure_contracts(actions=f.registry,resources=f.resources,scope_actor=lambda _:f.actor)
        scanners=SkillScannerRegistry();scanners.register(FakeScanner())
        skills.bind_security(SkillSecurityService(SkillSecurityStore(f.sqlite),registry,scanners))
        draft=skills.create(SkillCreate(skill_id='private-initial-publication',name='Private initial publication',
            instructions='Use the current durable publication step; preserve all originals and do not infer QA.',
            operational_procedure=initial_contract(f.repository.id,f.actor.identity_id,'gitlab.example')),actor=f.actor)
        published=skills.publish(draft['skillId'],draft['recordId'],SkillPublish(),actor=f.actor)
        self.pin=DefinitionReference.model_validate(published['definitionReference'])
        self.recovery_pin=None
        if include_recovery:
            from codex_web.procedure_control_plane import CanonicalOperationRegistry
            from codex_web.services.procedure_publication_recovery import PublicationRecoveryOperations,register_publication_operations
            control=CanonicalOperationRegistry()
            register_publication_operations(control,PublicationRecoveryOperations(workers=self.workers,
                workspaces=f.provider.contract.workspaces,intents=f.intents,work_items=None))
            skills.bind_procedure_contracts(actions=f.registry,resources=f.resources,
                scope_actor=lambda _:f.actor,control_plane=control)
            body=json.loads((Path(__file__).resolve().parents[1]/'examples/procedures/workspace-publication-recovery.INACTIVE.json').read_text())['payload']
            recovery=body['operational_procedure']
            recovery.update(owner_identity_id=f.actor.identity_id,reviewed_at=1,review_due_at=4102444800,
                expires_at=4102444801,environments=['development'],
                resource_selectors=[{'id':'repository','resource_types':['repository'],'resource_ids':[f.repository.id]}])
            for declared in recovery['steps']:
                declared['failure']['escalation_owner_identity_id']=f.actor.identity_id
            draft=skills.create(SkillCreate(skill_id='private-original-recovery',name='Private original recovery',
                instructions='Preserve original intent; recovery never creates a new initial publication.',
                operational_procedure=recovery),actor=f.actor)
            published=skills.publish(draft['skillId'],draft['recordId'],SkillPublish(),actor=f.actor)
            self.recovery_pin=DefinitionReference.model_validate(published['definitionReference'])
        events=CanonicalEventIngestionService(CanonicalEventBus(CanonicalEventStore(f.sqlite)))
        discovery=ProcedureDiscoveryService(skills,f.resources,max_context_characters=43000)
        discovery.execution_capabilities=ProcedureExecutionCapabilityService(self.workers,None,None)
        runner=f.runner
        runner.skills,runner.discovery=skills,discovery
        runner.approvals=ApprovalRequestService(ApprovalRequestStore(f.sqlite),f.identity,events,authority_roles=runner.authority)
        runner.attention=AttentionService(AttentionStore(f.sqlite),events,identity=f.identity)
        runner.worker_actor=f.worker
        runner.models=SimpleNamespace(invoke=AsyncMock(side_effect=AssertionError('No model calls')))
        f.intents.procedure_execution=runner;f.intents.procedure_discovery=discovery
        f.execution.procedure_admission=f.intents.procedure_provider_admission
        # Existing read-only MR adapter sees the new head after the one simulated
        # push. It still reports unavailable reviews/CI honestly, not QA consent.
        f.expected_fixture_credential='secret-gitlab-token'
        f.observed_calls=[];f.hook=None;f.approvals_status=None
        f.mr=dict(iid=33,project_id=42,sha=self.base_head,target_branch='main',state='opened',draft=False,
            detailed_merge_status='mergeable',author={'id':1},blocking_discussions_resolved=True,
            head_pipeline={'id':9,'sha':self.base_head,'status':'success'})
        f.approvals_fact=dict(approved=False,approved_by=[],approvals_required=1,approvals_left=1)
        def respond(request):
            if request.url.path.endswith('/projects/group/platform/codex-web'):
                self.assertEqual(request.method,'GET')
                self.assertEqual(request.headers['PRIVATE-TOKEN'],f.expected_fixture_credential)
                f.observed_calls.append(request.url.path)
                return httpx.Response(200,json={'id':42,'path_with_namespace':'group/platform/codex-web'})
            return read_fixture.PremergeReadinessProcedureTests.respond(f,request)
        http=GitLabClient(transport=httpx.MockTransport(respond))
        f.client.read_json_bounded=http.read_json_bounded
        values={'execution_workspace_id':'workspace-a','branch':f.branch,'head_revision':f.head,
            'expected_remote_revision':self.base_head,'pull_request_number':33}
        body=json.dumps(values,sort_keys=True).encode()
        artifact=f.evidence.create_artifact(ArtifactCreate(project_id=self.assignment.project_id,
            work_item_ref=self.assignment.work_item_ref,execution_id=self.assignment.execution_id,
            resource_ids=self.assignment.resource_ids,artifact_type='report',name='Canonical private publication inputs',
            digest=sha256_digest(body)),actor=f.actor)
        f.evidence.attach_artifact_content(artifact.id,(body,),actor=f.actor,media_type='application/json')
        started=runner.start(self.assignment.id,definition=self.pin,input_snapshot_id=artifact.id,actor=f.actor)
        return runner,started

    async def test_current_durable_publication_then_readonly_head_verification_has_one_effect(self):
        runner,started=await self.prepare_durable();f=self.f
        first=await runner.advance(started.id,actor=f.actor,run_id=started.procedure_checkpoint.id)
        state=first.procedure_checkpoint.steps['publish']
        self.assertEqual(state.status,'succeeded',first.procedure_checkpoint.failure_code)
        self.assertEqual(self.pushes,1)
        original=f.intents.store.load(intent_id=state.action_intent_id)
        self.assertEqual(len(original.receipts),1)
        self.assertTrue(original.verifications[-1].provider_verification.verified)
        second=await runner.advance(started.id,actor=f.actor,run_id=started.procedure_checkpoint.id)
        self.assertEqual(second.procedure_checkpoint.steps['observe-published-head'].status,'succeeded',second.procedure_checkpoint.failure_code)
        _,stream=f.evidence.open_artifact_content(second.procedure_checkpoint.steps['observe-published-head'].output_snapshot_id,actor=f.actor)
        observed=json.loads(b''.join(stream))
        self.assertEqual(observed['head_sha'],f.head)
        self.assertNotEqual(observed['readiness'],'requirements_observed_satisfied')
        completed=await runner.advance(started.id,actor=f.actor,run_id=started.procedure_checkpoint.id)
        self.assertEqual(completed.procedure_checkpoint.status,'completed',completed.procedure_checkpoint.failure_code)
        self.assertEqual(self.pushes,1)
        self.assertEqual(len(f.intents.store.load(intent_id=state.action_intent_id).receipts),1)
        runner.models.invoke.assert_not_called()

    async def test_initial_and_recovery_groups_are_selected_without_shadowing_or_ambiguity(self):
        runner,started=await self.prepare_durable(include_recovery=True)
        discovery=started.procedure_discoveries[-1]
        self.assertEqual(set(discovery.selected),{self.pin,self.recovery_pin})
        self.assertEqual(discovery.outcome,'matched')
        self.assertLessEqual(discovery.instruction_characters,43000)
        current=await runner.advance(started.id,actor=self.f.actor,run_id=started.procedure_checkpoint.id)
        self.assertEqual(current.procedure_checkpoint.steps['publish'].status,'succeeded',current.procedure_checkpoint.failure_code)
        self.assertEqual(self.pushes,1)
        self.assertEqual(current.procedure_checkpoint.definition,self.pin)

    async def test_unmediated_request_cannot_borrow_started_but_not_running_step(self):
        await self.prepare_durable()
        with self.assertRaisesRegex(ProcedureExecutionError,'procedure_action_creation_not_current'):
            await self.original()
        self.assertEqual(self.pushes,0)

    async def test_other_origin_or_attempt_cannot_relabel_original_publication(self):
        f=self.f;intent,receipt,history=await self.original()
        for bad_intent,bad_receipt in ((intent.model_copy(update={'execution_id':'foreign'}),receipt),
                (intent.model_copy(update={'status':'uncertain'}),receipt),
                (intent,receipt.model_copy(update={'attempt':receipt.attempt+1})),
                (intent,receipt.model_copy(update={'correlation_id':'foreign'}))):
            with self.assertRaisesRegex(ProcedureExecutionError,'origin_mismatch'):
                f.project(bad_intent,bad_receipt,history)
        self.assertEqual(self.pushes,1)

    async def test_pure_projection_preserves_existing_full_revision_contract(self):
        f=self.f;intent,receipt,_=await self.original()
        adapter=CodeHostProcedureResultAdapter(provider_instance='gitlab.example')
        for size in (40,64):
            head='e'*size
            request=intent.request.model_copy(deep=True)
            request.parameters={**request.parameters,'head_revision':' '+head+' '}
            result=receipt.result.model_copy(update={'external_id':head,
                'output':{**receipt.result.output,'head_revision':head}})
            self.assertEqual(adapter.project(request,result)['expected_head_sha'],head)
        self.assertEqual(self.pushes,1)
