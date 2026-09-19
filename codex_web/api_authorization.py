from __future__ import annotations

from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware import Middleware
from starlette.routing import Match

from codex_web.authority import (
    AuthorityDecisionOutcome,
    AuthorityEvaluationRequest,
    AuthorityLevel,
)
from codex_web.identity import (
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.authority_roles import AuthorityRoleService
from codex_web.services.identity import (
    AuthorizationError,
    IdentityService,
)


class APIAuthorizationKind(StrEnum):
    PUBLIC = "public"
    AUTHENTICATED = "authenticated"
    ADMIN = "admin"
    OPERATIONAL = "operational"


class APIServiceScopeMode(StrEnum):
    NONE = "none"
    DOMAIN = "domain"
    EXPLICIT = "explicit"


class APIAuthorizationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: APIAuthorizationKind
    human_roles: tuple[MembershipRole, ...] = ()
    required_assurance: AuthenticationAssurance | None = None
    service_scope_mode: APIServiceScopeMode = APIServiceScopeMode.NONE
    service_scopes: tuple[str, ...] = ()
    capability: str | None = None
    authority_level: AuthorityLevel | None = None
    domain_checks_remain_authoritative: bool = True
    description: str = Field(min_length=1)

    def openapi_extension(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "human_roles": [item.value for item in self.human_roles],
            "required_assurance": (
                self.required_assurance.value
                if self.required_assurance is not None
                else None
            ),
            "service_scope_mode": self.service_scope_mode.value,
            "service_scopes": list(self.service_scopes),
            "capability": self.capability,
            "authority_level": (
                self.authority_level.value
                if self.authority_level is not None
                else None
            ),
            "domain_checks_remain_authoritative": (
                self.domain_checks_remain_authoritative
            ),
            "description": self.description,
        }


PUBLIC_API_PATHS = frozenset(
    {
        "/api/livez",
        "/api/auth-verifier",
    }
)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

SELF_SERVICE_MUTATIONS = frozenset(
    {
        ("POST", "/api/identity/sessions/revoke-others"),
        ("DELETE", "/api/identity/sessions/{session_id}"),
    }
)

ADMIN_MUTATION_PREFIXES = (
    "/api/identity",
    "/api/secrets",
    "/api/crypto-keys",
    "/api/extensions",
    "/api/configuration",
    "/api/definitions",
    "/api/resources",
    "/api/entitlements",
    "/api/execution-workers",
    "/api/execution-workspaces",
    "/api/model-gateway",
    "/api/agent-providers",
    "/api/agent-runtimes",
    "/api/provider-capacity",
    "/api/releases",
    "/api/upgrades",
    "/api/recovery",
    "/api/incidents",
    "/api/business-context",
    "/api/business-data-sources",
    "/api/business-kpis",
    "/api/company-operations",
    "/api/data-governance",
)

OPERATIONAL_MUTATION_PREFIXES: tuple[tuple[str, str, AuthorityLevel], ...] = (
    ("/api/work-items", "work_items.operator", AuthorityLevel.EXECUTE),
)


def classify_api_policy(
    path: str,
    method: str,
) -> APIAuthorizationPolicy | None:
    method = method.upper()
    if not path.startswith("/api"):
        return None
    if path in PUBLIC_API_PATHS:
        return APIAuthorizationPolicy(
            kind=APIAuthorizationKind.PUBLIC,
            domain_checks_remain_authoritative=False,
            description="Public health/authentication-verifier endpoint.",
        )
    if method in SAFE_METHODS or (method, path) in SELF_SERVICE_MUTATIONS:
        return APIAuthorizationPolicy(
            kind=APIAuthorizationKind.AUTHENTICATED,
            description=(
                "Authenticated tenant member/service read; tenant isolation and "
                "domain-level visibility checks remain authoritative."
            ),
        )
    for prefix, capability, level in OPERATIONAL_MUTATION_PREFIXES:
        if path.startswith(prefix):
            return APIAuthorizationPolicy(
                kind=APIAuthorizationKind.OPERATIONAL,
                service_scope_mode=APIServiceScopeMode.DOMAIN,
                capability=capability,
                authority_level=level,
                description=(
                    "Operational mutation requires canonical Role authority in "
                    "addition to endpoint/domain authorization."
                ),
            )
    if path.startswith(ADMIN_MUTATION_PREFIXES):
        return APIAuthorizationPolicy(
            kind=APIAuthorizationKind.ADMIN,
            human_roles=(MembershipRole.OWNER, MembershipRole.ADMIN),
            required_assurance=AuthenticationAssurance.MFA,
            service_scope_mode=APIServiceScopeMode.DOMAIN,
            description=(
                "Human callers require Owner/Admin with MFA; service callers "
                "must satisfy the endpoint's exact service-scope policy."
            ),
        )
    return APIAuthorizationPolicy(
        kind=APIAuthorizationKind.AUTHENTICATED,
        description=(
            "Authenticated tenant member/service operation. Existing endpoint "
            "Role, service-scope, ApprovalRequest, authority and policy checks "
            "remain authoritative and may be stricter."
        ),
    )


def _route_methods(route: APIRoute) -> tuple[str, ...]:
    return tuple(sorted(method for method in route.methods or () if method != "OPTIONS"))


def _policy_key(method: str, path: str) -> str:
    return f"{method.upper()} {path}"


def _policy_for_scope(
    app: FastAPI,
    scope: dict[str, Any],
) -> tuple[APIRoute | None, APIAuthorizationPolicy | None, dict[str, Any]]:
    registry: dict[str, APIAuthorizationPolicy] = getattr(
        app.state,
        "api_authorization_policies",
        {},
    )
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        match, child_scope = route.matches(scope)
        if match != Match.FULL:
            continue
        method = str(scope.get("method") or "GET").upper()
        policy = registry.get(_policy_key(method, route.path))
        if policy is None and method == "HEAD":
            policy = registry.get(_policy_key("GET", route.path))
        return route, policy, child_scope
    return None, None, {}


def _unauthorized(detail: str, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


def _enforce_admin(policy: APIAuthorizationPolicy, request: Request) -> JSONResponse | None:
    actor = getattr(request.state, "identity_actor", None)
    if actor is None:
        return _unauthorized("authentication required", 401)
    if actor.principal_kind == PrincipalKind.SERVICE:
        # Exact service scopes remain endpoint/domain-owned. The HTTP boundary
        # deliberately does not guess or weaken those existing scope checks.
        if policy.service_scope_mode == APIServiceScopeMode.EXPLICIT:
            if not set(policy.service_scopes).issubset(set(actor.service_scopes)):
                return _unauthorized("required API service scope is missing", 403)
        return None
    if policy.human_roles and not actor.has_role(*policy.human_roles):
        return _unauthorized("administrator role required", 403)
    if policy.required_assurance is not None:
        try:
            IdentityService.require_assurance(actor, policy.required_assurance)
        except AuthorizationError as exc:
            return _unauthorized(str(exc), 403)
    return None


def _enforce_operational(
    policy: APIAuthorizationPolicy,
    request: Request,
    authority: AuthorityRoleService,
) -> JSONResponse | None:
    actor = getattr(request.state, "identity_actor", None)
    if actor is None:
        return _unauthorized("authentication required", 401)
    if policy.capability is None or policy.authority_level is None:
        return _unauthorized("API operational policy is incomplete", 403)
    decision = authority.evaluate(
        AuthorityEvaluationRequest(
            capability=policy.capability,
            level=policy.authority_level,
        ),
        actor=actor,
    )
    if decision.outcome != AuthorityDecisionOutcome.ALLOW:
        reason = "; ".join(decision.reasons) or "canonical Role authority denied"
        return _unauthorized(reason, 403)
    return None


def install_api_authorization(
    app: FastAPI,
    *,
    authority: AuthorityRoleService,
) -> None:
    if getattr(app.state, "api_authorization_installed", False):
        return

    registry: dict[str, APIAuthorizationPolicy] = {}
    undeclared: list[str] = []

    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api"):
            continue
        for method in _route_methods(route):
            policy = classify_api_policy(route.path, method)
            if policy is None:
                undeclared.append(_policy_key(method, route.path))
                continue
            registry[_policy_key(method, route.path)] = policy

        representative = next(
            (
                registry[_policy_key(method, route.path)]
                for method in _route_methods(route)
                if _policy_key(method, route.path) in registry
            ),
            None,
        )
        if representative is None:
            continue
        extra = dict(route.openapi_extra or {})
        extra["x-codex-authorization"] = representative.openapi_extension()
        extra["security"] = (
            []
            if representative.kind == APIAuthorizationKind.PUBLIC
            else [{"bearerAuth": []}, {"cookieSession": []}]
        )
        route.openapi_extra = extra
        if representative.kind != APIAuthorizationKind.PUBLIC:
            route.responses = {
                **dict(route.responses or {}),
                401: {"description": "Authentication required"},
                403: {"description": "Authorization denied"},
            }

    if undeclared:
        raise RuntimeError(
            "undeclared API authorization policy: " + ", ".join(sorted(undeclared))
        )

    app.state.api_authorization_policies = registry
    app.state.api_authorization_installed = True
    app.state.api_authorization_undeclared = tuple(undeclared)

    previous_openapi = app.openapi
    app.openapi_schema = None

    def openapi_with_authorization() -> dict[str, Any]:
        schema = previous_openapi()
        components = schema.setdefault("components", {})
        schemes = components.setdefault("securitySchemes", {})
        schemes.setdefault(
            "bearerAuth",
            {
                "type": "http",
                "scheme": "bearer",
                "description": (
                    "Session bearer token or tenant-scoped service token. "
                    "Exact service scopes are documented per operation."
                ),
            },
        )
        schemes.setdefault(
            "cookieSession",
            {
                "type": "apiKey",
                "in": "cookie",
                "name": "codex_web_session",
                "description": (
                    "Browser session cookie. Mutating cookie requests also "
                    "remain subject to canonical CSRF enforcement."
                ),
            },
        )
        return schema

    app.openapi = openapi_with_authorization

    class _APIAuthorizationMiddleware:
        def __init__(
            self,
            inner,
            *,
            fastapi_app: FastAPI,
            authority_service: AuthorityRoleService,
        ) -> None:
            self.inner = inner
            self.fastapi_app = fastapi_app
            self.authority_service = authority_service

        async def __call__(self, scope, receive, send) -> None:
            if scope.get("type") != "http":
                await self.inner(scope, receive, send)
                return
            request = Request(scope, receive=receive)
            path = request.url.path
            if not path.startswith("/api"):
                await self.inner(scope, receive, send)
                return
            route, policy, _child_scope = _policy_for_scope(
                self.fastapi_app,
                scope,
            )
            if route is None:
                # Unknown routes retain normal 404 behavior. Fail-closed applies
                # to declared application operations, not nonexistent paths.
                await self.inner(scope, receive, send)
                return
            if policy is None:
                response = _unauthorized(
                    "API operation has no declared authorization policy",
                    403,
                )
                await response(scope, receive, send)
                return
            if policy.kind == APIAuthorizationKind.PUBLIC:
                await self.inner(scope, receive, send)
                return

            actor = getattr(request.state, "identity_actor", None)
            if actor is None:
                response = _unauthorized("authentication required", 401)
                await response(scope, receive, send)
                return

            denied: JSONResponse | None = None
            if policy.kind == APIAuthorizationKind.ADMIN:
                denied = _enforce_admin(policy, request)
            elif policy.kind == APIAuthorizationKind.OPERATIONAL:
                denied = _enforce_operational(
                    policy,
                    request,
                    self.authority_service,
                )
            if denied is not None:
                await denied(scope, receive, send)
                return
            await self.inner(scope, receive, send)

    # Identity/CSRF middleware is installed earlier. Appending this middleware
    # makes it inner to those existing boundaries when Starlette builds the
    # stack, so request.state.identity_actor is canonical and already tenant-
    # validated before API policy evaluation.
    app.user_middleware.append(
        Middleware(
            _APIAuthorizationMiddleware,
            fastapi_app=app,
            authority_service=authority,
        )
    )
    app.middleware_stack = None
