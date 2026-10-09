"""Requested Project fences supplement the existing tenant boundary on Work Items."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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

    def test_matching_project_does_not_bypass_tenant_scope(self):
        self.state.organization_id = "org-other"
        response = self.client.get("/api/work-items/item-a", params={"project_id": "project-a"})
        self.assertEqual(response.status_code, 404)
        self.service.get.assert_not_called()

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
