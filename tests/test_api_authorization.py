from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from fastapi.routing import APIRoute

from codex_web.api_authorization import (
    APIAuthorizationKind,
    classify_api_policy,
    install_api_authorization,
)
from codex_web.authority import (
    AuthorityDecisionOutcome,
    AuthorityLevel,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)


def actor(
    *,
    roles=(MembershipRole.MEMBER,),
    assurance=AuthenticationAssurance.PRIMARY,
    principal_kind=PrincipalKind.HUMAN,
    service_scopes=(),
    identity_id="user-a",
):
    return AuthenticationActor(
        identity_id=identity_id,
        principal_kind=principal_kind,
        organization_id="org-a",
        workspace_id="ws-a",
        roles=roles,
        assurance=assurance,
        service_scopes=service_scopes,
    )


class FakeAuthority:
    def evaluate(self, request, *, actor, now=None):
        allowed = actor.identity_id in {"operator", "local-admin"}
        return SimpleNamespace(
            outcome=(
                AuthorityDecisionOutcome.ALLOW
                if allowed
                else AuthorityDecisionOutcome.DENY
            ),
            reasons=() if allowed else ("missing work_items.operator grant",),
        )


def build_test_app() -> tuple[FastAPI, TestClient]:
    app = FastAPI()

    @app.middleware("http")
    async def fake_identity(request: Request, call_next):
        mode = request.headers.get("x-test-actor")
        if mode == "member":
            request.state.identity_actor = actor()
        elif mode == "admin-primary":
            request.state.identity_actor = actor(
                roles=(MembershipRole.ADMIN,),
            )
        elif mode == "admin-mfa":
            request.state.identity_actor = actor(
                roles=(MembershipRole.ADMIN,),
                assurance=AuthenticationAssurance.MFA,
            )
        elif mode == "operator":
            request.state.identity_actor = actor(identity_id="operator")
        elif mode == "service":
            request.state.identity_actor = actor(
                identity_id="service-a",
                roles=(),
                principal_kind=PrincipalKind.SERVICE,
                assurance=AuthenticationAssurance.SERVICE_TOKEN,
                service_scopes=("secrets:admin",),
            )
        return await call_next(request)

    @app.get("/api/livez")
    async def livez():
        return {"ok": True}

    @app.get("/api/widgets")
    async def list_widgets():
        return {"items": []}

    @app.post("/api/secrets")
    async def create_secret(request: Request):
        current = request.state.identity_actor
        if current.principal_kind == PrincipalKind.SERVICE:
            if "secrets:admin" not in current.service_scopes:
                return {"domain_scope": "denied"}
        return {"ok": True}

    @app.post("/api/work-items/demo/progress")
    async def progress():
        return {"ok": True}

    install_api_authorization(app, authority=FakeAuthority())
    return app, TestClient(app)


class APIAuthorizationUnitTests(unittest.TestCase):
    def test_classifier_covers_public_read_admin_and_operational_classes(self) -> None:
        self.assertEqual(
            classify_api_policy("/api/livez", "GET").kind,
            APIAuthorizationKind.PUBLIC,
        )
        self.assertEqual(
            classify_api_policy("/api/metrics", "GET").kind,
            APIAuthorizationKind.AUTHENTICATED,
        )
        self.assertEqual(
            classify_api_policy(
                "/api/identity/sessions/revoke-others",
                "POST",
            ).kind,
            APIAuthorizationKind.AUTHENTICATED,
        )
        admin = classify_api_policy("/api/secrets", "POST")
        self.assertEqual(admin.kind, APIAuthorizationKind.ADMIN)
        self.assertEqual(
            admin.required_assurance,
            AuthenticationAssurance.MFA,
        )
        operational = classify_api_policy(
            "/api/work-items/demo/progress",
            "POST",
        )
        self.assertEqual(
            operational.kind,
            APIAuthorizationKind.OPERATIONAL,
        )
        self.assertEqual(
            operational.capability,
            "work_items.operator",
        )
        self.assertEqual(
            operational.authority_level,
            AuthorityLevel.EXECUTE,
        )

    def test_unknown_mutation_family_fails_closed_during_install(self) -> None:
        app = FastAPI()

        @app.post("/api/new-unclassified-domain")
        async def unclassified():
            return {"ok": True}

        with self.assertRaises(RuntimeError) as caught:
            install_api_authorization(app, authority=FakeAuthority())
        self.assertIn(
            "POST /api/new-unclassified-domain",
            str(caught.exception),
        )

    def test_runtime_enforces_auth_admin_stepup_and_operational_authority(self) -> None:
        app, client = build_test_app()
        try:
            self.assertEqual(client.get("/api/livez").status_code, 200)
            self.assertEqual(client.get("/api/widgets").status_code, 401)
            self.assertEqual(
                client.get(
                    "/api/widgets",
                    headers={"x-test-actor": "member"},
                ).status_code,
                200,
            )

            self.assertEqual(
                client.post(
                    "/api/secrets",
                    headers={"x-test-actor": "member"},
                ).status_code,
                403,
            )
            self.assertEqual(
                client.post(
                    "/api/secrets",
                    headers={"x-test-actor": "admin-primary"},
                ).status_code,
                403,
            )
            self.assertEqual(
                client.post(
                    "/api/secrets",
                    headers={"x-test-actor": "admin-mfa"},
                ).status_code,
                200,
            )
            self.assertEqual(
                client.post(
                    "/api/secrets",
                    headers={"x-test-actor": "service"},
                ).status_code,
                200,
            )

            denied = client.post(
                "/api/work-items/demo/progress",
                headers={"x-test-actor": "member"},
            )
            self.assertEqual(denied.status_code, 403)
            self.assertIn("work_items.operator", denied.json()["detail"])
            self.assertEqual(
                client.post(
                    "/api/work-items/demo/progress",
                    headers={"x-test-actor": "operator"},
                ).status_code,
                200,
            )
        finally:
            client.close()

    def test_openapi_uses_same_policy_and_protected_response_metadata(self) -> None:
        app, client = build_test_app()
        try:
            schema = app.openapi()
            public = schema["paths"]["/api/livez"]["get"]
            self.assertEqual(
                public["x-codex-authorization"]["kind"],
                "public",
            )
            self.assertEqual(public["security"], [])

            read = schema["paths"]["/api/widgets"]["get"]
            self.assertEqual(
                read["x-codex-authorization"]["kind"],
                "authenticated",
            )
            self.assertIn("401", read["responses"])
            self.assertIn("403", read["responses"])
            self.assertEqual(
                read["security"],
                [{"bearerAuth": []}, {"cookieSession": []}],
            )

            admin = schema["paths"]["/api/secrets"]["post"]
            self.assertEqual(
                admin["x-codex-authorization"]["kind"],
                "admin",
            )
            self.assertEqual(
                admin["x-codex-authorization"]["required_assurance"],
                "mfa",
            )

            operational = schema["paths"][
                "/api/work-items/demo/progress"
            ]["post"]
            self.assertEqual(
                operational["x-codex-authorization"]["capability"],
                "work_items.operator",
            )
            self.assertEqual(
                operational["x-codex-authorization"]["authority_level"],
                "execute",
            )

            self.assertIn(
                "**Authorization:**",
                operational["description"],
            )
            self.assertIn(
                "capability=`work_items.operator`",
                operational["description"],
            )

            schemes = schema["components"]["securitySchemes"]
            self.assertIn("bearerAuth", schemes)
            self.assertIn("cookieSession", schemes)
        finally:
            client.close()


class ComposedAPIContractTests(unittest.TestCase):
    def test_every_composed_api_operation_has_explicit_policy_and_openapi_metadata(self) -> None:
        from codex_web.application import app

        self.assertEqual(app.state.api_authorization_undeclared, ())
        registry = app.state.api_authorization_policies
        missing = []
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            if not route.path.startswith("/api"):
                continue
            for method in sorted(route.methods or ()):
                if method == "OPTIONS":
                    continue
                key = f"{method} {route.path}"
                if key not in registry and not (
                    method == "HEAD"
                    and f"GET {route.path}" in registry
                ):
                    missing.append(key)
            self.assertIn("x-codex-authorization", route.openapi_extra or {})
        self.assertEqual(missing, [])

        schema = app.openapi()
        missing_schema = []
        for path, operations in schema.get("paths", {}).items():
            if not path.startswith("/api"):
                continue
            for method, operation in operations.items():
                if method not in {
                    "get",
                    "post",
                    "put",
                    "patch",
                    "delete",
                    "head",
                    "options",
                }:
                    continue
                if "x-codex-authorization" not in operation:
                    missing_schema.append(f"{method.upper()} {path}")
        self.assertEqual(missing_schema, [])


if __name__ == "__main__":
    unittest.main()
