"""Real historical readers and canonical administrative broker recovery, offline."""
import builtins
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from urllib.parse import quote

from pydantic import ValidationError
from codex_web.definitions import DefinitionRecord, definition_checksum
from codex_web.execution_profiles import validate_execution_profile_catalog
from codex_web.execution_workers import (AssignmentCancelRequest, AssignmentClaimRequest,
    AssignmentStartRequest, ExecutionAssignmentCreate)
from codex_web.services.control_plane_broker import AssignmentBoundControlPlaneBroker
from codex_web.resources import ResourceType
from tests import test_administrative_broker_runtime as administrative_fixture

FROZEN = Path(__file__).parent/'fixtures/compatibility'
SHA = {'execution_profiles_v1.py':'5bfd443dfeb32cf2957dd2e752d7e3ce21ccd79d1c562e32ed38d3f1c04c9e1e',
       'execution_profile_seed_v1.py':'9a881a620b38895dcdcfeaa638564b8f2ca32e2216750413794fd406d981ea75',
       'execution_profile_seed_v1.json':'4f3ab583be6c2c0bc9268474225f42f23e777d176adfd8a4843f44fbd88197e9'}


def frozen_reader():
    for name,digest in SHA.items():
        if hashlib.sha256((FROZEN/name).read_bytes()).hexdigest()!=digest:
            raise AssertionError('Frozen original reader/seed bytes changed: '+name)
    name='tests._original_execution_profiles_v1'
    spec=importlib.util.spec_from_file_location(name,FROZEN/'execution_profiles_v1.py')
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
    return module


class OriginalExecutionProfileReaderTests(unittest.TestCase):
    def test_actual_original_seed_and_checksum_survive_current_reader(self):
        old=frozen_reader()
        self.addCleanup(sys.modules.pop,old.__name__,None)
        def legacy_import(name,globals=None,locals=None,fromlist=(),level=0):
            if name=='codex_web.execution_profiles':return old
            return builtins.__import__(name,globals,locals,fromlist,level)
        namespace={'__builtins__':{**vars(builtins),'__import__':legacy_import},'__name__':'original_seed'}
        path=FROZEN/'execution_profile_seed_v1.py'
        exec(compile(path.read_text(),str(path),'exec'),namespace)
        original=namespace['execution_profile_catalog_seed_payload']()
        self.assertEqual(original,json.loads((FROZEN/'execution_profile_seed_v1.json').read_text()))
        self.assertEqual(old.validate_execution_profile_catalog(original),original)
        self.assertEqual(validate_execution_profile_catalog(original),original)
        checksum='587b284005d3016fb3e962425a2aef7b2f7ecbab930262d512514be1c507246a'
        self.assertEqual(definition_checksum(definition_id='execution.profiles.default',
            kind='execution-profile-catalog',definition_schema_version='1.0',payload=original),checksum)
        record=DefinitionRecord(definition_id='execution.profiles.default',kind='execution-profile-catalog',
            definition_schema_version='1.0',revision=1,payload=original,checksum=checksum,created_by='original-fixture')
        self.assertEqual(DefinitionRecord.model_validate_json(record.model_dump_json()).payload,original)
        self.assertEqual(record.checksum,checksum)


class _UpsertProvider(administrative_fixture._AdministrativeProvider):
    """Only the external effect is synthetic; canonical intent/receipts are real."""
    def actions(self):
        return (super().actions()[0].model_copy(update={'action_id':'code-host.pull-request.upsert',
            'required_resource_types':(ResourceType.REPOSITORY,),
            'required_authority':('repository.pull-request.create',)}),)


class AdministrativeRecoveryQualificationTests(unittest.IsolatedAsyncioTestCase):
    async def fixture(self,operations):
        f=administrative_fixture.AdministrativeBrokerRuntimeTests()
        f._publish_worker_authority=lambda **kwargs: administrative_fixture.AdministrativeBrokerRuntimeTests._publish_worker_authority(
            f,repository_evidence_read=True)
        await f.asyncSetUp(operations=operations)
        self.addAsyncCleanup(f.asyncTearDown)
        return f

    async def fresh_assignment(self,f):
        await f.broker.stop()
        f.workers.cancel_bootstrap(f.assignment.id,AssignmentCancelRequest(expected_fence=f.assignment.fence,
            reason='Terminal original fixture custody; retain every intent and receipt'),actor=f.admin)
        old=f.assignment
        new=f.workers.create_assignment(ExecutionAssignmentCreate(subject=old.subject,
            execution_id=old.execution_id+'-replacement',project_id=old.project_id,resource_ids=(),
            execution_contract_version=old.execution_contract_version,required_capabilities=old.required_capabilities,
            sandbox=old.sandbox,approval_policy=old.approval_policy,execution_profile_id=old.execution_profile_id,
            execution_profile_definition=old.execution_profile_definition,agent_profile=old.agent_profile),actor=f.admin)
        claimed=f.workers.claim(f.worker.id,AssignmentClaimRequest(),actor=f.worker_actor,assignment_id=new.id)
        f.assignment=f.current_assignment=f.workers.start(f.worker.id,new.id,
            AssignmentStartRequest(fence=claimed.fence,lease_token=claimed.lease.lease_token),actor=f.worker_actor)
        f.broker=AssignmentBoundControlPlaneBroker(f.service,assignment=f.assignment,worker_id=f.worker.id,
            worker_actor=f.worker_actor,fence=f.assignment.fence,
            validator=lambda:f.workers.assignment_for_execution(f.assignment.execution_id,actor=f.admin))
        await f.broker.start()
        return old

    async def test_actual_published_project_v11_is_rejected_by_frozen_v10_reader(self):
        f=await self.fixture(('repository.issue.create',))
        record=f.profiles.record(organization_id=f.admin.organization_id,
            workspace_id=f.admin.workspace_id,project_id='project-a')
        self.assertEqual(record.definition_schema_version,'1.1')
        self.assertEqual(record.lifecycle.value,'published')
        old=frozen_reader();self.addCleanup(sys.modules.pop,old.__name__,None)
        before=record.model_dump(mode='json')
        with self.assertRaises(ValidationError) as refused:old.validate_execution_profile_catalog(record.payload)
        errors=refused.exception.errors()
        self.assertTrue(any(x['type']=='extra_forbidden' and x['loc'][-1]=='administrative_repository_ids' for x in errors))
        self.assertEqual(record.model_dump(mode='json'),before)
        self.assertEqual(f.profiles.record(project_id='project-a').record_id,record.record_id)

    async def test_fresh_admin_reads_actual_historical_upsert_receipt_without_mutation(self):
        f=await self.fixture(('repository.pull-request.upsert','repository.read.delivery-evidence','action_intent.reconcile'))
        provider=_UpsertProvider();f.intents.execution.registry.register(provider)
        status,_,body=await f._request('POST','/api/repository-actions/pull-request/upsert?repository_resource_id='+f.target.id,
            payload={'parameters':{'key':'original-prerequisite','value':'fixture publication'},'idempotency_key':'original-upsert'})
        self.assertEqual(status,200,body);intent=body['item']
        self.assertEqual(intent['action_id'],'code-host.pull-request.upsert')
        self.assertEqual(intent['status'],'succeeded')
        before=f.intents.history(intent['id'],f.admin)
        self.assertEqual(len(before['receipts']),1)
        self.assertTrue(before['verifications'][0]['verified'])
        origin=await self.fresh_assignment(f)
        self.assertNotEqual(origin.id,f.assignment.id)
        self.assertNotEqual(origin.execution_id,f.assignment.execution_id)
        path='/api/action-intents/'+intent['id']+'/delivery-evidence?project_id=project-a&repository_resource_id='+f.target.id
        status,_,observed=await f._request('GET',path)
        self.assertEqual(status,200,observed)
        self.assertEqual(observed['intent']['execution_id'],origin.execution_id)
        self.assertEqual(observed['receipts'][0]['id'],before['receipts'][0]['id'])
        self.assertEqual(observed['verifications'][0]['id'],before['verifications'][0]['id'])
        self.assertTrue(observed['verifications'][0]['verified'])
        self.assertEqual(f.intents.history(intent['id'],f.admin),before)
        self.assertEqual(provider.calls,1)
        status,_,denied=await f._request('POST','/api/action-intents/'+intent['id']+'/reconcile?repository_resource_id='+f.target.id,
            payload={'retry_if_idempotent':True})
        self.assertEqual(status,403,denied)
        self.assertEqual(f.intents.history(intent['id'],f.admin),before)
        self.assertEqual(provider.calls,1)

    async def test_admin_socket_commits_real_graph_and_handoff_with_scope_refusals(self):
        from codex_web.models import WorkItemState
        from codex_web.services.work_item_dependencies import WorkItemRuntimeDependencies
        from codex_web.services.work_item_state import WorkItemStateMachine
        from codex_web.services.work_items import WorkItemService
        from codex_web.services.work_graph import WorkGraphService
        from codex_web.storage.runtime_state import ModelMapRepository
        from codex_web.storage.work_graph import WorkGraphStore
        from codex_web.storage.sqlite_state import SQLiteStateStore
        from codex_web.agent_profiles import AgentProfileUpdate
        f=await self.fixture(('work_graph.edge.create','work_item.handoff'))
        root=Path(f.temp.name)
        items=ModelMapRepository(f.sqlite,namespace='work_item_states',
            legacy_path=root/'private-items.json',model=WorkItemState)
        source=WorkItemState(ref=f.ref,organization_id='local',workspace_id='default',
            project_id='project-a',current_owner='orchestrator',next_owner='orchestrator',
            current_stage='failed_with_action_owner',implementation_owner='james',
            validation_owner='quinn',resource_ids=[f.target.id],
            next_action='Resolve the prerequisite before release',blocker='Retained release hold',
            blocking_findings=['Keep the independent release hold'],release_gate=True,
            created_at=1000,updated_at=1000,last_meaningful_update_at=1000)
        target=source.model_copy(deep=True,update={'ref':'group/app#43'})
        foreign_project=source.model_copy(deep=True,update={'ref':'group/other#7','project_id':'project-b'})
        foreign_tenant=source.model_copy(deep=True,update={'ref':'foreign/app#9','organization_id':'other'})
        for item in (source,target,foreign_project,foreign_tenant):items.put(item.ref,item)
        host=SimpleNamespace(DATA_DIR=root,WORK_ITEM_EVENTS_FILE=root/'private-work-events.jsonl',
            _load_work_item_states=items.load,_save_work_item_states=items.save,
            _get_work_item_state_record=items.get,_put_work_item_state_record=lambda item:items.put(item.ref,item),
            _load_projects=lambda:[SimpleNamespace(id='project-a',organization_id='local',workspace_id='default')],
            _resource_ids_for_project=lambda *args,**kwargs:[f.target.id])
        dependencies=WorkItemRuntimeDependencies.from_host(host)
        machine=WorkItemStateMachine(store=f.sqlite,dependencies=dependencies)
        dispatches=[]
        def record(*args,**kwargs):dispatches.append((args,kwargs))
        continuity=SimpleNamespace(schedule_structured_handoff_dispatch=record,schedule_handoff_continuity_check=record)
        f.service.work_items=WorkItemService(host,state_machine=machine,
            work_item_dependencies=dependencies,continuity=continuity,recovery=SimpleNamespace(schedule=record))
        graph=WorkGraphService(WorkGraphStore(f.sqlite),items.load)
        f.service.work_graph=graph
        payload={'relation':'blocks','source_ref':source.ref,'target_ref':target.ref,
                 'failure_behavior':'pause','reason':'Typed original prerequisite'}
        status,_,body=await f._request('POST','/api/work-graph/edges',payload=payload)
        self.assertEqual(status,200,body)
        edge=body['edge']
        self.assertEqual((edge['source_ref'],edge['target_ref'],edge['relation']),
            (source.ref,target.ref,'blocks'))
        reader=SQLiteStateStore(f.sqlite.path)
        persisted=WorkGraphStore(reader).load()
        self.assertEqual(len(persisted.edges),1)
        self.assertEqual(persisted.edges[0].id,edge['id'])
        self.assertEqual(persisted.events[0].actor_id,f.admin.identity_id)
        status,_,again=await f._request('POST','/api/work-graph/edges',payload=payload)
        self.assertEqual(status,200,again)
        self.assertEqual(WorkGraphStore(reader).load(),persisted)
        for denied in (foreign_project,foreign_tenant):
            status,_,body=await f._request('POST','/api/work-graph/edges',payload={**payload,'target_ref':denied.ref})
            self.assertEqual(status,403,body)
            self.assertEqual(WorkGraphStore(reader).load(),persisted)
        handoff={'from_agent':'orchestrator','to_agent':'james','reason':'Actual canonical prerequisite handoff',
            'expected_action':'Accept the scoped prerequisite',
            'blocker':source.blocker,'blocking_findings':source.blocking_findings}
        path='/api/work-items/'+quote(source.ref,safe='')+'/handoff'
        status,_,body=await f._request('POST',path,payload=handoff)
        self.assertEqual(status,200,body)
        recovered=ModelMapRepository(reader,namespace='work_item_states',legacy_path=root/'reader-unused.json',model=WorkItemState).get(source.ref)
        self.assertEqual((recovered.handoff.from_agent,recovered.handoff.to_agent,recovered.handoff.status),
            ('orchestrator','james','pending'))
        self.assertEqual(recovered.handoff_history[-1].requested_at,recovered.handoff.requested_at)
        self.assertEqual(recovered.blocking_findings,source.blocking_findings)
        self.assertEqual(recovered.blocker,source.blocker)
        self.assertTrue(recovered.release_gate)
        self.assertEqual(len(dispatches),2)
        before=items.load()
        f.agent_profiles.update('orchestrator',AgentProfileUpdate(description='Changed exact profile',
            reason='Synthetic canonical profile revocation'),actor=f.admin)
        status,_,body=await f._request('POST',path,payload=handoff)
        self.assertEqual(status,403,body)
        self.assertEqual(items.load(),before)
        self.assertEqual(WorkGraphStore(reader).load(),persisted)
        self.assertEqual(len(dispatches),2)

    async def test_fresh_admin_settles_uncertain_original_merge_by_reads_only(self):
        from tests import test_action_intents as intent_fixture
        from codex_web.action_providers import ActionProviderBindingCreate
        class MergeProvider(intent_fixture._MergeReconciliationProvider):
            def actions(self):
                return (super().actions()[0].model_copy(update={
                    'required_resource_types':(ResourceType.REPOSITORY,),
                    'required_authority':('repository.pull-request.merge',)}),)
        f=await self.fixture(('repository.pull-request.merge','repository.read.delivery-evidence','action_intent.reconcile'))
        provider=MergeProvider()
        f.intents.execution.registry.register(provider)
        f.intents.execution.registry.bind(ActionProviderBindingCreate(provider_type=provider.provider_type,
            provider_instance=provider.provider_instance,project_id='project-a',resource_ids=(f.target.id,)),
            actor=f.admin,resources=f.resources)
        status,_,body=await f._request('POST','/api/repository-actions/pull-request/merge?repository_resource_id='+f.target.id,
            payload={'parameters':{'pull_request_number':42,'merge_method':'squash','expected_head_sha':'a'*40},
                     'idempotency_key':'original-merge'})
        self.assertEqual(status,200,body)
        intent=body['item'];self.assertEqual(intent['status'],'uncertain')
        self.assertEqual(provider.execute_calls,1)
        history=f.intents.history(intent['id'],f.admin)
        self.assertEqual(history['receipts'][-1]['outcome'],'unknown')
        prepares_before=provider.prepare_calls
        original=await self.fresh_assignment(f)
        self.assertEqual(intent['execution_id'],original.execution_id)
        path='/api/action-intents/'+intent['id']+'/reconcile?repository_resource_id='+f.target.id
        status,_,denied=await f._request('POST',path,payload={'retry_if_idempotent':True})
        self.assertEqual(status,403,denied)
        self.assertEqual(f.intents.history(intent['id'],f.admin),history)
        status,_,body=await f._request('POST',path,payload={'retry_if_idempotent':False})
        self.assertEqual(status,200,body)
        self.assertEqual(body['item']['status'],'succeeded')
        self.assertEqual(body['item']['execution_id'],original.execution_id)
        after=f.intents.history(intent['id'],f.admin)
        self.assertEqual(after['receipts'][:len(history['receipts'])],history['receipts'])
        self.assertTrue(after['verifications'][-1]['verified'])
        self.assertEqual(after['receipts'][-1]['details']['operation'],'provider-state-reconciliation')
        self.assertFalse(after['receipts'][-1]['details']['provider_mutation_replayed'])
        self.assertEqual(provider.execute_calls,1)
        self.assertEqual(provider.prepare_calls,prepares_before+1)
        self.assertEqual(provider.verify_calls,1)
        status,_,observed=await f._request('GET','/api/action-intents/'+intent['id']+
            '/delivery-evidence?project_id=project-a&repository_resource_id='+f.target.id)
        self.assertEqual(status,200,observed)
        self.assertEqual(observed['verifications'][-1]['id'],after['verifications'][-1]['id'])
        self.assertEqual(provider.execute_calls,1)
