"""Real governed Memory refusal through the assignment-bound HTTP socket."""
import copy
import json
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from codex_web.data_governance import DataClassification
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.execution_workers import ExecutionWorker
from codex_web.executive_roles import ExecutiveActivationCreate
from codex_web.models import Project
from codex_web.resources import ResourceCreate, ResourceType
from codex_web.organizational_memory import (
    KnowledgeCreate, KnowledgeObjectType, KnowledgeProvenance, KnowledgeSourceKind,
)
from codex_web.services.assignment_reasoning_broker import AssignmentReasoningBroker
from codex_web.services.control_plane_broker import AssignmentBoundControlPlaneBroker
from codex_web.services.data_governance import DataGovernanceService
from codex_web.services.execution_profile_definitions import install_execution_profile_definitions
from codex_web.services.executive_management import ExecutiveContextError
from codex_web.services.organizational_memory import OrganizationalMemoryService
from codex_web.services.procedure_execution import ProcedureExecutionService
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.work_graph import WorkGraphService
from codex_web.storage.data_governance import DataGovernanceStore
from codex_web.storage.organizational_memory import OrganizationalMemoryStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.work_graph import WorkGraphStore
from tests import test_control_plane_broker as socket_fixture
from tests import test_executive_management as executive_fixture


class ExecutiveContextBrokerRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.socket = socket_fixture.ControlPlaneBrokerTests()
        await self.socket.asyncSetUp(start_broker=False)
        self.addAsyncCleanup(self.socket.asyncTearDown)
        self.executive = executive_fixture.ExecutiveManagementTests()
        await self.executive.asyncSetUp()
        self.addAsyncCleanup(self.executive.asyncTearDown)
        fixture = self.socket
        self.actor = fixture.identity.local_trusted_actor()
        resources = ResourceCatalogService(ResourceCatalogStore(fixture.sqlite))
        repository = resources.create(ResourceCreate(name="Architecture repository",
            resource_type=ResourceType.REPOSITORY), actor=self.actor)
        resources.bind_project(project=Project(id="project-a", organization_id="local",
            workspace_id="default", name="Architecture", path=fixture.temp.name),
            resource_id=repository.id, actor=self.actor)
        fixture.authority.resources = resources
        self.executive.service.work_graph = WorkGraphService(
            WorkGraphStore(self.executive.store.store), lambda: {})
        self.memory = OrganizationalMemoryService(
            OrganizationalMemoryStore(fixture.sqlite),
            DataGovernanceService(DataGovernanceStore(fixture.sqlite)), fixture.authority)
        self.executive.service.organizational_memory = self.memory
        profiles = install_execution_profile_definitions(fixture.registry)
        active = profiles.record(organization_id="local", workspace_id="default", project_id="project-a")
        payload = copy.deepcopy(active.payload)
        for profile in payload["profiles"]:
            if profile["id"] == "orchestration-only":
                profile["control_plane_operations"] = [
                    "executive.activation.create", "executive.activation.read", "executive.activation.consult"]
        draft = fixture.registry.create_draft(DefinitionDraftCreate(
            definition_id=active.definition_id, kind=active.kind,
            definition_schema_version=active.definition_schema_version, payload=payload,
            actor="test", reason="Explicit Executive broker fixture"))
        fixture.registry.approve_publication(draft.record_id, actor="test-approver",
            reference="TEST-APPROVAL", reason="Fixture declaration")
        fixture.registry.publish(draft.record_id, DefinitionPublishRequest(
            actor="test", reason="Current explicit fixture", expected_active_revision=active.revision))
        _, reference = profiles.resolve("orchestration-only", organization_id="local",
            workspace_id="default", project_id="project-a")
        fixture.assignment.execution_profile_definition = reference
        fixture.assignment.resource_ids = (repository.id,)
        fixture.assignment.execution_workspace_id = "workspace-private"
        worker = ExecutionWorker(id="worker-1", organization_id="local", workspace_id="default",
            service_identity_id=fixture.worker_actor.identity_id, version="test",
            capabilities=fixture.assignment.required_capabilities,
            supported_execution_contract_versions=(fixture.assignment.execution_contract_version,),
            registered_by="test")
        workspace = NS(status="active", execution_id=fixture.assignment.execution_id,
            project_id="project-a", resource_ids=(repository.id,), lease_id="lease-private", id="workspace-private")
        resource_lease = NS(released_at=None, expires_at=time.time()+120,
            execution_id=fixture.assignment.execution_id, execution_workspace_id="workspace-private")
        workers = NS(store=NS(assignment=lambda _:fixture.current_assignment, worker=lambda _:worker),
            workspaces=NS(get=lambda *a:workspace, store=NS(lease=lambda _:resource_lease)))
        owner = ProcedureExecutionService(workers=workers, skills=None, discovery=None,
            intents=None, evidence=None, identity=fixture.identity, authority=fixture.authority,
            approvals=None, attention=None, control_actor=self.actor)
        fixture.service.execution_workers = workers
        fixture.service.execution_profiles = profiles
        self.adapter = AssignmentReasoningBroker(fixture.service,
            procedures=owner, executives=self.executive.service)
        fixture.service.assignment_reasoning = self.adapter
        # The broker seals its immutable assignment snapshot at construction.
        # Start only after the canonical Profile and complete custody exist.
        fixture.broker = AssignmentBoundControlPlaneBroker(fixture.service,
            assignment=fixture.assignment, worker_id="worker-1",
            worker_actor=fixture.worker_actor, fence=1,
            validator=lambda:fixture.current_assignment)
        await fixture.broker.start()

    def request(self):
        return ExecutiveActivationCreate(subject="Architecture context",
            request="Review architecture context policy", project_id="project-a",
            trigger_ref=self.adapter.origin(self.socket.assignment),
            requested_role_ids=("cto",), max_roles=1).model_dump(mode="json")

    def assert_audit(self, response, operation, status):
        events = [event for event in self.socket.service.audit.load().events
            if event.correlation_id == response["error"]["correlation_id"]]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].operation_id, operation)
        self.assertEqual(events[0].response_status, status)
        self.assertEqual(events[0].decision.value, "deny" if status < 500 else "error")
        self.assertEqual(events[0].denial_reason, response["error"]["message"])

    async def boundary_failures(self, error, expected_status):
        # Fault controls for each exposed operation; the Memory test below owns
        # the real composed cause. No alternate authorization or owner is used.
        status, _, created = await self.socket._request(
            "POST", "/api/executive/activations", payload=self.request())
        self.assertEqual(status, 200, created)
        activation_id = created["item"]["id"]
        before = self.executive.store.load().model_dump(mode="json")
        for operation, method, path, owner_method in (
            ("executive.activation.create", "POST", "/api/executive/activations", "list"),
            ("executive.activation.read", "GET", "/api/executive/activations", "list"),
            ("executive.activation.consult", "POST", f"/api/executive/activations/{activation_id}/consult", "consult"),
        ):
            with self.subTest(operation=operation):
                replacement = AsyncMock(side_effect=error) if owner_method == "consult" else None
                kwargs = {"new": replacement} if replacement is not None else {"side_effect": error}
                with patch.object(self.executive.service, owner_method, **kwargs):
                    status, _, response = await self.socket._request(method, path,
                        payload=self.request() if operation.endswith("create") else None)
                self.assertEqual(status, expected_status, response)
                if expected_status == 409:
                    self.assertEqual(response["error"]["code"], "executive_context_unavailable")
                self.assertNotIn("private failure context", json.dumps(response))
                self.assert_audit(response, operation, expected_status)
                self.assertEqual(self.executive.store.load().model_dump(mode="json"), before)
                self.assertEqual(self.executive.gateway.requests, [])

    async def test_known_context_failures_are_typed_across_create_read_consult(self):
        await self.boundary_failures(ExecutiveContextError("private failure context"), 409)

    async def test_unexpected_failures_remain_internal_errors(self):
        await self.boundary_failures(ValueError("private failure context"), 500)

    async def test_project_and_origin_denials_remain_forbidden(self):
        for field, value in (("project_id", "foreign-project"), ("trigger_ref", "foreign-origin")):
            request = self.request()
            request[field] = value
            status, _, response = await self.socket._request("POST", "/api/executive/activations", payload=request)
            self.assertEqual(status, 403, response)
            self.assertNotEqual(response["error"]["code"], "executive_context_unavailable")
            self.assert_audit(response, "executive.activation.create", 403)
        self.assertEqual(len(self.executive.store.load().activations), 0)
        self.assertEqual(self.executive.gateway.requests, [])

    async def test_unknown_memory_scope_returns_typed_http_refusal_and_matching_audit(self):
        # Canonical legacy/company Memory has no governed Resource projection.
        # The real retrieval must deny it, rather than authorize or discard it.
        private_text = "private architecture context must never enter broker errors"
        knowledge = self.memory.create(KnowledgeCreate(
            logical_key="architecture/private", object_type=KnowledgeObjectType.POLICY,
            title="Architecture context", summary="Architecture context policy",
            content=private_text, classification=DataClassification.INTERNAL,
            provenance=KnowledgeProvenance(source_kind=KnowledgeSourceKind.REPOSITORY,
                source_ref="repo://private/policy", source_revision="private-revision",
                authored_by="test")), actor=self.actor)
        request = self.request()
        status, headers, response = await self.socket._request(
            "POST", "/api/executive/activations", payload=request)
        # Check the actual cause before checking the missing transport adapter.
        runs = self.memory.retrieval_runs(actor=self.actor)
        self.assertEqual(len(runs), 1, (status, response))
        self.assertEqual([(item.knowledge_id, item.reason) for item in runs[0].denied],
            [(knowledge.id, "resource_scope:unknown")])
        self.assertEqual(len(self.executive.store.load().activations), 0)
        self.assertEqual(self.executive.gateway.requests, [])
        self.assertEqual(status, 409, response)
        self.assertEqual(response["error"]["code"], "executive_context_unavailable")
        correlation = response["error"]["correlation_id"]
        self.assertEqual(headers["x-correlation-id"], correlation)
        serialized = json.dumps(response)
        for private in (private_text, knowledge.id, runs[0].id, "ExecutiveContextError",
                        "resource_scope:unknown", "repo://private/policy"):
            self.assertNotIn(private, serialized)
        events = [event for event in self.socket.service.audit.load().events
            if event.correlation_id == correlation]
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.operation_id, "executive.activation.create")
        self.assertEqual(event.response_status, 409)
        self.assertEqual(event.decision.value, "deny")
        self.assertEqual(event.assignment_id, self.socket.assignment.id)
        self.assertEqual(event.denial_reason, response["error"]["message"])


if __name__ == "__main__":
    unittest.main()
