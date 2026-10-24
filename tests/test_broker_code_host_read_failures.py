from __future__ import annotations

import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx

from tests import test_control_plane_broker as fixture
from codex_web.authority import (AUTHORITY_ROLE_CATALOG_ID, AUTHORITY_ROLE_CATALOG_KIND,
    AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION, AuthorityRoleCatalogDefinition, AuthorityGrant, AuthorityLevel)
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.resources import RepositoryExecutionScope, RepositoryTargetSource
from codex_web.execution_workers import AssignmentStatus
from codex_web.code_hosts import CodeHostTransientError
from codex_web.services.control_plane_broker import ControlPlaneBrokerService, AssignmentBoundControlPlaneBroker
from codex_web.storage.control_plane_broker import ControlPlaneBrokerAuditStore
from codex_web.services.gitlab_code_host import GitLabCodeHostProvider
from codex_web.services.github_code_host import GitHubCodeHostProvider
from codex_web.integrations.gitlab_client import GitLabAPIError
from codex_web.integrations.github_client import GitHubAPIError


class BrokerCodeHostReadFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = fixture.ControlPlaneBrokerTests()
        await self.f.asyncSetUp(start_broker=False)
        f = self.f
        active = f.registry.resolve(definition_id=AUTHORITY_ROLE_CATALOG_ID, kind=AUTHORITY_ROLE_CATALOG_KIND)
        catalog = AuthorityRoleCatalogDefinition.model_validate(active.payload)
        catalog = catalog.model_copy(update={"roles": tuple(
            role.model_copy(update={"grants": (*role.grants, AuthorityGrant(
                id="fixture.commit.read", capability="repository.commit.read", level=AuthorityLevel.READ,
                project_ids=("project-a",)))}) if role.id == "orchestration-worker" else role
            for role in catalog.roles)})
        draft = f.registry.create_draft(DefinitionDraftCreate(definition_id=AUTHORITY_ROLE_CATALOG_ID,
            kind=AUTHORITY_ROLE_CATALOG_KIND, definition_schema_version=AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
            payload=catalog.model_dump(mode="json"), actor="test", reason="exact commit read fixture"))
        f.registry.approve_publication(draft.record_id, actor="test-approver", reference="TEST-APPROVAL",
            reason="exact commit read fixture")
        f.registry.publish(draft.record_id, DefinitionPublishRequest(actor="test",
            reason="exact commit read fixture", expected_active_revision=active.revision))
        f.authority.resources = NS(get=lambda resource_id, actor: NS(id=resource_id, lifecycle="active",
            resource_type="repository", risk="medium", sensitivity="internal"))
        self.hosts = fixture._CodeHosts()
        self.client = NS(get_json=AsyncMock())
        self.provider = GitLabCodeHostProvider(self.client)
        async def commit(binding_id, resource_id, revision, *, actor):
            return await self.provider.commit(self.hosts.registry.binding,
                NS(name="synthetic/repo", aliases=(), provenance=NS(external_url=None,
                    external_id="synthetic/repo")), revision, credential="synthetic-test-only")
        self.hosts.commit = commit
        f.service = ControlPlaneBrokerService(identity=f.identity, authority=f.authority,
            work_items=f.work_items, operator=fixture._Operator(), audit=ControlPlaneBrokerAuditStore(f.sqlite),
            action_intents=NS(execution=NS(registry=fixture._ActionRegistry())), code_hosts=self.hosts)
        f.assignment = f.assignment.model_copy(update={"execution_profile_id": "repository-write",
            "resource_ids": ("repository-a",), "repository_scope": RepositoryExecutionScope(
                organization_id="local", workspace_id="default", project_id="project-a",
                writable_repository_ids=("repository-a",), source=RepositoryTargetSource.SINGLE_REPOSITORY,
                source_ref="repository-a")})
        f.current_assignment = f.assignment
        f.broker = AssignmentBoundControlPlaneBroker(f.service, assignment=f.assignment, worker_id="worker-1",
            worker_actor=f.worker_actor, fence=1, validator=lambda: f.current_assignment)
        await f.broker.start()

    async def asyncTearDown(self):
        await self.f.asyncTearDown()

    async def read(self, correlation):
        return await self.f._request("GET", "/api/repository-facts/commits/destination",
            headers={"X-Correlation-ID": correlation})

    async def test_typed_http_failures_preserve_safe_status_without_absence_or_retry(self):
        for provider_type, error_type in ((GitLabCodeHostProvider, GitLabAPIError),
                                          (GitHubCodeHostProvider, GitHubAPIError)):
            self.provider = provider_type(self.client)
            for status in (401, 403, 404, 429, 503):
                with self.subTest(provider=provider_type.__name__, status=status):
                    error = error_type(status, "private/provider/path") if error_type is GitLabAPIError else error_type(status)
                    self.client.get_json.side_effect = error
                    correlation = f"read-{provider_type.__name__}-{status}"
                    actual, headers, payload = await self.read(correlation)
                    self.assertEqual(actual, 409)
                    self.assertEqual(payload["error"]["code"], "repository_read_provider_error")
                    self.assertIn(f"HTTP {status}", payload["error"]["message"])
                    self.assertIn("absence and permission are unproven; no retry is authorized", payload["error"]["message"])
                    self.assertNotIn("private/provider/path", str(payload))
                    self.assertNotIn("synthetic-test-only", str(payload))
                    self.assertEqual(headers["x-correlation-id"], correlation)
                    event = next(e for e in self.f.service.audit.load().events if e.correlation_id == correlation)
                    self.assertEqual((str(event.decision), event.response_status), ("deny", 409))
                    self.assertEqual(event.operation_id, "repository.read.commit")
                    self.assertIn(f"HTTP {status}", event.denial_reason)
                    self.assertNotIn("private/provider/path", event.denial_reason)
        self.assertEqual(self.client.get_json.await_count, 10)

    async def test_transport_failures_are_typed_and_still_transient_at_provider(self):
        for provider_type in (GitLabCodeHostProvider, GitHubCodeHostProvider):
            self.provider = provider_type(self.client)
            for error in (httpx.ReadTimeout("private-network-error"), httpx.ConnectError("private-network-error")):
                with self.subTest(provider=provider_type.__name__, error=type(error).__name__):
                    self.assertIsInstance(self.provider._classify(error), CodeHostTransientError)
                    self.client.get_json.side_effect = error
                    actual, _, payload = await self.read("transport-" + provider_type.__name__ + type(error).__name__)
                    self.assertEqual(actual, 409)
                    self.assertEqual(payload["error"]["code"], "repository_read_transport_error")
                    self.assertNotIn("private-network-error", str(payload))

    async def test_unknown_errors_and_legacy_message_are_not_typed_http_witnesses(self):
        for provider_type in (GitLabCodeHostProvider, GitHubCodeHostProvider):
            self.provider = provider_type(self.client)
            for error in (RuntimeError("private implementation error"), RuntimeError("HTTP 404 private path")):
                with self.subTest(provider=provider_type.__name__, error=str(error)):
                    self.client.get_json.side_effect = error
                    actual, _, payload = await self.read("unknown-error")
                    self.assertEqual(actual, 500)
                    self.assertEqual(payload["error"]["code"], "control_plane_broker_denied")
                    self.assertNotIn("private", str(payload))

    async def test_success_and_current_custody_denial_preserve_existing_behavior(self):
        self.client.get_json.return_value = {"id": "a" * 40, "message": "synthetic commit"}
        actual, _, payload = await self.read("success")
        self.assertEqual(actual, 200)
        self.assertEqual(payload["item"]["revision"], "a" * 40)
        self.f.current_assignment = self.f.assignment.model_copy(update={"status": AssignmentStatus.CANCELLED, "fence": 2, "lease": None})
        actual, _, payload = await self.read("revoked")
        self.assertEqual(actual, 403)
        self.assertEqual(payload["error"]["code"], "control_plane_broker_denied")
        self.assertEqual(self.client.get_json.await_count, 1)
