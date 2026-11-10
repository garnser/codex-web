"""Actual scoped broker/catalog contract; ActionIntent/provider boundary records only."""
from __future__ import annotations

from types import SimpleNamespace
import unittest

from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.control_plane_broker import ControlPlaneBrokerDecision
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.execution_profile_seed import execution_profile_catalog_seed_payload
from codex_web.resources import ResourceCreate, ResourceType
from codex_web.services.code_host_action_contract import CodeHostActionContract
from codex_web.services.execution_profile_definitions import install_execution_profile_definitions
from codex_web.services.resources import ResourceCatalogService
from codex_web.storage.resource_catalog import ResourceCatalogStore
from tests import test_control_plane_broker as fixtures


class _RecordingIntents(fixtures._ExecutingActionIntents):
    def __init__(self, registry):
        super().__init__(registry)
        self.executed = []

    async def execute_claimed(self, intent_id, worker_id, *, actor):
        self.executed.append((intent_id, worker_id, actor.identity_id))
        return await super().execute_claimed(intent_id, worker_id, actor=actor)


class IssueCreateBrokerSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = fixtures.ControlPlaneBrokerTests(methodName="runTest")
        await self.f.asyncSetUp()
        self.addAsyncCleanup(self.f.asyncTearDown)
        resources = ResourceCatalogService(ResourceCatalogStore(self.f.sqlite))
        self.repository_id = resources.create(ResourceCreate(name="Existing scoped repository",
            resource_type=ResourceType.REPOSITORY), actor=self.f.identity.local_trusted_actor()).id
        self.f.authority.resources = resources
        profiles = install_execution_profile_definitions(self.f.registry)
        payload = execution_profile_catalog_seed_payload()
        payload["profiles"].append({
            "id": "orchestration-admin", "name": "Scoped issue intake", "description": "Existing prerequisite",
            "workspace_mode": "scratch", "repository_access": "none",
            "required_worker_capabilities": ["command_execution"],
            "control_plane_operations": ["control_plane.operations.list", "repository.issue.create"],
            "administrative_repository_ids": [self.repository_id],
        })
        record = self.f.registry.create_draft(DefinitionDraftCreate(
            kind="execution-profile-catalog", definition_id="execution.profiles.default",
            definition_schema_version="1.1", payload=payload, actor="fixture",
            scope_type="project", scope_id="project-a"))
        self.f.registry.publish(record.record_id, DefinitionPublishRequest(actor="fixture"))
        _, reference = profiles.resolve("orchestration-admin", project_id="project-a")
        self.f.current_assignment = self.f.assignment.model_copy(update={
            "execution_profile_id": "orchestration-admin", "execution_profile_definition": reference,
            "agent_profile": AgentProfileExecutionBinding(profile_id="orchestrator", profile_revision=8,
                profile_record_id="orchestrator-record", role_id="orchestrator",
                authority_role_id="local-admin", execution_profile_id="orchestration-admin")})
        profiles.agent_profiles = SimpleNamespace(get=lambda profile_id, actor: SimpleNamespace(
            profile_id=profile_id, lifecycle="active", revision=8, record_id="orchestrator-record",
            role_id="orchestrator", authority_role_id="local-admin", execution_profile_id="orchestration-admin"))
        registry = fixtures._ActionRegistry("gitlab")
        original_bindings = registry.list_bindings

        def bindings(actor):
            items = original_bindings(actor)
            items[0].resource_ids = (self.repository_id,)
            return items

        registry.list_bindings = bindings
        self.intents = _RecordingIntents(registry)
        self.f.service.action_intents = self.intents
        self.f.service.execution_profiles = profiles
        self.f.service.limits = self.f.service.limits.model_copy(update={
            "max_request_bytes": 128 * 1024, "max_requests_per_minute": 100})
        self.f.broker.limits = self.f.service.limits
        self.f.broker_response_timeout = 30  # Fixture deadlock guard, not product latency acceptance.
        self.path = "/api/repository-actions/issue/create?repository_resource_id=" + self.repository_id

    async def test_unsupported_issue_labels_are_rejected_before_any_intent_admission(self):
        status, _, response = await self.f._request("POST", self.path, payload={
            "parameters": {"title": "Existing prerequisite", "body": "Preserve the existing MR scope",
                           "labels": ["bug"]}, "idempotency_key": "private-invalid-issue-shape"})
        self.assertEqual(status, 400, response)
        self.assertEqual(response.get("error", {}).get("code"), "invalid_issue_create_parameters")
        self.assertTrue(response.get("error", {}).get("correlation_id"))
        self.assertEqual(self.intents.created, [])
        self.assertEqual(self.intents.claimed, [])
        self.assertEqual(self.intents.executed, [])
        event = self.f.service.audit.load().events[-1]
        self.assertEqual(event.response_status, 400)
        self.assertEqual(event.decision, ControlPlaneBrokerDecision.DENY)

    async def test_catalog_exposes_exact_canonical_issue_parameter_contract(self):
        status, _, catalog = await self.f._request("GET", "/api/control-plane-broker/operations")
        self.assertEqual(status, 200, catalog)
        operations = {item["id"]: item for item in catalog.get("operations", [])}
        self.assertIn("repository.issue.create", operations)
        schema = operations["repository.issue.create"].get("request_body", {}).get("parameters")
        self.assertEqual(schema, {
            "type": "object", "additionalProperties": False, "required": ["title"],
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 256, "pattern": r"\S"},
                "body": {"type": "string", "maxLength": 65536, "default": ""},
            },
        })
        self.assertEqual(self.intents.created, [])
        operator_operation = next(item for item in self.f.service.public_assignment_capability(
            self.f.current_assignment)["operations"] if item["id"] == "repository.issue.create")
        self.assertEqual(operator_operation.get("request_body", {}).get("parameters"), schema)

    async def test_canonical_invalid_title_body_and_extra_fields_never_create_an_intent(self):
        invalid = ({}, {"title": " \n\t"}, {"title": 1}, {"title": "t" * 257},
                   {"title": "Existing prerequisite", "body": None},
                   {"title": "Existing prerequisite", "body": "b" * 65537},
                   {"title": "Existing prerequisite", "private_extra": "not accepted"})
        for index, parameters in enumerate(invalid):
            with self.subTest(index=index):
                request = fixtures.ActionRequest(action_id="code-host.issue.create", organization_id="local",
                    workspace_id="default", parameters=parameters)
                with self.assertRaises(ValueError):
                    CodeHostActionContract.issue_create(request)
                status, _, response = await self.f._request("POST", self.path, payload={
                    "parameters": parameters, "idempotency_key": f"invalid-issue-shape-{index}"})
                self.assertEqual(status, 400, response)
                self.assertEqual(response.get("error", {}).get("code"), "invalid_issue_create_parameters")
                self.assertEqual(self.intents.created, [])
                self.assertEqual(self.intents.claimed, [])
                self.assertEqual(self.intents.executed, [])

    async def test_valid_title_body_optional_body_and_exact_bounds_preserve_original_request(self):
        valid = ({"title": " Existing prerequisite ", "body": " Retain scope \n"},
                 {"title": "Existing prerequisite"}, {"title": "t" * 256, "body": "b" * 65536})
        for index, parameters in enumerate(valid):
            with self.subTest(index=index):
                status, _, response = await self.f._request("POST", self.path, payload={
                    "parameters": parameters, "idempotency_key": f"valid-issue-shape-{index}"})
                self.assertEqual(status, 200, response)
                self.assertEqual(len(self.intents.created), index + 1)
                created, actor = self.intents.created[-1]
                self.assertEqual(created.request.parameters, parameters)
                self.assertEqual(created.request.resource_ids, (self.repository_id,))
                self.assertEqual(created.request.idempotency_key, f"valid-issue-shape-{index}")
                self.assertEqual(created.execution_id, self.f.current_assignment.execution_id)
                self.assertEqual(actor.identity_id, "local-admin")
                self.assertEqual(len(self.intents.claimed), index + 1)
                self.assertEqual(len(self.intents.executed), index + 1)
