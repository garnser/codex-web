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
