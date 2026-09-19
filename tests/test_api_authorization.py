from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import FastAPI
from starlette.requests import Request

from codex_web.api.authorization import (
    ApiAccessMode,
    ApiAuthorizationError,
    ApiAuthorizationService,
    install_api_authorization,
    policy_for_operation,
)
from codex_web.authority import AuthorityDecisionOutcome
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)


class FakeAuthority:
    def __init__(self, outcome=AuthorityDecisionOutcome.ALLOW):
        self.outcome = outcome
        self.requests = []

    def evaluate(self, request, *, actor):
        self.requests.append((request, actor))
        reasons = () if self.outcome == AuthorityDecisionOutcome.ALLOW else ("denied by test role",)
        return SimpleNamespace(outcome=self.outcome, reasons=reasons)


def actor(*, roles=(MembershipRole.MEMBER,), assurance=AuthenticationAssurance.MFA):
    return AuthenticationActor(
        identity_id="user-a",
        principal_kind=PrincipalKind.HUMAN,
        organization_id="org-a",
        workspace_id="ws-a",
        roles=roles,
        assurance=assurance,
    )


def request_for(app: FastAPI, method: str, path: str) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
            "app": app,
        }
    )


class ApiAuthorizationTests(unittest.TestCase):
    def test_policy_resolution_is_fail_closed_for_unknown_api_domain(self):
        self.assertIsNone(policy_for_operation("GET", "/api/new-unclassified-domain"))
        self.assertEqual(
            policy_for_operation("GET", "/api/projects").access,
            ApiAccessMode.AUTHENTICATED,
        )
        self.assertEqual(
            policy_for_operation("POST", "/api/projects").access,
            ApiAccessMode.ADMIN,
        )

    def test_app_validation_rejects_unclassified_api_route(self):
        app = FastAPI()

        @app.get("/api/new-unclassified-domain")
        async def unknown():
            return {}

        with self.assertRaisesRegex(RuntimeError, "missing explicit authorization policy"):
            ApiAuthorizationService(FakeAuthority()).validate_app(app)

    def test_admin_policy_rejects_member_and_accepts_admin_with_mfa(self):
        app = FastAPI()

        @app.post("/api/projects")
        async def create_project():
            return {}

        service = ApiAuthorizationService(FakeAuthority())
        with self.assertRaisesRegex(ApiAuthorizationError, "administrator role required"):
            service.authorize_request(
                request_for(app, "POST", "/api/projects"),
                actor(),
            )

        policy = service.authorize_request(
            request_for(app, "POST", "/api/projects"),
            actor(roles=(MembershipRole.ADMIN,)),
        )
        self.assertEqual(policy.access, ApiAccessMode.ADMIN)

    def test_admin_policy_requires_configured_assurance(self):
        app = FastAPI()

        @app.post("/api/projects")
        async def create_project():
            return {}

        service = ApiAuthorizationService(FakeAuthority())
        with self.assertRaisesRegex(ApiAuthorizationError, "mfa"):
            service.authorize_request(
                request_for(app, "POST", "/api/projects"),
                actor(
                    roles=(MembershipRole.ADMIN,),
                    assurance=AuthenticationAssurance.PRIMARY,
                ),
            )

    def test_work_item_retry_uses_operational_authority(self):
        app = FastAPI()

        @app.post("/api/work-items/{ref:path}/retry")
        async def retry(ref: str):
            return {"ref": ref}

        denied = FakeAuthority(AuthorityDecisionOutcome.DENY)
        service = ApiAuthorizationService(denied)
        with self.assertRaisesRegex(ApiAuthorizationError, "denied by test role"):
            service.authorize_request(
                request_for(app, "POST", "/api/work-items/project/task/retry"),
                actor(),
            )
        self.assertEqual(denied.requests[0][0].capability, "work-item.operate")
        self.assertEqual(denied.requests[0][0].level.value, "execute")

        allowed = FakeAuthority(AuthorityDecisionOutcome.ALLOW)
        policy = ApiAuthorizationService(allowed).authorize_request(
            request_for(app, "POST", "/api/work-items/project/task/retry"),
            actor(),
        )
        self.assertEqual(policy.access, ApiAccessMode.AUTHORITY)

    def test_openapi_exposes_security_and_capability_contract(self):
        app = FastAPI(title="test", version="1")

        @app.get("/api/livez")
        async def livez():
            return {}

        @app.get("/api/projects")
        async def projects():
            return []

        @app.post("/api/projects")
        async def create_project():
            return {}

        install_api_authorization(app, FakeAuthority())
        schema = app.openapi()

        public = schema["paths"]["/api/livez"]["get"]
        self.assertEqual(public["security"], [])
        self.assertEqual(
            public["x-codex-authorization"]["access"],
            "public",
        )

        read = schema["paths"]["/api/projects"]["get"]
        self.assertEqual(
            read["x-codex-authorization"]["capability"],
            "api.projects.read",
        )
        self.assertIn("401", read["responses"])
        self.assertIn("403", read["responses"])
        self.assertIn("BearerAuth", schema["components"]["securitySchemes"])
        self.assertIn("CodexSession", schema["components"]["securitySchemes"])

        write = schema["paths"]["/api/projects"]["post"]
        self.assertEqual(
            write["x-codex-authorization"]["access"],
            "admin",
        )
        self.assertEqual(
            write["x-codex-authorization"]["required_assurance"],
            "mfa",
        )


if __name__ == "__main__":
    unittest.main()
