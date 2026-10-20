"""Real scoped Unix-socket broker/CodeHost validation; provider and secrets stubbed."""
from __future__ import annotations

import json
from types import SimpleNamespace
import unittest

from codex_web.code_hosts import CodeHostCapability, CodeHostError, CodeHostPullRequestFact
from codex_web.control_plane_broker import ControlPlaneBrokerDecision
from codex_web.resources import Resource, ResourceType, RepositoryExecutionScope, RepositoryTargetSource
from codex_web.services.code_hosts import CodeHostRegistry, CodeHostService
from codex_web.services.control_plane_broker import AssignmentBoundControlPlaneBroker
from tests import test_control_plane_broker as fixtures


class _Provider:
    provider_type = "github"

    def __init__(self):
        self.states = []
        self.failure = None

    def capabilities(self):
        return frozenset({CodeHostCapability.PULL_REQUEST_READ})

    async def pull_requests(self, binding, resource, *, credential, state):
        self.states.append(state)
        if self.failure:
            raise self.failure
        return (CodeHostPullRequestFact(external_id="954", number=954,
            title="Original scoped change", state="open", source_ref="fix/original", target_ref="main"),)


class _Secrets:
    def __init__(self):
        self.calls = []

    async def use_async(self, secret_id, *, actor, operation, consumer, context=None):
        self.calls.append((secret_id, operation))
        return await consumer("private-fixture-credential")


class BrokerPullRequestFilterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = fixtures.ControlPlaneBrokerTests()
        await self.f.asyncSetUp(start_broker=False)
        resource = Resource(id="repository-a", organization_id="local", workspace_id="default",
                            resource_type=ResourceType.REPOSITORY, name="Scoped repository")
        # Resource lookup and external transport use fixtures; identity, Role
        # evaluation, assignment scoping, CodeHost validation, socket and audit are real.
        resources = SimpleNamespace(get=lambda resource_id, actor: resource)
        self.f.authority.resources = resources
        self.provider, self.secrets = _Provider(), _Secrets()
        registry = CodeHostRegistry()
        registry.register_provider(self.provider)
        self.f.service.code_hosts = CodeHostService(registry, resources, secrets=self.secrets)
        self.f.service.action_intents = SimpleNamespace(execution=SimpleNamespace(registry=fixtures._ActionRegistry()))
        self.f.assignment = self.f.assignment.model_copy(update={
            "execution_profile_id": "repository-write", "resource_ids": (resource.id,),
            "repository_scope": RepositoryExecutionScope(organization_id="local", workspace_id="default",
                project_id="project-a", writable_repository_ids=(resource.id,),
                source=RepositoryTargetSource.SINGLE_REPOSITORY, source_ref=resource.id),
        })
        self.f.current_assignment = self.f.assignment
        self.f.broker = AssignmentBoundControlPlaneBroker(self.f.service,
            assignment=self.f.assignment, worker_id="worker-1", worker_actor=self.f.worker_actor,
            fence=1, validator=lambda: self.f.current_assignment)
        await self.f.broker.start()

    async def asyncTearDown(self):
        await self.f.asyncTearDown()

    async def test_catalog_advertises_provider_neutral_state_filter_and_default(self):
        status, _, response = await self.f._request("GET", "/api/control-plane-broker/operations")
        self.assertEqual(status, 200)
        operation = next(x for x in response["operations"] if x["id"] == "repository.read.pull-requests")
        self.assertIn("state", operation["optional_query_parameters"])
        self.assertEqual(operation["query_parameters"]["state"],
                         {"type": "string", "enum": ["open", "closed", "all"], "default": "open"})

    async def test_unsupported_state_is_audited_client_error_before_secret_or_provider_access(self):
        for state in ("opened", "merged", "private-fixture-credential"):
            with self.subTest(state=state):
                status, _, response = await self.f._request("GET", "/api/repository-facts/pull-requests?state="+state)
                self.assertEqual(status, 400)
                self.assertEqual(response["error"]["code"], "invalid_pull_request_state")
                self.assertIn("open, closed, or all", response["error"]["message"])
                self.assertNotIn(state, response["error"]["message"])
                event = self.f.service.audit.load().events[-1]
                self.assertEqual(event.decision, ControlPlaneBrokerDecision.DENY)
                self.assertEqual(event.response_status, 400)
                self.assertEqual(event.correlation_id, response["error"]["correlation_id"])
        self.assertEqual(self.secrets.calls, [])
        self.assertEqual(self.provider.states, [])

    async def test_default_and_supported_filters_keep_normalization_and_scoped_read(self):
        for suffix in ("", "?state=%20OPEN%20", "?state=closed", "?state=all"):
            status, _, response = await self.f._request("GET", "/api/repository-facts/pull-requests"+suffix)
            self.assertEqual(status, 200)
            self.assertEqual(response["items"][0]["number"], 954)
        self.assertEqual(self.provider.states, ["open", "open", "closed", "all"])
        self.assertEqual(len(self.secrets.calls), 4)

    async def test_foreign_repository_stays_denied_before_filter_validation(self):
        status, _, _ = await self.f._request("GET",
            "/api/repository-facts/pull-requests?state=opened&repository_resource_id=foreign-repository")
        self.assertEqual(status, 403)
        self.assertEqual(self.secrets.calls, [])
        self.assertEqual(self.provider.states, [])

    async def test_genuine_provider_error_is_not_reclassified_as_invalid_input_or_leaked(self):
        self.provider.failure = CodeHostError("private-fixture-credential upstream failure")
        status, _, response = await self.f._request("GET", "/api/repository-facts/pull-requests?state=open")
        self.assertEqual(status, 500)
        self.assertNotIn("private-fixture-credential", json.dumps(response))
        self.assertNotEqual(response["error"]["code"], "invalid_pull_request_state")
        self.assertEqual(self.f.service.audit.load().events[-1].decision, ControlPlaneBrokerDecision.ERROR)
        self.assertEqual(len(self.secrets.calls), 1)
        self.assertEqual(self.provider.states, ["open"])
