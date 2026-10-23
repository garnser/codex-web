"""Private canonical admission/provider contracts; no real model or host action."""
import copy
import unittest
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from codex_web.action_intents import ActionDecisionSnapshot, ActionIntentCreate, ActionIntentStatus
from codex_web.action_providers import ActionProviderBindingCreate, ActionRequest
from codex_web.definitions import DefinitionReference
from codex_web.resources import ResourceType, ResourceUpdate
from codex_web.runtime.execution import ExecutionPrestartRefusal
from codex_web.services.native_procedure_preparation import NativeProviderPreparation, native_provider_preparation
from codex_web.services.procedure_discovery import NativeProcedureAdmissionService
from codex_web.services.procedure_execution import ProcedureExecutionService
from codex_web.services.action_providers import ActionRequirementError
from codex_web.skills import SkillCreate, SkillPublish
import tests.test_procedure_native_admission as native_fixtures


class NativeProviderPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=native_fixtures.ProductionNativeProcedureAdmissionTests()
        await self.fixture.asyncSetUp()
        f=self.fixture
        from codex_web.resources import RepositoryExecutionScope
        scope=RepositoryExecutionScope(organization_id=f.actor.organization_id,
            workspace_id=f.actor.workspace_id,project_id=f.project.id,
            writable_repository_ids=(f.repo.id,),source="explicit")
        f.native_assignment=f.workers.store.update_assignment(f.procedure_worker.id,f.native_assignment.id,
            lambda state:state.model_copy(update={"assignments":[state.assignments[0].model_copy(
                update={"repository_scope":scope})]})).assignments[0]
        original=f.ops.actions
        actions=tuple(item.model_copy(update={"required_resource_types":(ResourceType.REPOSITORY,)})
            for item in original())
        f.ops.actions=lambda:actions
        f.registry.register(f.ops,tenant_scope=f.actor.tenant)
        self.binding=f.registry.bind(ActionProviderBindingCreate(provider_type="ops",provider_instance="test",
            project_id=f.project.id,resource_ids=(f.repo.id,)),actor=f.actor,resources=f.resources)
        f.resources.update(f.repo.id,ResourceUpdate(operational_context={"environment":"development",
            "service_versions":{"gitlab":"18.0.0"}}),actor=f.actor)
        content=copy.deepcopy(f.content)
        content["resource_selectors"][0]["resource_types"]=["repository"]
        content["environments"]=["development"]
        for step in content["steps"]:
            if "worker" in step:
                del step["worker"]
                step["action"]={"provider_type":"ops","provider_instance":"test",
                    "provider_contract_version":"1.2","action_id":"gitlab.logs.read","resource_selector":"service"}
        self.content=content
        created=f.skills.create(SkillCreate(skill_id="native-provider-required",name="Native provider procedure",
            instructions="Prepare locally; use the exact canonical procedure for privileged operations.",
            operational_procedure=content),actor=f.actor)
        published=f.skills.publish(created["skillId"],created["recordId"],SkillPublish(),actor=f.actor)
        self.reference=DefinitionReference.model_validate(published["definitionReference"])
        self.owner=ProcedureExecutionService(workers=f.workers,skills=f.skills,discovery=f.discovery,
            intents=f.service,evidence=None,identity=f.identity,authority=f.authority,
            approvals=None,attention=None,control_actor=f.actor)
        f.service.procedure_execution=self.owner
        f.execution.procedure_admission=f.service.procedure_provider_admission
        self.admission=NativeProcedureAdmissionService(f.discovery,f.workers,f.history,f.actor,f.profiles)
        self.admission.procedure_execution=self.owner
        self.attestor=Mock()
        self.admission.native_session_attestor=self.attestor

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def admit(self):
        f=self.fixture
        return await self.admission(project=f.project,actor=f.actor,thread_id="native-provider-thread",
            execution_id=f.native_assignment.execution_id,assignment_id=f.native_assignment.id,
            sandbox=f.native_assignment.sandbox,source="queued:web",request_checksum="a"*64)

    async def test_real_discovery_old_refusal_then_attested_provider_preparation(self):
        self.admission.native_session_attestor=None
        with self.assertRaises(ExecutionPrestartRefusal) as caught:
            await self.admit()
        self.assertEqual(caught.exception.detail["code"],"procedure_unsupported_enforcement")
        self.admission.native_session_attestor=self.attestor
        discovery,_=await self.admit()
        self.assertTrue(discovery.admitted,discovery)
        self.assertEqual(discovery.selected,(self.reference,))
        self.assertIn("canonical:native-provider-preparation:",discovery.policy_source)
        self.assertEqual(self.fixture.ops.calls,[])

    async def test_missing_broker_or_invalid_current_proof_refuses(self):
        self.attestor.side_effect=ValueError("broker not serving")
        with self.assertRaises(ExecutionPrestartRefusal):
            await self.admit()
        self.assertEqual(self.fixture.ops.calls,[])

    async def test_final_guard_rechecks_broker_and_fingerprint(self):
        f=self.fixture
        discovery,_=await self.admit()
        guard=self.admission.preparation_input_guard(discovery,project=f.project,actor=f.actor,
            execution_id=f.native_assignment.execution_id,assignment_id=f.native_assignment.id,
            sandbox=f.native_assignment.sandbox,request_checksum="a"*64)
        guard()
        self.attestor.side_effect=ValueError("lost broker")
        with self.assertRaises(ExecutionPrestartRefusal):guard()
        self.attestor.side_effect=None
        f.resources.update(f.repo.id,ResourceUpdate(operational_context={"environment":"production",
            "service_versions":{"gitlab":"18.0.0"}}),actor=f.actor)
        with self.assertRaises(ExecutionPrestartRefusal):guard()

    async def test_environment_and_native_network_denials_are_not_rescued(self):
        f=self.fixture
        f.resources.update(f.repo.id,ResourceUpdate(operational_context={}),actor=f.actor)
        with self.assertRaises(ExecutionPrestartRefusal):await self.admit()
        self.attestor.assert_not_called()
        f.resources.update(f.repo.id,ResourceUpdate(operational_context={"environment":"development",
            "service_versions":{"gitlab":"18.0.0"}}),actor=f.actor)
        f.workers.store.update_assignment(f.procedure_worker.id,f.native_assignment.id,
            lambda state: state.model_copy(update={"assignments":[state.assignments[0].model_copy(
                update={"network":state.assignments[0].network.model_copy(update={"enabled":True})})]}))
        with self.assertRaises(ExecutionPrestartRefusal):await self.admit()
        self.attestor.assert_not_called()

    async def test_worker_step_and_changed_provider_contract_remain_unsupported(self):
        f=self.fixture
        original_get=f.skills.definitions.get_record
        def worker_record(record_id):
            record=original_get(record_id)
            if record_id==self.reference.record_id:
                payload=copy.deepcopy(record.payload)
                step=payload["operational_procedure"]["steps"][2]
                del step["action"]
                step["worker"]={"capability":"network","artifact_reference":"artifact-worker",
                    "artifact_checksum":"a"*64,"resource_selector":"service"}
                record=record.model_copy(update={"payload":payload})
            return record
        with patch.object(f.skills.definitions,"get_record",side_effect=worker_record):
            with self.assertRaisesRegex(Exception,"worker_step_unsupported"):
                self.owner.validate_native_preparation(f.native_assignment,(self.reference,),f.actor)
        with patch.object(f.ops,"contract_version","9.0"):
            with self.assertRaises(ExecutionPrestartRefusal):await self.admit()

    async def test_attested_subset_cannot_authorize_different_selection_or_escape_scope(self):
        f=self.fixture
        context=self.admission._preparation_context(f.native_assignment,f.actor,f.project,
            f.native_assignment.sandbox,"a"*64)
        other=self.reference.model_copy(update={"revision":self.reference.revision+1})
        validator=Mock()
        with native_provider_preparation(NativeProviderPreparation((other,),"stored-string-is-not-authority",validator)):
            refused,_=f.discovery.discover(context)
        self.assertFalse(refused.admitted)
        validator.assert_not_called()
        refused,_=f.discovery.discover(context)
        self.assertEqual(refused.outcome,"unsupported_enforcement")

    async def test_actual_process_session_requires_serving_same_broker_and_isolation(self):
        from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSession
        from codex_web.services.control_plane_broker import ControlPlaneBrokerService, AssignmentBoundControlPlaneBroker
        from codex_web.storage.control_plane_broker import ControlPlaneBrokerAuditStore
        from codex_web.local_execution_backend import BubblewrapExecutionBackend, LocalIsolationStatus
        f=self.fixture
        service=ControlPlaneBrokerService(identity=f.identity,authority=f.authority,work_items=None,operator=NS(),
            audit=ControlPlaneBrokerAuditStore(f.sqlite))
        broker=AssignmentBoundControlPlaneBroker(service,assignment=f.native_assignment,
            worker_id=f.procedure_worker.id,worker_actor=f.procedure_worker_actor,
            fence=f.native_assignment.fence,
            validator=lambda:f.workers.store.assignment(f.native_assignment.id))
        await broker.start()
        try:
            backend=BubblewrapExecutionBackend(executable="/private-fixture-not-executed")
            status=LocalIsolationStatus(backend="bubblewrap",ready=True,reason=None,
                supports_network_disabled=True,supports_network_allowlist=False,supports_resource_limits=True)
            backend.probe=Mock(return_value=status)
            session=object.__new__(AssignmentBoundAgentProcessSession)
            session.local_worker=NS(backend=backend,worker=f.procedure_worker)
            session._native_preparation_backend=backend
            session.control_plane_broker=broker
            session.validate_current=Mock(return_value=f.native_assignment)
            session.status=Mock(return_value=NS(running=True,ready=True))
            self.assertEqual(session.attest_native_provider_preparation(f.native_assignment).id,f.native_assignment.id)
            with self.assertRaises(Exception):
                session.attest_native_provider_preparation(f.native_assignment.model_copy(update={"fence":99}))
            backend.probe.return_value=replace(status,supports_network_disabled=False)
            with self.assertRaises(Exception):session.attest_native_provider_preparation(f.native_assignment)
            backend.probe.return_value=status
            await broker.stop()
            with self.assertRaises(Exception):session.attest_native_provider_preparation(f.native_assignment)
        finally:
            await broker.stop()

    async def test_actual_native_owner_final_guard_refuses_before_model_input(self):
        from tests.test_turn_execution import TurnExecutionStartTests
        f=self.fixture
        discovery,_=await self.admit()
        _,_,sessions,runtime=TurnExecutionStartTests._service(self)
        checked=[]
        class Gate:
            async def __call__(gate,**kwargs):return discovery,"Required provider preparation"
            def preparation_input_guard(gate,*args,**kwargs):
                def validate():
                    checked.append(True)
                    raise ExecutionPrestartRefusal(status_code=409,detail={"code":"procedure_native_preparation_changed"})
                return validate
        runtime.procedure_admission=Gate()
        with self.assertRaises(ExecutionPrestartRefusal):
            await runtime.start_thread_turn_now("native-owner-thread",project=f.project,actor=f.actor,
                message="Prepare through canonical broker",sandbox="workspace-write",approval_policy="on-request",
                source="web",execution_id=f.native_assignment.execution_id)
        self.assertTrue(checked)
        self.assertFalse(any(method=="turn/start" for method,_ in sessions.session.requests))

    async def test_direct_provider_and_nonstep_intent_cannot_mutate(self):
        f=self.fixture
        request=ActionRequest(action_id="service.rotate",organization_id=f.actor.organization_id,
            workspace_id=f.actor.workspace_id,project_id=f.project.id,resource_ids=(f.repo.id,))
        with self.assertRaisesRegex(ActionRequirementError,"procedure_unsupported_enforcement"):
            await f.execution.execute(self.binding.id,request,actor=f.actor)
        intent=f.service.create(ActionIntentCreate(binding_id=self.binding.id,
            execution_id=f.native_assignment.execution_id,request=request,
            policy_decision=ActionDecisionSnapshot(source="canonical:private-test",outcome="allow")),actor=f.actor)
        from tests.test_action_intents import ActionIntentTests
        completed=await ActionIntentTests._execute(f,intent)
        self.assertEqual(completed.status,ActionIntentStatus.FAILED,completed)
        self.assertIn("durable current procedure step",completed.last_error)
        self.assertEqual(f.ops.calls,[])


if __name__=="__main__":unittest.main()
