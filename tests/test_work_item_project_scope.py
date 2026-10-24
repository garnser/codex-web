"""Requested Project fences supplement the existing tenant boundary on Work Items."""
from types import SimpleNamespace
import asyncio
import threading
from unittest.mock import AsyncMock, Mock, patch

import unittest
import tempfile
from pathlib import Path
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from codex_web.api.authorization import ApiAuthorizationError, install_api_authorization
from codex_web.authority import (AUTHORITY_ROLE_CATALOG_ID, AUTHORITY_ROLE_CATALOG_KIND,
    AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION, AuthorityGrant, AuthorityLevel,
    AuthorityRoleBinding, AuthorityRoleCatalogDefinition, AuthorityRoleDefinition)
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, PrincipalKind, MembershipRole
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore

from codex_web.api.work_items import build_work_items_router


class WorkItemProjectScopeTests(unittest.TestCase):
    def setUp(self):
        state = SimpleNamespace(organization_id="org-a", workspace_id="workspace-a", project_id="project-a")
        service = SimpleNamespace(
            state_machine=SimpleNamespace(_work_item_state=lambda ref: state),
            work_items=SimpleNamespace(),
            get=AsyncMock(return_value={"ref": "item-a", "project_id": "project-a"}),
        )
        operator = SimpleNamespace(
            require_handoff_inspection_ready=Mock(),
            handoff_eligibility=Mock(return_value={'inspection_only': True, 'eligible': False}),
            detail=Mock(return_value={"item": {"ref": "item-a"}}),
            retry=AsyncMock(return_value={"item": {"ref": "item-a"}}),
            reconcile=AsyncMock(return_value={"item": {"ref": "item-a"}}),
        )
        app = FastAPI()

        @app.middleware("http")
        async def tenant_scope(request, call_next):
            request.state.tenant_scope = SimpleNamespace(organization_id="org-a", workspace_id="workspace-a")
            request.state.identity_actor = SimpleNamespace(
                identity_id="canonical-operator"
            )
            return await call_next(request)

        with patch("codex_web.api.work_items.WorkItemOperatorService", return_value=operator):
            app.include_router(build_work_items_router(service))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.service, self.operator, self.state = service, operator, state

    def test_foreign_project_rejected_before_read_or_mutation(self):
        for suffix, method in [("", "GET"), ("/operator", "GET"), ("/runs", "GET"), ("/runs/run-a", "GET"), ("/retry", "POST"), ("/reconcile", "POST")]:
            with self.subTest(suffix=suffix, method=method):
                kwargs = {"json": {"actor": "operator", "reason": "scope test"}} if method == "POST" else {}
                response = self.client.request(method, f"/api/work-items/item-a{suffix}", params={"project_id": "project-b"}, **kwargs)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json(), {"detail": "Work item not found"})
                self.service.get.assert_not_called()
                self.operator.detail.assert_not_called()
                self.operator.retry.assert_not_called()
                self.operator.reconcile.assert_not_called()

    def test_matching_project_and_existing_unscoped_contract_work(self):
        for params in [{"project_id": "project-a"}, {}]:
            with self.subTest(params=params):
                response = self.client.get("/api/work-items/item-a", params=params)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["project_id"], "project-a")
                self.service.get.assert_awaited_with("item-a")

    def test_handoff_inspection_is_explicit_scoped_read_without_detail_or_mutation(self):
        params = {'project_id': 'project-a', 'expected_requested_at': 1,
                  'expected_recipient': 'quinn'}
        route = '/api/work-items/item-a/operator/handoff-eligibility'
        response = self.client.get(route, params=params)
        self.assertEqual(response.status_code, 200)
        self.operator.handoff_eligibility.assert_called_once_with('item-a',
            expected_requested_at=1, expected_recipient='quinn',
            organization_id='org-a', workspace_id='workspace-a', project_id='project-a')
        self.operator.detail.assert_not_called()
        self.operator.retry.assert_not_called()
        self.operator.reconcile.assert_not_called()
        self.operator.handoff_eligibility.reset_mock()
        params['project_id'] = 'project-b'
        self.assertEqual(self.client.get(route, params=params).status_code, 404)
        params['project_id'] = 'project-a'
        self.state.workspace_id = 'foreign'
        self.assertEqual(self.client.get(route, params=params).status_code, 404)
        self.operator.handoff_eligibility.assert_not_called()

    def test_matching_project_does_not_bypass_tenant_scope(self):
        self.state.organization_id = "org-other"
        response = self.client.get("/api/work-items/item-a", params={"project_id": "project-a"})
        self.assertEqual(response.status_code, 404)
        self.service.get.assert_not_called()

    def test_uninitialized_inspection_refuses_before_lazy_work_lookup(self):
        self.operator.require_handoff_inspection_ready.side_effect = HTTPException(
            status_code=503, detail={'code': 'owner_routing_inspection_not_initialized'})
        self.service.state_machine._work_item_state = Mock(side_effect=AssertionError('lookup prohibited'))
        response = self.client.get('/api/work-items/item-a/operator/handoff-eligibility',
            params={'expected_requested_at': 1, 'expected_recipient': 'quinn'})
        self.assertEqual(response.status_code, 503)
        self.service.state_machine._work_item_state.assert_not_called()
        self.operator.handoff_eligibility.assert_not_called()

    def test_reconcile_uses_authenticated_actor_not_caller_attribution(self):
        response = self.client.post(
            "/api/work-items/item-a/reconcile",
            json={"actor": "spoofed-operator", "reason": "repair attribution"},
        )

        self.assertEqual(response.status_code, 200)
        self.operator.reconcile.assert_awaited_once_with(
            "item-a",
            actor="canonical-operator",
            reason="repair attribution",
        )


class HandoffInspectionOffloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_readiness_does_not_block_the_request_loop(self):
        fixture = WorkItemProjectScopeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        started, release = threading.Event(), threading.Event()
        observed_release = []
        def blocked_read():
            started.set()
            observed_release.append(release.wait(timeout=1))
        fixture.operator.require_handoff_inspection_ready.side_effect = blocked_read
        with patch('codex_web.api.work_items.WorkItemOperatorService', return_value=fixture.operator):
            router = build_work_items_router(fixture.service)
        endpoint = next(route.endpoint for route in router.routes
            if getattr(route, 'path', '') == '/api/work-items/{ref:path}/operator/handoff-eligibility')
        request = SimpleNamespace(state=SimpleNamespace(tenant_scope=SimpleNamespace(
            organization_id='org-a', workspace_id='workspace-a')), query_params={'project_id':'project-a'})
        task = asyncio.create_task(endpoint('item-a', request, expected_requested_at=1,
            expected_recipient='quinn', project_id='project-a'))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            await asyncio.sleep(.01)
            self.assertFalse(task.done())
        finally:
            release.set()
            result = await task
        self.assertEqual(observed_release, [True])
        self.assertTrue(result['inspection_only'])
        fixture.operator.detail.assert_not_called()


class NestedReconcileAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        registry = DefinitionRegistryService(DefinitionRegistryStore(
            SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")))
        authority = install_authority_roles(registry)
        active = registry.resolve(definition_id=AUTHORITY_ROLE_CATALOG_ID,
                                  kind=AUTHORITY_ROLE_CATALOG_KIND)
        catalog = AuthorityRoleCatalogDefinition(
            roles=(AuthorityRoleDefinition(id="operator", name="Operator", description="Tenant-bound operational authority fixture",
                grants=(AuthorityGrant(id="operate", capability="work-item.operate",
                                       level=AuthorityLevel.EXECUTE),)),),
            bindings=(AuthorityRoleBinding(id="operator-binding", role_id="operator",
                subject_kind="identity", subject_id="operator-a",
                organization_id="org-a", workspace_id="workspace-a"),))
        draft = registry.create_draft(DefinitionDraftCreate(
            definition_id=AUTHORITY_ROLE_CATALOG_ID, kind=AUTHORITY_ROLE_CATALOG_KIND,
            definition_schema_version=AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
            payload=catalog.model_dump(mode="json"), actor="fixture", reason="test fixture"))
        registry.approve_publication(draft.record_id, actor="fixture-reviewer",
                                     reference="TEST-APPROVAL", reason="test fixture")
        registry.publish(draft.record_id, DefinitionPublishRequest(
            actor="fixture", reason="test fixture", expected_active_revision=active.revision))
        self.actor = AuthenticationActor(identity_id="operator-a",
            principal_kind=PrincipalKind.HUMAN, organization_id="org-a",
            workspace_id="workspace-a", roles=(MembershipRole.MEMBER,), assurance=AuthenticationAssurance.MFA)
        self.state = SimpleNamespace(organization_id="org-a", workspace_id="workspace-a",
                                     project_id="project-a")
        service = SimpleNamespace(state_machine=SimpleNamespace(
            _work_item_state=lambda ref: self.state), work_items=SimpleNamespace())
        self.operator = SimpleNamespace(reconcile=AsyncMock(return_value={"ok": True}))
        app = FastAPI()
        parent = APIRouter()
        with patch("codex_web.api.work_items.WorkItemOperatorService", return_value=self.operator):
            parent.include_router(build_work_items_router(service))
        app.include_router(parent)
        auth = install_api_authorization(app, authority)
        self.auth = auth

        @app.middleware("http")
        async def authenticated_boundary(request, call_next):
            request.state.identity_actor = self.actor
            request.state.tenant_scope = self.actor.tenant
            try:
                auth.authorize_request(request, self.actor)
            except ApiAuthorizationError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)
            return await call_next(request)

        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_member_cannot_reconcile_and_operator_actor_cannot_be_spoofed(self):
        self.actor = self.actor.model_copy(update={"identity_id": "member-a"})
        response = self.client.post("/api/work-items/item-a/reconcile",
                                    json={"actor": "operator-a"})
        self.assertEqual(response.status_code, 403)
        self.operator.reconcile.assert_not_awaited()
        self.actor = self.actor.model_copy(update={"identity_id": "operator-a"})
        response = self.client.post("/api/work-items/item-a/reconcile",
            params={"project_id": "project-a"}, json={"actor": "spoofed", "reason": "verified source"})
        self.assertEqual(response.status_code, 200)
        self.operator.reconcile.assert_awaited_once_with(
            "item-a", actor="operator-a", reason="verified source")

    def test_handoff_inspection_requires_existing_authenticated_read_permission(self):
        with self.assertRaises(ApiAuthorizationError):
            self.auth.authorize_operation('GET',
                '/api/work-items/{ref:path}/operator/handoff-eligibility', None)

    def test_operator_binding_and_object_tenant_project_fences(self):
        self.actor = self.actor.model_copy(update={"workspace_id": "workspace-other"})
        response = self.client.post("/api/work-items/item-a/reconcile", json={})
        self.assertEqual(response.status_code, 403)
        self.actor = self.actor.model_copy(update={"workspace_id": "workspace-a"})
        response = self.client.post("/api/work-items/item-a/reconcile",
                                    params={"project_id": "project-other"}, json={})
        self.assertEqual(response.status_code, 404)
        self.state.organization_id = "org-other"
        response = self.client.post("/api/work-items/item-a/reconcile", json={})
        self.assertEqual(response.status_code, 404)
        self.operator.reconcile.assert_not_awaited()
