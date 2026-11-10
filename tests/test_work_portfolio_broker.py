"""Actual administrative broker HTTP + published custody procedure + indexed Work reads.

No app/native/provider invocation. Canonical policy/worker/Skill/discovery owners
are real; only unrelated continuity scheduling is absent from this read fixture.
"""
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode, quote
import unittest
from unittest.mock import patch

from codex_web.models import Project, WorkItemState
from codex_web.control_plane_broker import ControlPlaneBrokerLimits
from codex_web.services.control_plane_broker import AssignmentBoundControlPlaneBroker
from codex_web.services.work_items import WorkItemService
from codex_web.storage.runtime_state import ModelMapRepository
from codex_web.storage.work_item_list_index import WorkItemListIndex
from codex_web.procedure_control_plane import CanonicalOperationRegistry
from codex_web.services.procedure_custody_recovery import register_custody_operations, output_contract
from codex_web.services.procedure_discovery import BrokerProcedureAdmissionService, ProcedureDiscoveryService
from codex_web.services.action_providers import ActionProviderRegistry
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.services.skills import SkillService
from codex_web.services.skill_security import SkillSecurityService
from codex_web.storage.skill_security import SkillSecurityStore
from codex_web.services.skill_scanners import SkillScannerRegistry
from codex_web.skills import SkillCreate, SkillPublish
from tests import test_administrative_broker_runtime as admin_fixture
from tests import test_control_plane_broker as broker_fixture
from tests.test_skill_security import FakeScanner


class WorkPortfolioBrokerTests(unittest.IsolatedAsyncioTestCase):
    _assignment = staticmethod(broker_fixture.ControlPlaneBrokerTests._assignment)
    _publish_worker_authority = broker_fixture.ControlPlaneBrokerTests._publish_worker_authority
    _request = broker_fixture.ControlPlaneBrokerTests._request
    _publish_profile = admin_fixture.AdministrativeBrokerRuntimeTests._publish_profile
    asyncTearDown = broker_fixture.ControlPlaneBrokerTests.asyncTearDown

    async def asyncSetUp(self):
        await admin_fixture.AdministrativeBrokerRuntimeTests.asyncSetUp(self,
            operations=['work_item.list', 'work_item.read', 'work_item.handoff', 'control_plane.operations.list'],
            prepare_only=True)
        self.service.limits = ControlPlaneBrokerLimits(max_requests_per_minute=60)
        root=Path(self.temp.name)
        self.items=ModelMapRepository(self.sqlite,namespace='work_item_states',
            legacy_path=root/'work.json',model=WorkItemState)
        for number,project,tenant in [(1,'project-a','default'),(2,'project-a','default'),
                (3,'project-a','default'),(4,'project-b','default'),(5,'project-a','foreign')]:
            state=WorkItemState(ref='group/app#'+str(number),project_id=project,
                workspace_id=tenant,title='Portfolio item '+str(number),current_owner='carl',
                current_stage='implementation_active',last_meaningful_update_at=number,
                updated_at=number,created_at=number,notes=['Private long history']*100)
            self.items.put(state.ref,state)
        project=Project(id='project-a',name='Private Project',path=str(root))
        host=SimpleNamespace(DATA_DIR=root,WORK_ITEM_EVENTS_FILE=root/'events.jsonl',
            _load_work_item_states=self.items.load,_get_work_item_state_record=self.items.get,
            _save_work_item_states=self.items.save,_load_projects=lambda:[project])
        index=WorkItemListIndex(self.sqlite);index.rebuild(self.items.load())
        self.work_service=WorkItemService(host,work_item_list_index=index)
        self.service.work_items=self.work_service
        actions=ActionProviderRegistry(ActionProviderStateStore(self.sqlite))
        control=CanonicalOperationRegistry()
        register_custody_operations(control,workers=self.workers,routing=None,threads=None,
            continuity=None,get_work=self.items.get,preflight=None)
        skills=SkillService(self.registry,clock=lambda:100)
        skills.bind_procedure_contracts(actions=actions,resources=self.resources,
            scope_actor=lambda _:self.admin,control_plane=control)
        scans=SkillScannerRegistry();scans.register(FakeScanner())
        skills.bind_security(SkillSecurityService(SkillSecurityStore(self.sqlite),self.registry,scans,clock=lambda:100))
        inputs=[{'name':name,'type':kind,'description':'Exact canonical custody input','required':name!='original_preflight_id'}
            for name,kind in [('work_item_ref','string'),('target_assignment_id','string'),
                ('expected_target_fence','integer'),('expected_handoff_requested_at','number'),('original_preflight_id','string')]]
        operation='work.handoff.inspect.v1'
        contract={'schema_version':'1.4','owner_identity_id':self.admin.identity_id,
            'task_triggers':['custody.recovery'],'operation_triggers':['work_item.handoff'],
            'resource_selectors':[{'id':'repository','resource_types':['repository'],'resource_ids':[self.target.id]}],
            'environments':['development'],'inputs':inputs,'reviewed_at':1,'review_due_at':200,'expires_at':300,
            'conflict_group':'private-custody','precedence':1,'max_duration_seconds':30,
            'steps':[{'id':'inspect','kind':'diagnostic','description':'Inspect exact governed handoff',
                'inputs':inputs,'outputs':[v.model_dump(mode='json') for v in output_contract(operation)],
                'preconditions':[],'expected_evidence':[{'name':'observation','kind':'artifact','description':'Exact canonical custody observation'}],'timeout_seconds':30,'max_attempts':1,
                'retry_backoff_seconds':0,'failure':{'outcome':'escalate',
                    'escalation_owner_identity_id':self.admin.identity_id,'attention_reason':'Preserve original custody'},
                'permissions':[{'capability':'work_item.read','level':'read','resource_selector':'repository'}],
                'control_plane':{'operation_id':operation,'contract_version':'1.0','resource_selector':'repository'}}]}
        draft=skills.create(SkillCreate(skill_id='private-custody-recovery',name='Private custody recovery',
            instructions='Exact custody only; metadata observations cannot execute recovery.',
            operational_procedure=contract),actor=self.admin)
        self.published=skills.publish(draft['skillId'],draft['recordId'],SkillPublish(),actor=self.admin)
        self.discovery_records=[]
        owner=SimpleNamespace(record_procedure_discovery=lambda *a,**k:self.discovery_records.append(a[1]))
        self.admission=BrokerProcedureAdmissionService(ProcedureDiscoveryService(skills,self.resources),owner,self.admin)
        self.service.procedure_admission=self.admission
        self.broker=AssignmentBoundControlPlaneBroker(self.service,assignment=self.assignment,
            worker_id=self.worker.id,worker_actor=self.worker_actor,fence=self.assignment.fence,
            validator=lambda:self.current_assignment)
        await self.broker.start()

    async def test_same_project_portfolio_is_readable_with_actual_inaccessible_custody_procedure(self):
        before=self.items.load()
        status,_,body=await self._request('GET','/api/work-items?project_id=project-a&limit=2')
        self.assertEqual(status,200,body)
        self.assertEqual([x['ref'] for x in body['items']],['group/app#3','group/app#2'])
        self.assertEqual(body['pageSize'],2)
        self.assertTrue(body['hasMore'])
        self.assertNotIn('notes',body['items'][0]);self.assertNotIn('execution',body['items'][0])
        availability=body['procedure_availability']
        self.assertEqual(availability['discovery']['outcome'],'inaccessible')
        self.assertIn('target_resources_unresolved',availability['discovery']['blockers'])
        self.assertIn('environment_unresolved',availability['discovery']['blockers'])
        self.assertFalse(availability['execution_authorized'])
        self.assertFalse(availability['assignment_history_recorded'])
        self.assertEqual(self.discovery_records,[])
        self.assertEqual(self.items.load(),before)
        self.assertEqual(self.assignment.resource_ids,())
        self.assertIsNone(self.assignment.repository_scope)

    async def test_real_pagination_preserves_filters_and_rejects_changed_cursor(self):
        query={'owner':'carl','stage':'implementation_active','q':'Portfolio','limit':1}
        status,_,first=await self._request('GET','/api/work-items?'+urlencode(query))
        self.assertEqual(status,200,first)
        status,_,second=await self._request('GET','/api/work-items?'+urlencode({**query,'cursor':first['nextCursor']}))
        self.assertEqual(status,200,second)
        self.assertNotEqual(first['items'][0]['ref'],second['items'][0]['ref'])
        status,_,error=await self._request('GET','/api/work-items?'+urlencode({**query,'owner':'james','cursor':first['nextCursor']}))
        self.assertEqual(status,400,error)

    async def test_invalid_foreign_and_unsupported_queries_never_inspect_procedures(self):
        with patch.object(self.admission,'inspect_current',wraps=self.admission.inspect_current) as inspecting:
            for query in ['project_id=foreign','project_id=project-a&project_id=project-a',
                    'kind=merge_request','kind=','view=coordination','view=',
                    'project_id=&project_id=project-a','project_id=project-a&project_id=',
                    'limit=','repository_resource_id=caller',
                    'limit=101','limit=0','limit=invalid']:
                with self.subTest(query=query):
                    status,_,body=await self._request('GET','/api/work-items?'+query)
                    self.assertIn(status,(400,403),body)
            status,_,body=await self._request('GET','/api/work-items',payload={})
            self.assertEqual(status,400,body)
            inspecting.assert_not_called()

    async def test_concrete_work_and_handoff_still_fail_actual_procedure_admission(self):
        # Concrete Work access has its own canonical target; no metadata exception.
        ref='group/app#1'
        for method,path,payload in [('GET','/api/work-items/'+quote(ref,safe=''),None),
                ('POST','/api/work-items/'+quote(ref,safe='')+'/handoff',
                 {'from_agent':'james','to_agent':'quinn','reason':'Guard remains mandatory'})]:
            status,_,body=await self._request(method,path,payload=payload)
            self.assertEqual(status,409,body)
            self.assertEqual(body['error']['code'],'procedure_inaccessible')
        self.assertEqual(len(self.discovery_records),2)

    async def test_changed_fence_still_denies_before_metadata_or_discovery(self):
        self.current_assignment=self.assignment.model_copy(update={'fence':self.assignment.fence+1})
        with patch.object(self.admission,'inspect_current',wraps=self.admission.inspect_current) as inspecting:
            status,_,body=await self._request('GET','/api/work-items')
            self.assertEqual(status,403,body);inspecting.assert_not_called()

    async def test_profile_denial_still_precedes_metadata_inspection(self):
        self.current_assignment=self.assignment.model_copy(update={'execution_profile_definition':None})
        with patch.object(self.admission,'inspect_current',wraps=self.admission.inspect_current) as inspecting:
            status,_,body=await self._request('GET','/api/work-items')
            self.assertEqual(status,403,body);inspecting.assert_not_called()

    async def test_public_descriptor_truthfully_describes_supported_filters(self):
        status,_,body=await self._request('GET','/api/control-plane-broker/operations')
        self.assertEqual(status,200,body)
        item=next(x for x in body['operations'] if x['id']=='work_item.list')
        schema=item['query_parameters']
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(set(schema['properties']),{'project_id','owner','stage','q','limit','cursor'})
        self.assertEqual(schema['properties']['limit']['maximum'],100)

    async def test_revoked_worker_cannot_observe_portfolio(self):
        import time
        def revoke(state):
            state.services=[item.model_copy(update={'disabled_at':time.time()})
                if item.id==self.worker_actor.identity_id else item for item in state.services]
            return state
        self.identity.store.update(revoke)
        with patch.object(self.admission,'inspect_current',wraps=self.admission.inspect_current) as inspecting:
            status,_,body=await self._request('GET','/api/work-items')
            self.assertEqual(status,403,body);inspecting.assert_not_called()

    async def test_metadata_inspection_does_not_relax_protected_history_capacity(self):
        from codex_web.services.execution_workers import ProcedureHistoryExhaustedError
        def exhausted(*args,**kwargs):
            raise ProcedureHistoryExhaustedError('Retained original history is full')
        self.admission.workers.record_procedure_discovery=exhausted
        status,_,body=await self._request('GET','/api/work-items?limit=1')
        self.assertEqual(status,200,body)
        self.assertFalse(body['procedure_availability']['history_capacity_checked'])
        status,_,body=await self._request('GET','/api/work-items/'+quote('group/app#1',safe=''))
        self.assertEqual(status,409,body)
        self.assertEqual(body['error']['code'],'procedure_assignment_exhausted')
