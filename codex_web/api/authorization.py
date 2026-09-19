from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from starlette.routing import Match

from codex_web.authority import (
    AuthorityDecisionOutcome,
    AuthorityEvaluationRequest,
    AuthorityLevel,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.authority_roles import AuthorityRoleService
from codex_web.services.identity import AuthorizationError, IdentityService


class ApiAccessMode(StrEnum):
    PUBLIC = "public"
    AUTHENTICATED = "authenticated"
    ADMIN = "admin"
    AUTHORITY = "authority"


@dataclass(frozen=True, slots=True)
class ApiAuthorizationPolicy:
    capability: str
    level: AuthorityLevel
    access: ApiAccessMode = ApiAccessMode.AUTHENTICATED
    required_assurance: AuthenticationAssurance | None = None
    service_scopes: tuple[str, ...] = ()
    description: str | None = None

    @property
    def public(self) -> bool:
        return self.access == ApiAccessMode.PUBLIC

    def openapi(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "access": self.access.value,
            "capability": self.capability,
            "level": self.level.value,
        }
        if self.required_assurance is not None:
            payload["required_assurance"] = self.required_assurance.value
        if self.service_scopes:
            payload["service_scopes"] = list(self.service_scopes)
        if self.description:
            payload["description"] = self.description
        return payload


@dataclass(frozen=True, slots=True)
class ApiDomainPolicy:
    read_access: ApiAccessMode = ApiAccessMode.AUTHENTICATED
    write_access: ApiAccessMode = ApiAccessMode.AUTHENTICATED
    write_assurance: AuthenticationAssurance | None = None
    write_service_scopes: tuple[str, ...] = ()


class ApiAuthorizationError(AuthorizationError):
    def __init__(self, message: str, *, status_code: int = 403) -> None:
        super().__init__(message)
        self.status_code = status_code


PUBLIC_OPERATIONS: dict[tuple[str, str], ApiAuthorizationPolicy] = {
    ("GET", "/api/livez"): ApiAuthorizationPolicy(
        capability="api.system.liveness",
        level=AuthorityLevel.READ,
        access=ApiAccessMode.PUBLIC,
        description="Unauthenticated process liveness probe.",
    ),
    ("GET", "/api/auth-verifier"): ApiAuthorizationPolicy(
        capability="api.identity.auth-verifier",
        level=AuthorityLevel.READ,
        access=ApiAccessMode.PUBLIC,
        description="Unauthenticated authentication-verifier endpoint.",
    ),
}


# These are the API domains currently exposed by the application. There is no
# generic catch-all: a new top-level /api/<domain> fails closed until its domain
# is classified here or the operation receives an exact override below.
API_DOMAINS: dict[str, ApiDomainPolicy] = {
    "account": ApiDomainPolicy(),
    "action-intents": ApiDomainPolicy(),
    "action-providers": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "agent-providers": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "agent-runtimes": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "agent-sessions": ApiDomainPolicy(),
    "approvals": ApiDomainPolicy(),
    "approval-requests": ApiDomainPolicy(),
    "artifacts": ApiDomainPolicy(),
    "attention": ApiDomainPolicy(),
    "authority": ApiDomainPolicy(
        read_access=ApiAccessMode.ADMIN,
        write_access=ApiAccessMode.ADMIN,
        write_assurance=AuthenticationAssurance.MFA,
    ),
    "autonomy": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "bots": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "business-context": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "business-data-sources": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "business-kpis": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "capacity": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "company-operations": ApiDomainPolicy(),
    "compatibility": ApiDomainPolicy(),
    "configuration": ApiDomainPolicy(
        write_access=ApiAccessMode.ADMIN,
        write_assurance=AuthenticationAssurance.MFA,
    ),
    "context": ApiDomainPolicy(),
    "crypto-keys": ApiDomainPolicy(
        read_access=ApiAccessMode.ADMIN,
        write_access=ApiAccessMode.ADMIN,
        write_assurance=AuthenticationAssurance.MFA,
    ),
    "data-governance": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "decisions": ApiDomainPolicy(),
    "diagnostics": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "definitions": ApiDomainPolicy(
        write_access=ApiAccessMode.ADMIN,
        write_assurance=AuthenticationAssurance.MFA,
    ),
    "entitlements": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "evaluations": ApiDomainPolicy(),
    "executive": ApiDomainPolicy(),
    "executive-management": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "extensions": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "execution-workspaces": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "execution-workers": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "goals": ApiDomainPolicy(),
    "healthz": ApiDomainPolicy(),
    "identity": ApiDomainPolicy(
        write_access=ApiAccessMode.ADMIN,
        write_assurance=AuthenticationAssurance.MFA,
    ),
    "incidents": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "input-plugins": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "integrations": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "livez": ApiDomainPolicy(),
    "memory": ApiDomainPolicy(),
    "metrics": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "model-gateway": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "models": ApiDomainPolicy(),
    "operations": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "orchestration": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "projects": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "provider-capacity": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "recovery": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "releases": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "resources": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "scheduler": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "secrets": ApiDomainPolicy(
        read_access=ApiAccessMode.ADMIN,
        write_access=ApiAccessMode.ADMIN,
        write_assurance=AuthenticationAssurance.MFA,
    ),
    "security": ApiDomainPolicy(
        read_access=ApiAccessMode.ADMIN,
        write_access=ApiAccessMode.ADMIN,
    ),
    "status": ApiDomainPolicy(),
    "task-sources": ApiDomainPolicy(),
    "thread-settings": ApiDomainPolicy(),
    "threads": ApiDomainPolicy(),
    "turns": ApiDomainPolicy(),
    "upgrades": ApiDomainPolicy(write_access=ApiAccessMode.ADMIN),
    "work-graph": ApiDomainPolicy(),
    "work-graphs": ApiDomainPolicy(),
    "work-items": ApiDomainPolicy(),
}


# Exact overrides are deliberately small and security-significant. Work-item
# retry/reconcile were previously only tenant-visible at the HTTP layer; they
# now require canonical operational authority while ordinary progress/handoff
# traffic remains compatible with authenticated-member workflows.
EXACT_POLICIES: dict[tuple[str, str], ApiAuthorizationPolicy] = {
    ("POST", "/api/work-items/{ref:path}/retry"): ApiAuthorizationPolicy(
        capability="work-item.operate",
        level=AuthorityLevel.EXECUTE,
        access=ApiAccessMode.AUTHORITY,
        description="Retry canonical work only with explicit operational authority.",
    ),
    ("POST", "/api/work-items/{ref:path}/reconcile"): ApiAuthorizationPolicy(
        capability="work-item.operate",
        level=AuthorityLevel.EXECUTE,
        access=ApiAccessMode.AUTHORITY,
        description="Reconcile canonical work only with explicit operational authority.",
    ),
}


def _domain_for(path: str) -> str | None:
    if not path.startswith("/api/"):
        return None
    remainder = path[len("/api/") :]
    return remainder.split("/", 1)[0] or None


def policy_for_operation(method: str, path: str) -> ApiAuthorizationPolicy | None:
    method = method.upper()
    exact = EXACT_POLICIES.get((method, path))
    if exact is not None:
        return exact
    public = PUBLIC_OPERATIONS.get((method, path))
    if public is not None:
        return public

    domain = _domain_for(path)
    domain_policy = API_DOMAINS.get(domain or "")
    if domain_policy is None:
        return None

    read = method in {"GET", "HEAD", "OPTIONS"}
    access = domain_policy.read_access if read else domain_policy.write_access
    assurance = None if read else domain_policy.write_assurance
    scopes = () if read else domain_policy.write_service_scopes
    action = "read" if read else "write"
    level = AuthorityLevel.READ if read else AuthorityLevel.EXECUTE
    return ApiAuthorizationPolicy(
        capability=f"api.{domain}.{action}",
        level=level,
        access=access,
        required_assurance=assurance,
        service_scopes=scopes,
    )


def _match_api_route(app: FastAPI, request: Request) -> APIRoute | None:
    scope = dict(request.scope)
    for route in app.router.routes:
        if not isinstance(route, APIRoute):
            continue
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return route
    return None


def _admin_actor(policy: ApiAuthorizationPolicy, actor: AuthenticationActor) -> None:
    if actor.principal_kind == PrincipalKind.SERVICE:
        # Service identities retain the domain-specific scope checks already
        # enforced by sensitive handlers. If the central policy declares
        # scopes, require one of them here as an additional fail-closed gate.
        if policy.service_scopes and not set(policy.service_scopes).intersection(
            actor.service_scopes
        ):
            raise ApiAuthorizationError(
                "service token lacks required API scope: "
                + " or ".join(policy.service_scopes)
            )
        return

    if not actor.has_role(MembershipRole.OWNER, MembershipRole.ADMIN):
        raise ApiAuthorizationError("administrator role required")
    if policy.required_assurance is not None:
        try:
            IdentityService.require_assurance(actor, policy.required_assurance)
        except AuthorizationError as exc:
            raise ApiAuthorizationError(str(exc)) from exc


class ApiAuthorizationService:
    def __init__(self, authority: AuthorityRoleService) -> None:
        self.authority = authority

    def authorize_request(
        self,
        request: Request,
        actor: AuthenticationActor | None,
    ) -> ApiAuthorizationPolicy | None:
        route = _match_api_route(request.app, request)
        if route is None or not route.path.startswith("/api/"):
            return None

        policy = policy_for_operation(request.method, route.path)
        if policy is None:
            raise ApiAuthorizationError(
                f"API authorization policy missing for {request.method} {route.path}"
            )
        request.state.api_authorization_policy = policy

        if policy.public:
            return policy
        if actor is None:
            raise ApiAuthorizationError("authentication required", status_code=401)
        if policy.access == ApiAccessMode.AUTHENTICATED:
            return policy
        if policy.access == ApiAccessMode.ADMIN:
            _admin_actor(policy, actor)
            return policy
        if policy.access == ApiAccessMode.AUTHORITY:
            decision = self.authority.evaluate(
                AuthorityEvaluationRequest(
                    capability=policy.capability,
                    level=policy.level,
                ),
                actor=actor,
            )
            request.state.api_authorization_decision = decision
            if decision.outcome != AuthorityDecisionOutcome.ALLOW:
                reason = "; ".join(decision.reasons) or "operational authority denied"
                raise ApiAuthorizationError(reason)
            return policy
        raise ApiAuthorizationError("unsupported API authorization policy")

    @staticmethod
    def validate_app(app: FastAPI) -> None:
        missing: list[str] = []
        for route in app.routes:
            if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
                continue
            for method in sorted(route.methods or ()):
                if method in {"HEAD", "OPTIONS"}:
                    continue
                if policy_for_operation(method, route.path) is None:
                    missing.append(f"{method} {route.path}")
        if missing:
            raise RuntimeError(
                "API routes missing explicit authorization policy: "
                + ", ".join(missing)
            )


def _install_openapi_metadata(app: FastAPI) -> None:
    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        components = schema.setdefault("components", {})
        schemes = components.setdefault("securitySchemes", {})
        schemes.setdefault(
            "CodexSession",
            {
                "type": "apiKey",
                "in": "cookie",
                "name": "codex_web_session",
                "description": "Codex Web authenticated browser/API session.",
            },
        )
        schemes.setdefault(
            "BearerAuth",
            {
                "type": "http",
                "scheme": "bearer",
                "description": "Session token or scoped service token.",
            },
        )

        for route in app.routes:
            if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
                continue
            path_item = schema.get("paths", {}).get(route.path_format, {})
            for method in sorted(route.methods or ()):
                if method in {"HEAD", "OPTIONS"}:
                    continue
                operation = path_item.get(method.lower())
                if operation is None:
                    continue
                policy = policy_for_operation(method, route.path)
                if policy is None:
                    continue
                operation["x-codex-authorization"] = policy.openapi()
                if policy.public:
                    operation["security"] = []
                    continue
                operation["security"] = [
                    {"CodexSession": []},
                    {"BearerAuth": []},
                ]
                responses = operation.setdefault("responses", {})
                responses.setdefault(
                    "401",
                    {"description": "Authentication required or invalid."},
                )
                responses.setdefault(
                    "403",
                    {"description": "Authenticated actor lacks required authority."},
                )

        app.openapi_schema = schema
        return schema

    app.openapi_schema = None
    app.openapi = custom_openapi  # type: ignore[method-assign]


def install_api_authorization(
    app: FastAPI,
    authority: AuthorityRoleService,
) -> ApiAuthorizationService:
    service = ApiAuthorizationService(authority)
    service.validate_app(app)
    app.state.api_authorization_service = service
    _install_openapi_metadata(app)
    return service
