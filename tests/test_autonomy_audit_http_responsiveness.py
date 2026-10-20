from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import Mock

import httpx
from fastapi import FastAPI

from codex_web.api.autonomy_audit import build_autonomy_audit_router
from codex_web.identity import (
    AuthenticationActor, AuthenticationAssurance, MembershipRole, PrincipalKind,
)
from codex_web.services.autonomy_audit import AutonomyAuditError, AutonomyAuditService


class AutonomyAuditHttpResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = Mock(spec=AutonomyAuditService)
        self.actor = AuthenticationActor(
            identity_id="admin", principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a", workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,), assurance=AuthenticationAssurance.MFA,
        )
        self.app = FastAPI()

        @self.app.middleware("http")
        async def identity(request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)

        self.app.include_router(build_autonomy_audit_router(self.service))

        @self.app.get("/health")
        async def health():
            return {"audit_still_blocked": not self.finished.is_set()}

    async def assert_responsive(self, method, path, operation, result):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def blocked(*args, **kwargs):
            entered.set()
            try:
                release.wait(2)
                return result
            finally:
                finished.set()

        getattr(self.service, operation).side_effect = blocked

        self.finished = finished

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
        ) as client:
            task = asyncio.create_task(client.request(method, path))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                health = await client.get("/health")
                self.assertTrue(health.json()["audit_still_blocked"])
            finally:
                release.set()
                response = await task
            self.assertEqual(response.status_code, 200)
        return response

    async def test_blocked_record_read_does_not_block_other_requests(self):
        response = await self.assert_responsive("GET", "/api/autonomy/audit?limit=7", "list_records", ())
        self.assertEqual(response.json(), {"items": [], "count": 0})
        self.service.list_records.assert_called_once_with(
            organization_id="org-a", workspace_id="ws-a", limit=7,
        )

    async def test_full_history_inspection_routes_remain_responsive(self):
        for path, operation, result in (
            ("/metrics", "metrics", Mock(model_dump=Mock(return_value={"count": 0}))),
            ("/checkpoints", "list_checkpoints", ()),
            ("/signals", "list_signals", ()),
        ):
            with self.subTest(path=path):
                await self.assert_responsive("GET", "/api/autonomy/audit" + path, operation, result)

    async def test_record_lookup_does_not_block_other_requests(self):
        record = object()
        self.service.public_record.return_value = {"id": "record-a"}
        response = await self.assert_responsive("GET", "/api/autonomy/audit/record-a", "get", record)
        self.assertEqual(response.json(), {"item": {"id": "record-a"}})
        self.service.get.assert_called_once_with("record-a", organization_id="org-a", workspace_id="ws-a")

    async def test_authorization_and_not_found_are_preserved(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
        ) as client:
            self.actor = self.actor.model_copy(update={"roles": (MembershipRole.MEMBER,)})
            self.assertEqual((await client.get("/api/autonomy/audit")).status_code, 403)
            self.service.list_records.assert_not_called()
            self.actor = self.actor.model_copy(update={"roles": (MembershipRole.ADMIN,), "assurance": AuthenticationAssurance.PRIMARY})
            self.assertEqual((await client.post("/api/autonomy/audit/verify")).status_code, 403)
            self.service.verify.assert_not_called()
            self.service.get.side_effect = AutonomyAuditError("missing")
            self.assertEqual((await client.get("/api/autonomy/audit/missing")).status_code, 404)
            self.service.get.assert_called_once_with("missing", organization_id="org-a", workspace_id="ws-a")

    async def test_service_scope_is_enforced_before_storage(self):
        self.actor = AuthenticationActor(
            identity_id="service", principal_kind=PrincipalKind.SERVICE,
            organization_id="org-b", workspace_id="ws-b",
            assurance=AuthenticationAssurance.SERVICE_TOKEN, service_scopes=(),
        )
        self.service.list_records.return_value = ()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
        ) as client:
            self.assertEqual((await client.get("/api/autonomy/audit")).status_code, 403)
            self.service.list_records.assert_not_called()
            self.actor = self.actor.model_copy(update={"service_scopes": ("autonomy:read",)})
            response = await client.get("/api/autonomy/audit")
            self.assertEqual(response.json(), {"items": [], "count": 0})
            self.service.list_records.assert_called_once_with(
                organization_id="org-b", workspace_id="ws-b", limit=200,
            )
            self.assertEqual((await client.post("/api/autonomy/audit/verify")).status_code, 403)
            self.service.verify.assert_not_called()
