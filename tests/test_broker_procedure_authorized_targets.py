"""Real private broker/authority/discovery; external action dispatch is synthetic."""
import unittest
from unittest.mock import AsyncMock, patch

from codex_web.agent_profiles import AgentProfileCreate, AgentProfileExecutionBinding
from codex_web.execution_workers import (
    AssignmentClaimRequest, AssignmentCompleteRequest, AssignmentStartRequest,
    ExecutionAssignmentCreate, WorkerCapability,
)
from codex_web.resources import (
    RepositoryExecutionScope, RepositoryExecutionTarget, ResourceCreate,
    ResourceOperationalContext, ResourceType, ResourceUpdate,
)
from codex_web.services.action_providers import ActionProviderRegistry
from codex_web.services.control_plane_broker import AssignmentBoundControlPlaneBroker
from codex_web.services.procedure_discovery import BrokerProcedureAdmissionService, ProcedureDiscoveryService
from codex_web.services.skills import SkillService
from codex_web.services.skill_scanners import SkillScannerRegistry
from codex_web.services.skill_security import SkillSecurityService
from codex_web.skills import SkillCreate, SkillPublish
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.storage.skill_security import SkillSecurityStore
from tests import test_administrative_broker_runtime as fixtures
from tests import test_skill_procedures as procedures
from tests.test_skill_security import FakeScanner


class _RepositoryProcedureProvider(procedures.MetadataProvider):
    def actions(self):
        return tuple(action.model_copy(update={"required_resource_types": (ResourceType.REPOSITORY,)})
                     for action in super().actions())


class AuthorizedProcedureTargetTests(unittest.IsolatedAsyncioTestCase):
    _assignment = staticmethod(fixtures.AdministrativeBrokerRuntimeTests._assignment)
    _publish_worker_authority = fixtures.AdministrativeBrokerRuntimeTests._publish_worker_authority
    _publish_profile = fixtures.AdministrativeBrokerRuntimeTests._publish_profile
    _request = fixtures.AdministrativeBrokerRuntimeTests._request
    asyncTearDown = fixtures.AdministrativeBrokerRuntimeTests.asyncTearDown

    async def asyncSetUp(self):
        await fixtures.AdministrativeBrokerRuntimeTests.asyncSetUp(self,
            operations=("repository.pull-request.merge",), prepare_only=True)
        self.resources.update(self.target.id, ResourceUpdate(
            operational_context=ResourceOperationalContext(environment="development")), actor=self.admin)
        actions = ActionProviderRegistry(ActionProviderStateStore(self.sqlite))
        actions.register(_RepositoryProcedureProvider(), tenant_scope=self.admin.tenant)
        skills = SkillService(self.registry, clock=lambda: 100)
        skills.bind_procedure_contracts(actions=actions, resources=self.resources,
                                       scope_actor=lambda record: self.admin)
        scanners = SkillScannerRegistry()
        scanners.register(FakeScanner())
        skills.bind_security(SkillSecurityService(SkillSecurityStore(self.sqlite),
            self.registry, scanners, clock=lambda: 100))
        content = procedures.procedure()
        content.update(operation_triggers=["repository.pull-request.merge"],
                       environments=["development"], service_versions=[], required_worker_capabilities=[])
        content["resource_selectors"][0]["resource_types"] = ["repository"]
        for step in content["steps"]:
            if "worker" in step:
                del step["worker"]
                step["action"] = dict(content["steps"][0]["action"])
        draft = skills.create(SkillCreate(skill_id="repository-merge-procedure",
            name="Repository merge guidance",
            instructions="Consult the canonical repository release checks.",
            operational_procedure=content), actor=self.admin)
        self.published = skills.publish(draft["skillId"], draft["recordId"], SkillPublish(), actor=self.admin)
        self.service.procedure_admission = BrokerProcedureAdmissionService(
            ProcedureDiscoveryService(skills, self.resources), self.workers, self.admin)
        self.handler = AsyncMock(return_value={"synthetic_external_handler": True})
        self.service._execute_repository_action = self.handler
        await self._start_broker()

    async def _start_broker(self):
        self.broker = AssignmentBoundControlPlaneBroker(self.service,
            assignment=self.assignment, worker_id=self.worker.id, worker_actor=self.worker_actor,
            fence=self.assignment.fence,
            validator=lambda: self.workers.assignment_for_execution(self.assignment.execution_id, actor=self.admin))
        await self.broker.start()

    async def merge(self, repository_id=None, *, query=True, parameters=None):
        path = "/api/repository-actions/pull-request/merge"
        if query:
            path += "?repository_resource_id=" + (repository_id or self.target.id)
        return await self._request("POST", path,
            payload={"parameters": parameters or {"pull_request_number": 62}})

    async def test_admin_scratch_merge_keeps_authorized_target_and_canonical_environment(self):
        self.assertEqual(self.assignment.resource_ids, ())
        with patch.object(self.service, "_operation_repository_id",
                          wraps=self.service._operation_repository_id) as resolve:
            status, _, response = await self.merge()
        self.assertEqual(status, 200, response)
        resolve.assert_called_once()
        discovery = self.workers.store.assignment(self.assignment.id).procedure_discoveries[-1]
        self.assertTrue(discovery.admitted)
        self.assertEqual(discovery.resource_ids, (self.target.id,))
        self.assertEqual(discovery.selected[0].record_id, self.published["recordId"])
        self.handler.assert_awaited_once()
        self.assertEqual(self.handler.await_args.kwargs["repository_id"], self.target.id)
        self.assertEqual(self.workers.store.assignment(self.assignment.id).resource_ids, ())

    async def test_environment_cannot_be_inferred_from_name_or_request_parameters(self):
        self.resources.update(self.target.id, ResourceUpdate(name="Development repository",
            operational_context=ResourceOperationalContext()), actor=self.admin)
        status, _, response = await self.merge(parameters={"pull_request_number": 62,
                                                         "environment": "development"})
        self.assertEqual(status, 409, response)
        self.assertIn("environment_unresolved", response["error"]["procedure_discovery"]["blockers"])
        self.handler.assert_not_awaited()

    async def test_undeclared_repository_is_denied_before_discovery_or_handler(self):
        other = self.resources.create(ResourceCreate(resource_type=ResourceType.REPOSITORY,
            name="Other Development repository",
            operational_context=ResourceOperationalContext(environment="development")), actor=self.admin)
        status, _, response = await self.merge(other.id)
        self.assertEqual(status, 403, response)
        self.assertEqual(self.workers.store.assignment(self.assignment.id).procedure_discoveries, ())
        self.handler.assert_not_awaited()

    async def test_admin_request_requires_an_explicit_repository(self):
        status, _, response = await self.merge(query=False)
        self.assertEqual(status, 400, response)
        self.handler.assert_not_awaited()

    async def test_ordinary_repository_scope_is_preserved_and_cannot_be_overridden(self):
        await self.broker.stop()
        old = self.assignment
        self.workers.complete(self.worker.id, old.id, AssignmentCompleteRequest(
            fence=old.fence, lease_token=old.lease.lease_token, succeeded=True), actor=self.worker_actor)
        profile = self.agent_profiles.create(AgentProfileCreate(profile_id="repository-implementer",
            name="Repository implementer", role_id="orchestrator", authority_role_id="local-admin",
            execution_profile_id="repository-write"), actor=self.admin)
        _, definition = self.profiles.resolve("repository-write", project_id="project-a")
        tenant = dict(organization_id="local", workspace_id="default", project_id="project-a", source="explicit")
        pending = self.workers.create_assignment(ExecutionAssignmentCreate(
            subject=old.subject.model_copy(update={"ref": "ordinary-repository-target"}),
            execution_id="ordinary-repository-target", project_id="project-a", resource_ids=(self.target.id,),
            repository_target=RepositoryExecutionTarget(**tenant, mutable_repository_id=self.target.id),
            repository_scope=RepositoryExecutionScope(**tenant, writable_repository_ids=(self.target.id,)),
            execution_contract_version="thread-bootstrap/1.0", required_capabilities=(WorkerCapability.COMMAND_EXECUTION,),
            sandbox="workspace-write", approval_policy="on-request", execution_profile_id="repository-write",
            execution_profile_definition=definition, agent_profile=AgentProfileExecutionBinding(
                profile_id=profile.profile_id, profile_revision=profile.revision, profile_record_id=profile.record_id,
                role_id=profile.role_id, authority_role_id=profile.authority_role_id,
                execution_profile_id="repository-write", authority_definition_ref=profile.authority_definition_ref)),
            actor=self.admin)
        claimed = self.workers.claim(self.worker.id, AssignmentClaimRequest(),
                                     actor=self.worker_actor, assignment_id=pending.id)
        self.assignment = self.workers.start(self.worker.id, claimed.id, AssignmentStartRequest(
            fence=claimed.fence, lease_token=claimed.lease.lease_token), actor=self.worker_actor)
        await self._start_broker()
        status, _, response = await self.merge(query=False)
        self.assertEqual(status, 200, response)
        discovery = self.workers.store.assignment(self.assignment.id).procedure_discoveries[-1]
        self.assertEqual(discovery.resource_ids, (self.target.id,))
        self.assertEqual(self.handler.await_args.kwargs["repository_id"], self.target.id)
        self.handler.reset_mock()
        status, _, response = await self.merge()
        self.assertEqual(status, 400, response)
        self.handler.assert_not_awaited()
