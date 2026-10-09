from __future__ import annotations

import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from fastapi.testclient import TestClient
from codex_web.api.scheduler import build_scheduler_router
from codex_web.api.automations import build_automations_router

from codex_web.api.authorization import (
    ApiAccessMode,
    ApiAuthorizationError,
    ApiAuthorizationService,
    install_api_authorization,
    policy_for_operation,
    _match_api_route,
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

        @app.post("/api/configuration/drafts")
        async def create_configuration():
            return {}

        service = ApiAuthorizationService(FakeAuthority())
        with self.assertRaisesRegex(ApiAuthorizationError, "mfa"):
            service.authorize_request(
                request_for(app, "POST", "/api/configuration/drafts"),
                actor(
                    roles=(MembershipRole.ADMIN,),
                    assurance=AuthenticationAssurance.PRIMARY,
                ),
            )

    def test_retained_execution_retry_requires_admin_mfa(self):
        policy = policy_for_operation(
            "POST",
            "/api/threads/{thread_id}/preflight-attempts/{attempt_id}/retry",
        )
        self.assertEqual(policy.access, ApiAccessMode.ADMIN)
        self.assertEqual(policy.capability, "execution.retry")
        self.assertEqual(
            policy.required_assurance,
            AuthenticationAssurance.MFA,
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

    def test_nested_prefixes_methods_and_route_order_match_dispatch(self):
        app = FastAPI()
        child = APIRouter()

        @child.post("/{ref:path}/reconcile")
        async def reconcile(ref: str):
            return {}

        parent = APIRouter()
        parent.include_router(child, prefix="/work-items")
        app.include_router(parent, prefix="/api")
        service = ApiAuthorizationService(FakeAuthority())
        request = request_for(app, "POST", "/api/work-items/docs/item/reconcile")
        self.assertEqual(_match_api_route(app, request).path,
                         "/api/work-items/{ref:path}/reconcile")
        self.assertEqual(service.authorize_request(request, actor()).capability,
                         "work-item.operate")
        with self.assertRaisesRegex(ApiAuthorizationError, "authentication required"):
            service.authorize_request(request, None)
        rooted = request_for(app, "POST", "/proxy/api/work-items/item/reconcile")
        rooted.scope["root_path"] = "/proxy"
        self.assertEqual(service.authorize_request(rooted, actor()).capability,
                         "work-item.operate")
        self.assertIsNone(service.authorize_request(
            request_for(app, "GET", request.url.path), actor()))
        self.assertIsNone(service.authorize_request(
            request_for(app, "POST", "/work-items/item/reconcile"), actor()))

        # An earlier non-API route owns the request, as it does in ASGI dispatch.
        app.router.routes.insert(0, Route(request.url.path,
            endpoint=lambda request: PlainTextResponse("asset"), methods=["POST"]))
        self.assertIsNone(service.authorize_request(request, None))

    def test_flattened_fastapi_compatibility_and_full_match_after_wrong_method(self):
        app = FastAPI()

        @app.get("/api/work-items/{ref:path}/reconcile")
        async def read(ref: str):
            return {}

        @app.post("/api/work-items/{ref:path}/reconcile")
        async def reconcile(ref: str):
            return {}

        with patch("codex_web.api.authorization.routing.iter_route_contexts", None,
                   create=True):
            route = _match_api_route(app, request_for(
                app, "POST", "/api/work-items/item/reconcile"))
        self.assertIs(route.endpoint, reconcile)

    def test_nested_unknown_policy_validation_and_openapi_metadata(self):
        app = FastAPI()
        child = APIRouter()

        @child.get("/api/new-unclassified-domain")
        async def unknown():
            return {}

        app.include_router(child)
        with self.assertRaisesRegex(RuntimeError, "missing explicit authorization policy"):
            ApiAuthorizationService(FakeAuthority()).validate_app(app)
        with self.assertRaisesRegex(ApiAuthorizationError, "policy missing"):
            ApiAuthorizationService(FakeAuthority()).authorize_request(
                request_for(app, "GET", "/api/new-unclassified-domain"), actor())

        app = FastAPI()
        child = APIRouter()

        @child.post("/api/work-items/{ref:path}/reconcile")
        async def reconcile(ref: str):
            return {}

        @child.get("/api/livez")
        async def livez():
            return {}

        app.include_router(child)
        install_api_authorization(app, FakeAuthority())
        schema = app.openapi()
        operation = schema["paths"]["/api/work-items/{ref}/reconcile"]["post"]
        self.assertEqual(operation["x-codex-authorization"]["capability"],
                         "work-item.operate")
        self.assertIn("403", operation["responses"])
        self.assertEqual(schema["paths"]["/api/livez"]["get"]["security"], [])
        self.assertEqual(ApiAuthorizationService(FakeAuthority()).authorize_request(
            request_for(app, "GET", "/api/livez"), None).access, ApiAccessMode.PUBLIC)
        self.assertIsNone(ApiAuthorizationService(FakeAuthority()).authorize_request(
            request_for(app, "GET", "/static/app.js"), None))

    def test_existing_domain_classifications_preserve_owner_and_trigger_access(self):
        for path in ("/api/agent-profiles", "/api/agent-teams", "/api/evidence",
                     "/api/automations/{automation_id}/runs/manual"):
            with self.subTest(path=path):
                self.assertEqual(policy_for_operation("POST", path).access,
                                 ApiAccessMode.AUTHENTICATED)
        for path in ("/api/automations/drafts",
                     "/api/automations/drafts/{record_id}/publish",
                     "/api/automations/{automation_id}/schedule/reconcile",
                     "/api/schedules", "/api/crypto/keys"):
            with self.subTest(path=path):
                policy = policy_for_operation("POST", path)
                self.assertEqual(policy.access, ApiAccessMode.ADMIN)
                self.assertEqual(policy.required_assurance, AuthenticationAssurance.MFA)
        for path in ("/api/readyz", "/api/home", "/api/version"):
            self.assertEqual(policy_for_operation("GET", path).access,
                             ApiAccessMode.AUTHENTICATED)

    def _client_for_actor(self, app, authenticated_actor):
        authorization = install_api_authorization(app, FakeAuthority())

        @app.middleware("http")
        async def authenticated_boundary(request, call_next):
            request.state.identity_actor = authenticated_actor
            try:
                authorization.authorize_request(request, authenticated_actor)
            except ApiAuthorizationError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)
            return await call_next(request)

        client = TestClient(app)
        self.addCleanup(client.close)
        return client

    def test_scheduler_service_reader_scope_remains_required(self):
        for scopes, expected in (((), 403), (("scheduler:read",), 200)):
            with self.subTest(scopes=scopes):
                service = SimpleNamespace(list=Mock(return_value=[]))
                app = FastAPI()
                app.include_router(build_scheduler_router(service))
                service_actor = actor().model_copy(update={
                    "principal_kind": PrincipalKind.SERVICE, "service_scopes": scopes})
                response = self._client_for_actor(app, service_actor).get("/api/schedules")
                self.assertEqual(response.status_code, expected)
                if expected == 200:
                    service.list.assert_called_once_with()
                else:
                    service.list.assert_not_called()

    def test_manual_automation_trigger_retains_authenticated_member_path(self):
        triggers = SimpleNamespace(manual=Mock(return_value=SimpleNamespace(
            run=SimpleNamespace(model_dump=lambda **kwargs: {"id": "run-a"}),
            inserted=True, launch_allowed=False)))
        app = FastAPI()
        app.include_router(build_automations_router(
            SimpleNamespace(), SimpleNamespace(), triggers, SimpleNamespace()))
        member = actor()
        response = self._client_for_actor(app, member).post(
            "/api/automations/owner-automation/runs/manual",
            json={"idempotency_key": "manual-1", "project_id": "project-a"})
        self.assertEqual(response.status_code, 200)
        triggers.manual.assert_called_once_with("owner-automation",
            organization_id=member.organization_id, workspace_id=member.workspace_id,
            project_id="project-a", actor_id=member.identity_id,
            idempotency_key="manual-1")
        self.assertFalse(response.json()["launchAllowed"])

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

        @app.post("/api/configuration/drafts")
        async def create_configuration():
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
        self.assertNotIn(
            "required_assurance",
            write["x-codex-authorization"],
        )

        sensitive = schema["paths"]["/api/configuration/drafts"]["post"]
        self.assertEqual(
            sensitive["x-codex-authorization"]["required_assurance"],
            "mfa",
        )


if __name__ == "__main__":
    unittest.main()
