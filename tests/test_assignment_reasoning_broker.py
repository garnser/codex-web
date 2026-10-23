"""Assignment adapters; private owners, no application composition or native work."""
import asyncio
import copy
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, AsyncMock
from fastapi import HTTPException

from codex_web.executive_roles import ExecutiveActivationCreate, ExecutiveActivationStatus
from codex_web.identity import PrincipalKind
from codex_web.services.assignment_reasoning_broker import AssignmentReasoningBroker, OPERATIONS, resolve
from codex_web.services.control_plane_broker import ControlPlaneBrokerService
from codex_web.services.executive_management import ExecutiveConsultationError
import tests.test_executive_management as executive_fixture


class AssignmentReasoningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Existing canonical Executive/SQLite fixture contains no application import.
        self.fixture = executive_fixture.ExecutiveManagementTests()
        await self.fixture.asyncSetUp()
        self.fixture.service.work_graph = NS(snapshot=lambda project_id, **kwargs:
            NS(model_dump=lambda **kwargs: {"project_id":project_id,"nodes":[]}))
        self.actor = self.fixture.actor
        self.assignment = NS(id="assignment-one", fence=1, execution_id="exec-one",
            assigned_worker_id="worker-one", organization_id="local", workspace_id="default",
            project_id="project-a", created_by=self.actor.identity_id, resource_ids=("repo-a",),
            resource_context=None, execution_profile_definition="exact-ref", agent_profile=None,
            execution_profile_id="orchestration-only", procedure_checkpoint=None)
        self.live = copy.copy(self.assignment)
        self.allowed = [op.id for op in OPERATIONS]
        self.broker = NS(execution_workers=NS(store=NS(assignment=lambda _: self.live)),
            execution_profiles=NS(resolve=lambda *a, **k: (NS(control_plane_operations=self.allowed), "exact-ref")),
            _actor=Mock(return_value=NS(identity_id="worker-service")),
            _requester_actor=Mock(return_value=self.actor), _authorize=Mock(),
            _state_for_target=Mock(return_value=NS(organization_id="local", workspace_id="default",
                project_id="project-a", resource_ids=("repo-a",))))
        self.procedures = NS(_live=Mock(), get=Mock(return_value=self.assignment),
            start=Mock(return_value=self.assignment), advance=AsyncMock(return_value=self.assignment))
        self.adapter = AssignmentReasoningBroker(self.broker, procedures=self.procedures,
            executives=self.fixture.service)

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def call(self, name, payload=None, target=None, query=None):
        op = next(op for op in OPERATIONS if op.id == name)
        return await self.adapter.dispatch(self.assignment, NS(identity_id="worker-service"), op,
            target, payload or {}, query or {})

    def request(self):
        return ExecutiveActivationCreate(subject="Priority exception", request="Review a bounded technical priority.",
            project_id="project-a", trigger_ref=self.adapter.origin(self.assignment),
            requested_role_ids=("cto",), max_roles=1).model_dump(mode="json")

    async def test_closed_routes_and_actual_broker_resolution(self):
        for op in OPERATIONS:
            path = op.path_template.replace("{ref}", "assignment-one")
            resolved = ControlPlaneBrokerService._resolve_operation(op.method, path)
            self.assertEqual(resolved.operation.id, op.id)
        with self.assertRaises(HTTPException):
            resolve("POST", "/api/executive/activations/other/materialize")
        with self.assertRaises(HTTPException):
            resolve("GET", "/api/execution-workers/assignments/%2Fother/procedure")

    async def test_procedure_dispatch_preserves_exact_actor_and_owner(self):
        result = await self.call("procedure.read", target=self.assignment.id)
        self.assertEqual(result["assignment_id"], self.assignment.id)
        self.procedures.get.assert_called_once_with(self.assignment.id, self.actor)
        await self.call("procedure.advance", target=self.assignment.id)
        self.procedures.advance.assert_awaited_once_with(self.assignment.id, actor=self.actor)

    async def test_cross_assignment_stale_fence_tenant_and_allowlist_refuse(self):
        with self.assertRaises(HTTPException):
            await self.call("procedure.read", target="other")
        for field, value in (("fence",2), ("organization_id","other"),
                ("project_id","other"), ("execution_id","new"), ("resource_ids",("foreign",))):
            original = getattr(self.live, field)
            setattr(self.live, field, value)
            with self.assertRaises(HTTPException):
                await self.call("procedure.read", target=self.assignment.id)
            setattr(self.live, field, original)
        self.allowed.clear()
        with self.assertRaises(HTTPException):
            await self.call("procedure.read", target=self.assignment.id)
        self.procedures.get.assert_not_called()

    async def test_live_owner_expiry_and_actual_authority_denial_propagate(self):
        self.procedures._live.side_effect = RuntimeError("lease expired")
        with self.assertRaisesRegex(RuntimeError, "lease expired"):
            await self.call("procedure.read", target=self.assignment.id)
        self.procedures._live.side_effect = None
        self.broker._authorize.side_effect = HTTPException(403, "denied")
        with self.assertRaises(HTTPException):
            await self.call("procedure.read", target=self.assignment.id)
        self.procedures.get.assert_not_called()

    async def test_service_requester_requires_real_scope_without_impersonation(self):
        self.broker._requester_actor.return_value = self.actor.model_copy(update={
            "principal_kind":PrincipalKind.SERVICE,"service_scopes":(),"identity_id":"actual-service"})
        with self.assertRaises(HTTPException):
            await self.call("executive.activation.read")
        self.broker._requester_actor.return_value = self.broker._requester_actor.return_value.model_copy(
            update={"service_scopes":("executive:consult",)})
        await self.call("executive.activation.read")
        self.assertEqual(self.broker._authorize.call_args.kwargs["actor"].identity_id,"actual-service")

    async def test_create_read_lost_response_reuse_and_changed_payload_refuses(self):
        result = await self.call("executive.activation.create", self.request())
        self.assertFalse(result["reused"])
        again = await self.call("executive.activation.create", self.request())
        self.assertTrue(again["reused"])
        self.assertEqual(result["item"]["id"], again["item"]["id"])
        read = await self.call("executive.activation.read")
        self.assertEqual(read["count"],1)
        changed=self.request();changed["request"]="Different question"
        with self.assertRaises(HTTPException):
            await self.call("executive.activation.create",changed)
        self.assertEqual(len(self.fixture.store.load().activations),1)

    async def test_project_origin_and_resource_ref_denials_create_nothing(self):
        for field, value in (("project_id","foreign"), ("trigger_ref","arbitrary")):
            request=self.request();request[field]=value
            with self.assertRaises(HTTPException):
                await self.call("executive.activation.create",request)
        self.broker._state_for_target.return_value.resource_ids=("foreign",)
        request=self.request();request["work_item_refs"]=["item-one"]
        with self.assertRaises(HTTPException):
            await self.call("executive.activation.create",request)
        self.assertEqual(len(self.fixture.store.load().activations),0)

    async def test_unprovable_automatic_company_context_refuses(self):
        self.fixture.service.organizational_memory = executive_fixture._Memory()
        with self.assertRaises(HTTPException):
            await self.call("executive.activation.create",self.request())
        self.assertEqual(len(self.fixture.store.load().activations),0)

    async def test_duplicate_create_has_one_canonical_id(self):
        results = await asyncio.gather(*[self.call("executive.activation.create",self.request())
            for _ in range(2)], return_exceptions=True)
        self.assertEqual(len(self.fixture.store.load().activations),1)
        self.assertTrue(any(isinstance(result,dict) for result in results))

    async def test_canonical_consult_current_status_cas_prevents_duplicate_model(self):
        created = await self.call("executive.activation.create",self.request())
        activation_id=created["item"]["id"]
        # Simulate another request winning after stale get/catalog, before the
        # actual store critical section. Test the canonical owner, not a mutex.
        original_update=self.fixture.store.update
        won=False
        def racing_update(item_id, updater):
            nonlocal won
            if not won:
                won=True
                def reserve(state,current):
                    current.status=ExecutiveActivationStatus.CONSULTING;current.revision+=1
                    return state,current
                original_update(item_id,reserve)
            return original_update(item_id,updater)
        self.fixture.store.update=racing_update
        with self.assertRaises(ExecutiveConsultationError):
            await self.fixture.service.consult(activation_id, actor=self.actor)
        self.assertEqual(self.fixture.gateway.requests,[])

    async def test_explicit_admin_envelope_is_executive_only_and_per_repository(self):
        self.assignment.execution_profile_id=self.live.execution_profile_id="orchestration-admin"
        self.assignment.resource_ids=self.live.resource_ids=()
        self.broker.authority=object()
        allowed={"repo-a"}
        def target(assignment, *, repository_id, **kwargs):
            if repository_id not in allowed:
                raise ValueError("undeclared admin repository")
        self.broker.execution_profiles.authorize_administrative_target=Mock(side_effect=target)
        operation=next(op for op in OPERATIONS if op.id=="executive.activation.create")
        request=ExecutiveActivationCreate.model_validate({**self.request(),"work_item_refs":["work-340"]})
        self.adapter.context(request,self.assignment,self.actor,operation)
        self.assertEqual(self.broker.execution_profiles.authorize_administrative_target.call_args.kwargs["repository_id"],"repo-a")
        allowed.clear()
        with self.assertRaisesRegex(ValueError,"undeclared"):
            self.adapter.context(request,self.assignment,self.actor,operation)
        self.assignment.execution_profile_id=self.live.execution_profile_id="orchestration-only"
        with self.assertRaises(HTTPException):
            self.adapter.context(request,self.assignment,self.actor,operation)

    async def test_copied_stale_definition_does_not_authorize(self):
        self.broker.execution_profiles.resolve=lambda *a,**k:(NS(control_plane_operations=self.allowed),"new-ref")
        with self.assertRaises(HTTPException):
            await self.call("executive.activation.create",self.request())
        self.assertEqual(len(self.fixture.store.load().activations),0)

    async def test_prepared_unrequested_and_out_of_scope_snapshots_refuse(self):
        original=self.fixture.service.prepare
        for row in ({"id":"not-requested","organization_id":"local","workspace_id":"default","project_id":"project-a"},
                {"id":"not-requested","organization_id":"other","workspace_id":"default","project_id":"project-a"}):
            def expanded(request, *, actor):
                item=original(request,actor=actor)
                item.context=item.context.model_copy(update={"evidence":(row,)})
                return item
            self.fixture.service.prepare=expanded
            with self.assertRaises(HTTPException):
                await self.call("executive.activation.create",self.request())
            self.assertEqual(len(self.fixture.store.load().activations),0)

    async def test_procedure_start_uses_shared_canonical_dto_and_exact_input(self):
        _,reference=self.fixture.roles.resolve(organization_id="local",workspace_id="default",project_id="project-a")
        payload={"definition":reference.model_dump(mode="json"),"input_snapshot_id":"artifact-input"}
        await self.call("procedure.start",payload,target=self.assignment.id)
        self.procedures.start.assert_called_once_with(self.assignment.id,
            definition=reference,input_snapshot_id="artifact-input",actor=self.actor)
        with self.assertRaises(ValueError):
            await self.call("procedure.start",{**payload,"actor":"override"},target=self.assignment.id)

    async def test_body_and_query_are_closed(self):
        for name,target in (("procedure.read",self.assignment.id),("executive.activation.read",None)):
            with self.assertRaises(HTTPException):
                await self.call(name,{"actor":"other"},target)
            with self.assertRaises(HTTPException):
                await self.call(name,target=target,query={"project_id":["foreign"]})


if __name__ == "__main__":
    unittest.main()
