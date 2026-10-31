"""One known procedure reference through the private broker; no app or network."""
import copy
import unittest
from types import SimpleNamespace as NS
from urllib.parse import urlencode
from unittest.mock import Mock, patch

from fastapi import HTTPException

from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.execution_workers import ExecutionWorker
from codex_web.identity import PrincipalKind
from codex_web.services.control_plane_broker import ControlPlaneBrokerService, ControlPlaneBrokerDeniedError
from codex_web.services.procedure_execution import ProcedureExecutionService
from codex_web.services.skills import SkillResolutionFailure
from codex_web.skills import SkillCreate, SkillUpdate
from tests import test_assignment_context_reads as context_fixture
from tests import test_skill_procedures as skill_fixture


class AssignmentProcedureReferenceResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = context_fixture.AssignmentContextReadTests()
        await self.f.asyncSetUp()
        self.f.assignment.resource_ids = self.f.live.resource_ids = ()
        self.f.assignment.execution_profile_id = self.f.live.execution_profile_id = "orchestration-admin"
        # The existing administrative broker attributes agent-role fields from
        # its admitted Agent Profile, independently of requester/worker identity.
        binding = AgentProfileExecutionBinding(profile_id="private-coordinator", profile_revision=1,
            profile_record_id="private-profile-record", role_id="orchestrator",
            authority_role_id="local-admin", execution_profile_id="orchestration-admin")
        self.f.assignment.agent_profile = self.f.live.agent_profile = binding
        self.f.skill_fixture.skills.procedure_scope_actor = lambda record: (
            self.f.actor if record.scope_id in {self.f.actor.workspace_id, "project-a"}
            else self.f.skill_fixture.foreign)
        self.service = ControlPlaneBrokerService(identity=None, authority=None, work_items=None,
            audit=None, operator=NS())
        self.service._dispatch_scope = Mock(return_value=(self.f.actor, self.f.actor, None, None))
        self.service.assignment_reasoning = self.f.adapter

    async def asyncTearDown(self):
        await self.f.asyncTearDown()

    def publish(self):
        draft = self.f.skill_fixture.skills.create(SkillCreate(skill_id="project-procedure",
            project_id="project-a", name="Project procedure", instructions="Consult canonical procedure",
            operational_procedure=skill_fixture.procedure()), actor=self.f.actor)
        return self.f.skill_fixture.publish(draft)

    async def read(self, skill_id, query=None, body=b""):
        path = "/api/skills/" + skill_id
        if query is not None:
            path += "?" + urlencode(query, doseq=True)
        status, value, operation, target, _ = await self.service.dispatch(
            assignment=self.f.assignment, worker_actor=NS(identity_id="worker-service"),
            method="GET", raw_target=path, body=body)
        self.assertEqual((status, operation.id, target), (200, "skill.procedure.read", skill_id))
        return value

    async def metadata(self, skill_id):
        return await self.read(skill_id, {"view": "reference"})

    async def test_existing_entrypoint_resolves_new_publication_then_exact_content_without_repinning(self):
        old = self.publish()
        draft = self.f.skill_fixture.skills.update(old["skillId"], SkillUpdate(
            instructions="Current reviewed procedure", reason="Canonical private publication"), actor=self.f.actor)
        new = self.f.skill_fixture.publish(draft)
        before = copy.deepcopy(vars(self.f.assignment))
        with self.assertRaises(HTTPException) as refused:
            await self.read(old["skillId"], self.f.query(old))
        self.assertEqual(refused.exception.status_code, 403)
        # Old source reaches this real broker request and refuses the unsupported
        # explicit mode; no new production helper is imported by this regression.
        expected = {"item": {"definitionReference": new["definitionReference"]}}
        with patch.object(self.f.skill_fixture.skills, "get", side_effect=AssertionError("content loaded")):
            self.assertEqual(await self.metadata(old["skillId"]), expected)
            self.assertEqual(await self.metadata(old["skillId"]), expected)
        exact = await self.read(new["skillId"], self.f.query(new))
        self.assertEqual(exact["item"]["definitionReference"], new["definitionReference"])
        self.assertIn("operational_procedure", exact["item"]["skill"])
        self.assertEqual(vars(self.f.assignment), before)
        self.f.procedures.start.assert_not_called()
        self.f.procedures.advance.assert_not_awaited()
        self.assertEqual(self.f.fixture.gateway.requests, [])

    async def test_closed_query_modes_body_and_inventory_remain_denied(self):
        view = self.publish()
        for query in ({}, {"view":"latest"}, {"view":["reference","reference"]},
                      {"view":"reference","project_id":"foreign"},
                      {"view":"reference", **self.f.query(view)}, {"revision":"1"}):
            with self.subTest(query=query), self.assertRaises(HTTPException) as error:
                await self.read(view["skillId"], query)
            self.assertEqual(error.exception.status_code, 403)
        with self.assertRaises(HTTPException):
            await self.read(view["skillId"], {"view":"reference"}, b'{"override":true}')
        with self.assertRaises(HTTPException):
            await self.f.call("goal.read", target=self.f.goal.id, query={"view":["reference"]})
        for path in ("/api/skills", "/api/skills/x/revisions", "/api/definitions/records/x"):
            with self.subTest(path=path), self.assertRaises((HTTPException, ControlPlaneBrokerDeniedError)):
                self.service._resolve_operation("GET", path)

    async def test_draft_nonprocedure_missing_quarantined_and_expired_are_ineligible(self):
        draft = self.f.skill_fixture.draft()
        with self.assertRaises(HTTPException):
            await self.metadata(draft["skillId"])
        with self.assertRaises(HTTPException):
            await self.metadata("missing-procedure")
        ordinary = self.f.skill_fixture.skills.create(SkillCreate(skill_id="ordinary-guidance",
            name="Guidance", instructions="Read only descriptive guidance"), actor=self.f.actor)
        ordinary = self.f.skill_fixture.publish(ordinary)
        with self.assertRaises(HTTPException):
            await self.metadata(ordinary["skillId"])
        view = self.f.skill_fixture.publish(draft)
        self.f.skill_fixture.now = 301
        with self.assertRaises(HTTPException):
            await self.metadata(view["skillId"])
        self.f.skill_fixture.now = 100
        self.f.skill_fixture.definitions.quarantine(view["recordId"], actor="admin", reason="Private refusal")
        with self.assertRaises(HTTPException):
            await self.metadata(view["skillId"])

    async def test_foreign_scope_absent_project_and_undeclared_operation_are_denied(self):
        view = self.publish()
        for field, foreign in (("workspace_id","foreign"), ("organization_id","foreign"),
                               ("project_id",None), ("project_id","foreign")):
            original = getattr(self.f.assignment, field)
            setattr(self.f.assignment, field, foreign)
            setattr(self.f.live, field, foreign)
            # The fixture stubs requester resolution. Model the actor that the
            # real tenant-scoped Identity owner would return for this assignment,
            # rather than retaining an actor from the original tenant. The real
            # Skill/Project owner must deny this inaccessible project publication.
            self.f.broker._requester_actor.return_value = (
                self.f.actor.model_copy(update={field:foreign})
                if field in {"organization_id", "workspace_id"} else self.f.actor)
            try:
                with self.subTest(field=field, value=foreign), self.assertRaises(HTTPException) as denied:
                    await self.metadata(view["skillId"])
                self.assertEqual(denied.exception.status_code, 403)
                self.assertEqual(denied.exception.detail["code"], "assignment_reasoning_denied")
            finally:
                setattr(self.f.assignment, field, original)
                setattr(self.f.live, field, original)
                self.f.broker._requester_actor.return_value = self.f.actor
        self.f.allowed.remove("skill.procedure.read")
        with self.assertRaises(HTTPException):
            await self.metadata(view["skillId"])

    async def test_security_and_current_requester_authority_are_rechecked_each_call(self):
        view = self.publish()
        await self.metadata(view["skillId"])
        with patch.object(self.f.skill_fixture.skills.security, "assert_allowed",
                side_effect=SkillResolutionFailure("procedure_security_blocked", "blocked")):
            with self.assertRaises(HTTPException):
                await self.metadata(view["skillId"])
        self.f.broker._authorize.side_effect = HTTPException(403, "current authority revoked")
        with self.assertRaises(HTTPException):
            await self.metadata(view["skillId"])

    async def test_publication_change_during_resolution_returns_no_reference(self):
        old = self.publish()
        draft = self.f.skill_fixture.skills.update(old["skillId"], SkillUpdate(
            instructions="Successor reviewed guidance", reason="Race fixture"), actor=self.f.actor)
        original = self.f.skill_fixture.skills.validate_required_procedure
        published = []
        def validate(*args, **kwargs):
            result = original(*args, **kwargs)
            if not published:
                published.append(self.f.skill_fixture.publish(draft))
            return result
        with patch.object(self.f.skill_fixture.skills, "validate_required_procedure", side_effect=validate):
            with self.assertRaises(HTTPException) as refused:
                await self.metadata(old["skillId"])
        self.assertEqual(refused.exception.status_code, 403)
        self.assertEqual(len(published), 1)
        self.assertEqual((await self.metadata(old["skillId"]))["item"]["definitionReference"],
                         published[0]["definitionReference"])

    async def test_retirement_or_declaration_change_during_validation_returns_no_reference(self):
        view = self.publish()
        original = self.f.skill_fixture.skills.validate_required_procedure
        for change in ("fence", "declaration"):
            def validate(*args, **kwargs):
                result = original(*args, **kwargs)
                if change == "fence":
                    self.f.live.fence = self.f.assignment.fence + 1
                else:
                    self.f.allowed[:] = [x for x in self.f.allowed if x != "skill.procedure.read"]
                return result
            try:
                with self.subTest(change=change), patch.object(self.f.skill_fixture.skills,
                        "validate_required_procedure", side_effect=validate), self.assertRaises(HTTPException):
                    await self.metadata(view["skillId"])
            finally:
                self.f.live.fence = self.f.assignment.fence
                if "skill.procedure.read" not in self.f.allowed:
                    self.f.allowed.append("skill.procedure.read")

    async def test_real_live_owner_rejects_expired_assignment_and_workspace_leases(self):
        view = self.publish()
        a = self.f.assignment
        a.status = "running"; a.required_capabilities = ("command_execution",)
        a.execution_contract_version = "test/1.0"; a.sandbox = "read-only"; a.deadline_at = 500
        a.execution_workspace_id = "workspace-one"
        a.lease = NS(expires_at=200, fence=a.fence, worker_id=a.assigned_worker_id)
        self.f.live = copy.copy(a)
        worker = ExecutionWorker(id=a.assigned_worker_id, organization_id=a.organization_id,
            workspace_id=a.workspace_id, service_identity_id="worker-service", version="test",
            capabilities=a.required_capabilities, supported_execution_contract_versions=(a.execution_contract_version,),
            supported_sandbox_profiles=("read-only",), registered_by="test")
        workspace = NS(status="active", execution_id=a.execution_id, project_id=a.project_id,
            resource_ids=a.resource_ids, lease_id="resource-lease", id=a.execution_workspace_id)
        lease = NS(released_at=None, expires_at=200, execution_id=a.execution_id,
            execution_workspace_id=a.execution_workspace_id)
        workers = NS(store=NS(worker=lambda _:worker), workspaces=NS(get=lambda *args:workspace,
            store=NS(lease=lambda _:lease)))
        owner = ProcedureExecutionService(workers=workers, skills=None, discovery=None, intents=None,
            evidence=None, identity=NS(actor_for_identity=lambda *a, **k:NS(principal_kind=PrincipalKind.SERVICE)),
            authority=None, approvals=None, attention=None, control_actor=self.f.actor, clock=lambda:100)
        self.f.procedures._live = owner._live
        a.lease.expires_at = 99
        with self.assertRaises(HTTPException) as refused:
            await self.metadata(view["skillId"])
        self.assertIn("lease or fence", refused.exception.detail["message"])
        a.lease.expires_at = 200; lease.expires_at = 99
        with self.assertRaises(HTTPException) as refused:
            await self.metadata(view["skillId"])
        self.assertIn("lease or fence", refused.exception.detail["message"])

    async def test_unexpected_storage_failure_is_not_reclassified_as_missing(self):
        view = self.publish()
        error = RuntimeError("private storage sentinel")
        with patch.object(self.f.skill_fixture.definitions, "resolve", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                await self.metadata(view["skillId"])
        self.assertIs(caught.exception, error)

    def test_existing_operation_help_advertises_exclusive_metadata_mode(self):
        # Exercise the real public operation serializer without requiring any
        # new test-facing production helper on the old baseline.
        from codex_web.services.assignment_reasoning_broker import OPERATIONS
        operation = next(op for op in OPERATIONS if op.id == "skill.procedure.read")
        help_text = self.service.public_operation(operation)
        self.assertEqual(help_text["query_parameters"]["one_of"], [
            {"required":["record_id","revision","checksum"]},
            {"required":["view"],"values":{"view":["reference"]}}])
        self.assertEqual(help_text["request_body"], {"allowed":False})
        self.assertIn("metadata only", help_text["query_parameters"]["description"])
