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
        f.service.native_invocation_authorizer=lambda activation,actor:lambda:None
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
        f.service.native_invocation_authorizer=lambda activation,actor:lambda:None
        self.assertEqual(f.service._native_context(activation,'cto',f.actor,1000,1000),{})
        # The existing ordinary gateway accepts no native-only keyword/context.
        # Native opt-in must not break a legitimate projectless consultation.
        f.service.native_invocation_authorizer=lambda *args: (_ for _ in ()).throw(
            AssertionError('ordinary HTTP route borrowed native requester guard'))
        completed=await f.service.consult(activation.id,actor=f.actor)
        self.assertEqual(completed.status,'completed')
        self.assertEqual(len(f.gateway.requests),1)
