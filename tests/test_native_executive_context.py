import unittest
from types import SimpleNamespace

from codex_web.executive_roles import ExecutiveActivationCreate
from codex_web.services.executive_management import ExecutiveConsultationError
from tests import test_executive_management as fixture


class NativeExecutiveContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixture.ExecutiveManagementTests('test_deterministic_selection_invokes_only_relevant_roles_and_preserves_disagreement')
        await self.fixture.asyncSetUp()
        self.addCleanup(self.fixture.temp.cleanup)

    async def test_reserved_context_has_project_role_and_original_origin_guard(self):
        f=self.fixture
        activation=f.service.create(ExecutiveActivationCreate(subject='Architecture migration',
            request='Assess one bounded architecture migration decision',requested_role_ids=('cto',)),actor=f.actor)
        # Canonical creation permits a projectless advisory; native execution
        # requires an explicit actual project scope rather than inventing Home.
        def pin(state,current):
            updated=current.model_copy(update={'project_id':'project'})
            state.activations=[updated if x.id==updated.id else x for x in state.activations]
            return state,updated
        activation=f.store.update(activation.id,pin)
        f.service.native_invocation_authorizer=lambda activation,actor,**kwargs:lambda:None
        guards=[]
        original=f.gateway.invoke
        async def invoke(request,*,actor,native_context):
            self.assertEqual(native_context.project_id,'project')
            self.assertEqual(native_context.activation_id,activation.id)
            self.assertEqual(request.execution_id,f'executive:{activation.id}:cto')
            native_context.validate_current()
            return await original(request,actor=actor)
        f.gateway.invoke=invoke
        completed=await f.service.consult(activation.id,actor=f.actor,
            native_origin_guard=lambda:guards.append('current-origin'))
        self.assertEqual(completed.status,'completed')
        self.assertEqual(guards,['current-origin'])
        self.assertEqual(len(f.gateway.requests),1)

    async def test_native_optin_preserves_ordinary_projectless_consultation(self):
        f=self.fixture
        activation=f.service.create(ExecutiveActivationCreate(subject='Architecture migration',
            request='Assess one bounded architecture migration decision',requested_role_ids=('cto',)),actor=f.actor)
        self.assertEqual(f.service._native_context(activation,'cto',f.actor,1000,1000),{})
        f.service.native_invocation_authorizer=lambda activation,actor,**kwargs:lambda:None
        self.assertEqual(f.service._native_context(activation,'cto',f.actor,1000,1000),{})
        # The existing ordinary gateway accepts no native-only keyword/context.
        # Native opt-in must not break a legitimate projectless consultation.
        f.service.native_invocation_authorizer=lambda *args: (_ for _ in ()).throw(
            AssertionError('ordinary HTTP route borrowed native requester guard'))
        completed=await f.service.consult(activation.id,actor=f.actor)
        self.assertEqual(completed.status,'completed')
        self.assertEqual(len(f.gateway.requests),1)

    async def test_governed_source_revoked_after_reservation_refuses_final_native_context(self):
        from pathlib import Path
        from codex_web.data_governance import GovernedDataCreate
        from codex_web.services.data_governance import DataGovernanceService
        from codex_web.storage.data_governance import DataGovernanceStore
        from codex_web.storage.sqlite_state import SQLiteStateStore
        f=self.fixture
        governance=DataGovernanceService(DataGovernanceStore(SQLiteStateStore(Path(f.temp.name)/'governance.sqlite')))
        f.service.data_governance=governance
        source=governance.register_domain_record(GovernedDataCreate(object_type='knowledge',object_id='source',
            category='memory',classification='internal'),actor=f.actor)
        activation=f.service.create(ExecutiveActivationCreate(subject='Architecture migration',
            request='Assess one bounded migration',requested_role_ids=('cto',)),actor=f.actor)
        context=activation.context.model_copy(update={'memory':({'knowledge_id':'source','version':1,
            'classification':'internal','governance_record_id':source.id,'summary':'Bounded source'},)})
        def pin(state,current):
            updated=current.model_copy(update={'project_id':'project','context':context})
            state.activations=[updated if row.id==current.id else row for row in state.activations]
            return state,updated
        activation=f.store.update(activation.id,pin)
        catalog,_=f.service._catalog(actor=f.actor,project_id='project')
        self.assertTrue(f.service._filter_context(context,catalog.role_map['cto'])['memory'])
        f.service.native_invocation_authorizer=lambda *args,**kwargs:lambda:None
        async def invoke(request,*,actor,native_context):
            native_context.validate_current()  # The same original is initially allowed.
            def revoke(state):
                state.records=[row.model_copy(update={'deny_model_context':True}) if row.id==source.id else row
                               for row in state.records]
                return state
            governance.store.update(revoke)
            native_context.validate_current()  # Simulates the fresh native writer edge.
            self.fail('Revoked source reached inference')
        f.gateway.invoke=invoke
        with self.assertRaisesRegex(ExecutiveConsultationError,'governance permission changed'):
            await f.service.consult(activation.id,actor=f.actor)
        self.assertEqual(f.gateway.requests,[])
