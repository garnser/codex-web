"""Real assignment broker HTTP requests cannot bypass discovery in local handlers."""
import unittest
from types import SimpleNamespace
from urllib.parse import quote

from codex_web.resources import ResourceCreate, ResourceOperationalContext, ResourceType
from codex_web.services.action_providers import ActionProviderRegistry
from codex_web.services.procedure_discovery import BrokerProcedureAdmissionService, ProcedureDiscoveryService
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.skills import SkillService
from codex_web.services.skill_security import SkillSecurityService
from codex_web.services.skill_scanners import SkillScannerRegistry
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.skill_security import SkillSecurityStore
from codex_web.skills import SkillCreate, SkillPublish
from tests import test_control_plane_broker as broker_fixtures
from tests import test_administrative_broker_runtime as administrative_fixtures
from tests import test_skill_procedures as procedure_fixtures
from tests.test_skill_security import FakeScanner


class ProcedureBrokerAdmissionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = broker_fixtures.ControlPlaneBrokerTests.asyncSetUp
    asyncTearDown = broker_fixtures.ControlPlaneBrokerTests.asyncTearDown
    _assignment = staticmethod(broker_fixtures.ControlPlaneBrokerTests._assignment)
    _publish_worker_authority = broker_fixtures.ControlPlaneBrokerTests._publish_worker_authority
    _request = broker_fixtures.ControlPlaneBrokerTests._request

    async def test_broker_local_mutation_consults_pins_and_blocks_after_quarantine(self):
        admin = self.identity.local_trusted_actor()
        resources = ResourceCatalogService(ResourceCatalogStore(self.sqlite))
        target = resources.create(ResourceCreate(resource_type=ResourceType.SERVICE, name='GitLab',
            operational_context=ResourceOperationalContext(environment='staging', service_versions={'gitlab':'18.0.0'})), actor=admin)
        self.authority.resources = resources
        self.states[self.ref].resource_ids = (target.id,)
        actions = ActionProviderRegistry(ActionProviderStateStore(self.sqlite))
        actions.register(procedure_fixtures.MetadataProvider(), tenant_scope=admin.tenant)
        skills = SkillService(self.registry, clock=lambda:100)
        skills.bind_procedure_contracts(actions=actions, resources=resources, scope_actor=lambda r:admin)
        scanners = SkillScannerRegistry(); scanners.register(FakeScanner())
        skills.bind_security(SkillSecurityService(SkillSecurityStore(self.sqlite),self.registry,scanners,clock=lambda:100))
        content = procedure_fixtures.procedure()
        content['operation_triggers'] = ['work_item.handoff']
        content['required_worker_capabilities'] = []
        for step in content['steps']:
            if 'worker' in step:
                del step['worker']; step['action'] = dict(content['steps'][0]['action'])
        draft = skills.create(SkillCreate(skill_id='handoff-procedure',name='Scoped coordination',instructions='Consult canonical scoped guidance.',operational_procedure=content),actor=admin)
        published = skills.publish(draft['skillId'],draft['recordId'],SkillPublish(),actor=admin)
        records = []
        workers = SimpleNamespace(record_procedure_discovery=lambda *a, **k: records.append(a[1]))
        self.service.procedure_admission = BrokerProcedureAdmissionService(ProcedureDiscoveryService(skills,resources),workers,admin)
        path = '/api/work-items/'+quote(self.ref,safe='')+'/handoff'
        body = {'from_agent':'james','to_agent':'quinn','reason':'Explicit bounded handoff'}
        status, _, response = await self._request('POST',path,payload=body)
        self.assertEqual(status,200,response)
        self.assertEqual(records[-1].selected[0].record_id,published['recordId'])
        self.assertEqual(len(self.work_items.handoffs),1)
        self.registry.quarantine(published['recordId'],actor='security',reason='Revoked procedure trust')
        status, _, response = await self._request('POST',path,payload=body)
        self.assertEqual(status,409,response)
        self.assertEqual(response['error']['code'],'procedure_inaccessible')
        self.assertIn('procedure_quarantined',response['error']['procedure_discovery']['blockers'])
        self.assertEqual(len(self.work_items.handoffs),1)

        status, _, response = await self._request('GET','/api/control-plane-broker/operations')
        self.assertEqual(status,200,response)
        self.assertEqual(records[-1].outcome,'no_match')
        self.assertTrue(records[-1].admitted)

    async def test_exhaustion_is_typed_before_broker_handler_and_keeps_prior_mutation(self):
        from unittest.mock import patch
        from codex_web.services.execution_workers import ProcedureHistoryExhaustedError
        from codex_web.procedure_discovery import ProcedureDiscovery
        admin = self.identity.local_trusted_actor()
        discovery = ProcedureDiscovery(actor_identity_id=admin.identity_id,
            organization_id=admin.organization_id, workspace_id=admin.workspace_id,
            project_id=self.assignment.project_id, resource_ids=(), operation='work_item.handoff',
            context_checksum='exhausted-test', evaluated_at=100, outcome='no_match',
            policy_source='canonical:test', policy_allows=True)
        workers = SimpleNamespace(record_procedure_discovery=lambda *a, **k: None)
        admission = BrokerProcedureAdmissionService(None, workers, admin)
        self.service.procedure_admission = admission
        path = '/api/work-items/'+quote(self.ref,safe='')+'/handoff'
        body = {'from_agent':'james','to_agent':'quinn','reason':'Prior admitted mutation'}
        with patch.object(admission, 'inspect_current', return_value=discovery):
            status, _, response = await self._request('POST',path,payload=body)
            self.assertEqual(status,200,response)
            prior = list(self.work_items.handoffs)
            with patch.object(workers, 'record_procedure_discovery',
                    side_effect=ProcedureHistoryExhaustedError('history full')):
                status, _, response = await self._request('POST',path,payload={
                    **body, 'reason':'Must not dispatch this request'})
        self.assertEqual(status,409,response)
        self.assertEqual(response['error']['code'],'procedure_assignment_exhausted')
        self.assertEqual(response['error']['procedure_discovery']['outcome'],'failed')
        self.assertIn('procedure_assignment_exhausted',response['error']['procedure_discovery']['blockers'])
        self.assertIn('positively idle',response['error']['message'])
        self.assertEqual(self.work_items.handoffs,prior)


class ProcedureAdministrativeHistoryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = administrative_fixtures.AdministrativeBrokerRuntimeTests.asyncSetUp
    asyncTearDown = administrative_fixtures.AdministrativeBrokerRuntimeTests.asyncTearDown
    _assignment = staticmethod(administrative_fixtures.AdministrativeBrokerRuntimeTests._assignment)
    _publish_worker_authority = administrative_fixtures.AdministrativeBrokerRuntimeTests._publish_worker_authority
    _request = administrative_fixtures.AdministrativeBrokerRuntimeTests._request
    _publish_profile = administrative_fixtures.AdministrativeBrokerRuntimeTests._publish_profile
    submit = administrative_fixtures.AdministrativeBrokerRuntimeTests.submit

    async def test_real_admin_full_history_refuses_before_provider_or_history_mutation(self):
        import time
        from unittest.mock import patch
        from codex_web.procedure_discovery import ProcedureDiscovery
        observation = ProcedureDiscovery(actor_identity_id=self.admin.identity_id,
            organization_id=self.admin.organization_id,workspace_id=self.admin.workspace_id,
            project_id=self.assignment.project_id,resource_ids=(),operation='repository.issue.create',
            context_checksum='real-admin-full',evaluated_at=time.time(),outcome='no_match',
            policy_source='canonical:test',policy_allows=True)
        for index in range(64):
            self.workers.record_procedure_discovery(self.assignment.id,
                observation.model_copy(update={'context_checksum':f'real-admin-{index}'}),
                expected_fence=self.assignment.fence,actor=self.admin)
        before = self.workers.store.assignment(self.assignment.id)
        admission = BrokerProcedureAdmissionService(None,self.workers,self.admin)
        self.service.procedure_admission = admission
        with patch.object(admission,'inspect_current',return_value=observation):
            status,_,response = await self.submit()
        self.assertEqual(status,409,response)
        self.assertEqual(response['error']['code'],'procedure_assignment_exhausted')
        self.assertEqual(response['error']['procedure_discovery']['outcome'],'failed')
        self.assertIn('procedure_assignment_exhausted',response['error']['procedure_discovery']['blockers'])
        self.assertEqual(self.provider.calls,0)
        self.assertEqual(self.workers.store.assignment(self.assignment.id),before)
