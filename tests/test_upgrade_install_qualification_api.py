import unittest
from unittest.mock import Mock

import httpx
from fastapi import FastAPI

from codex_web.api.upgrades import build_upgrades_router
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, MembershipRole, PrincipalKind
from codex_web.services.upgrades import UpgradePreflightError


class LocalInstallQualificationAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.actor = AuthenticationActor(identity_id="operator", principal_kind=PrincipalKind.HUMAN,
                                         organization_id="org", workspace_id="workspace",
                                         roles=(MembershipRole.ADMIN,), assurance=AuthenticationAssurance.MFA)
        self.service = Mock()
        self.service.qualify_local_deployment.return_value = {"upgrade_plan_id": "upgrade-1", "rollback_available": False}
        self.app = FastAPI()
        @self.app.middleware("http")
        async def identity(request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)
        self.app.include_router(build_upgrades_router(self.service))
        self.payload = dict(service_instance_id="instance", source_revision="a" * 40,
                            target_revision="b" * 40, preflight_evidence_id="evidence-1")

    async def request(self, payload=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
            return await client.post("/api/upgrades/upgrade-1/qualify-local-deployment", json=payload or self.payload)

    async def test_authorized_operator_uses_the_existing_canonical_guard(self):
        response = await self.request()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["qualification"]["rollback_available"])
        self.service.qualify_local_deployment.assert_called_once_with("upgrade-1", actor=self.actor, **self.payload)

    async def test_weaker_human_assurance_or_unscoped_service_cannot_qualify(self):
        actors = [self.actor.model_copy(update={"assurance": AuthenticationAssurance.PRIMARY}),
                  self.actor.model_copy(update={"roles": (MembershipRole.MEMBER,)}),
                  self.actor.model_copy(update={"principal_kind": PrincipalKind.SERVICE, "service_scopes": ()})]
        for actor in actors:
            with self.subTest(actor=actor):
                self.actor = actor
                self.assertEqual((await self.request()).status_code, 403)
        self.service.qualify_local_deployment.assert_not_called()

    async def test_stale_or_incomplete_qualification_is_rejected(self):
        self.service.qualify_local_deployment.side_effect = UpgradePreflightError("stale preflight")
        response = await self.request()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "stale preflight")
        self.service.reset_mock()
        response = await self.request({**self.payload, "source_revision": "bad"})
        self.assertEqual(response.status_code, 422)
        self.service.qualify_local_deployment.assert_not_called()
