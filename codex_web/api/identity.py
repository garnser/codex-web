from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.identity import (
    AuthenticationAssurance,
    HumanIdentityCreate,
    HumanUserCreate,
    Membership,
    MembershipCreate,
    MembershipRole,
    MembershipUpdate,
    OrganizationCreate,
    PrincipalKind,
    ServiceIdentityCreate,
    ServiceTokenCreate,
    TenantScope,
    WorkspaceCreate,
)
from codex_web.services.identity import (
    AuthenticationError,
    AuthorizationError,
    IdentityError,
    IdentityService,
    identity_http_error,
)


SESSION_COOKIE = "codex_web_session"
CSRF_HEADER = "x-csrf-token"
ORG_HEADER = "x-codex-organization"
WORKSPACE_HEADER = "x-codex-workspace"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def request_actor(request: Request):
    actor = getattr(request.state, "identity_actor", None)
    if actor is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return actor


def _public_state(service: IdentityService, actor) -> dict[str, Any]:
    IdentityService.require_admin(actor)
    state = service.state()
    return {
        "schema_version": state.schema_version,
        "organizations": [item.model_dump(mode="json") for item in state.organizations],
        "workspaces": [item.model_dump(mode="json") for item in state.workspaces],
        "humans": [item.model_dump(mode="json") for item in state.humans],
        "services": [item.model_dump(mode="json") for item in state.services],
        "teams": [item.model_dump(mode="json") for item in state.teams],
        "memberships": [item.model_dump(mode="json") for item in state.memberships],
        "sessions": [
            {
                "id": item.id,
                "identity_id": item.identity_id,
                "organization_id": item.organization_id,
                "workspace_id": item.workspace_id,
                "assurance": item.assurance,
                "created_at": item.created_at,
                "last_seen_at": item.last_seen_at,
                "idle_expires_at": item.idle_expires_at,
                "absolute_expires_at": item.absolute_expires_at,
                "step_up_until": item.step_up_until,
                "rotation": item.rotation,
                "revoked_at": item.revoked_at,
                "revoke_reason": item.revoke_reason,
            }
            for item in state.sessions
        ],
        "service_tokens": [
            {
                "id": item.id,
                "service_identity_id": item.service_identity_id,
                "organization_id": item.organization_id,
                "workspace_id": item.workspace_id,
                "scopes": item.scopes,
                "created_at": item.created_at,
                "created_by": item.created_by,
                "expires_at": item.expires_at,
                "last_used_at": item.last_used_at,
                "rotation": item.rotation,
                "rotated_at": item.rotated_at,
                "rotated_by": item.rotated_by,
                "revoked_at": item.revoked_at,
                "revoked_by": item.revoked_by,
                "revoke_reason": item.revoke_reason,
            }
            for item in state.service_tokens
        ],
        "recovery_factors": [
            item.model_dump(mode="json") for item in state.recovery_factors
        ],
    }


def install_identity_middleware(app: Any, service: IdentityService) -> None:
    if getattr(app.state, "identity_middleware_installed", False):
        return
    app.state.identity_middleware_installed = True

    @app.middleware("http")
    async def identity_boundary(request: Request, call_next: Any):
        mode = (os.environ.get("CODEX_WEB_IDENTITY_MODE") or "local-trusted").strip().lower()
        session_token = request.cookies.get(SESSION_COOKIE)
        auth_header = request.headers.get("authorization", "")
        bearer = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else None
        actor = None
        used_cookie_session = False

        try:
            if session_token:
                used_cookie_session = True
                authenticated = service.authenticate_session(
                    session_token,
                    csrf_token=request.headers.get(CSRF_HEADER),
                    require_csrf=request.method.upper() not in SAFE_METHODS,
                )
                actor = authenticated.actor
            elif bearer:
                try:
                    actor = service.authenticate_session(
                        bearer,
                        require_csrf=False,
                    ).actor
                except AuthenticationError:
                    actor = service.authenticate_service_token(bearer)
            elif mode == "local-trusted":
                actor = service.local_trusted_actor()
            elif request.url.path in {"/api/livez", "/api/auth-verifier"}:
                actor = None
            else:
                raise AuthenticationError("authentication required")

            if actor is not None:
                requested_org = request.headers.get(ORG_HEADER)
                requested_workspace = request.headers.get(WORKSPACE_HEADER)
                if requested_org and requested_org != actor.organization_id:
                    raise AuthorizationError("cross-organization request denied")
                if requested_workspace and requested_workspace != actor.workspace_id:
                    raise AuthorizationError("cross-workspace request denied")
                request.state.identity_actor = actor
                request.state.tenant_scope = actor.tenant
                request.state.used_cookie_session = used_cookie_session

            api_authorization = getattr(
                request.app.state,
                "api_authorization_service",
                None,
            )
            if api_authorization is not None:
                try:
                    api_authorization.authorize_request(request, actor)
                except Exception as exc:
                    from codex_web.api.authorization import ApiAuthorizationError
                    if isinstance(exc, ApiAuthorizationError):
                        from fastapi.responses import JSONResponse
                        return JSONResponse(
                            status_code=exc.status_code,
                            content={"detail": str(exc)},
                        )
                    raise
        except IdentityError as exc:
            error = identity_http_error(exc)
            from fastapi.responses import JSONResponse

            return JSONResponse(status_code=error.status_code, content={"detail": error.detail})

        response = await call_next(request)
        return response


def build_identity_router(service: IdentityService) -> APIRouter:
    router = APIRouter(tags=["identity"])

    @router.get("/api/identity/me")
    async def me(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        return actor.model_dump(mode="json")

    @router.get("/api/identity")
    async def identity_state(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            return _public_state(service, actor)
        except IdentityError as exc:
            raise identity_http_error(exc) from exc

    def require_sensitive_admin(request: Request):
        actor = request_actor(request)
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
        return actor

    @router.get("/api/identity/authentication-status")
    async def authentication_status(request: Request) -> dict[str, Any]:
        try:
            actor = require_sensitive_admin(request)
            state = service.state()
            configured_mode = (
                os.environ.get("CODEX_WEB_IDENTITY_MODE") or "local-trusted"
            ).strip().lower()
            effective_mode = (
                "local-trusted"
                if configured_mode == "local-trusted"
                else "enforced"
            )

            provider_counts: dict[str, int] = {}
            linked_identity_ids: set[str] = set()
            for human in state.humans:
                if human.disabled_at is not None:
                    continue
                for link in human.external_links:
                    provider = str(link.provider or "").strip()
                    if not provider:
                        continue
                    provider_counts[provider] = provider_counts.get(provider, 0) + 1
                    linked_identity_ids.add(human.id)

            recovery_counts: dict[str, int] = {}
            active_recovery_factors = 0
            for factor in state.recovery_factors:
                if factor.disabled_at is not None:
                    continue
                active_recovery_factors += 1
                provider = str(factor.provider or "").strip()
                if provider:
                    recovery_counts[provider] = recovery_counts.get(provider, 0) + 1

            active_sessions = sum(
                1
                for item in state.sessions
                if item.organization_id == actor.organization_id
                and item.workspace_id == actor.workspace_id
                and item.revoked_at is None
            )
            active_service_tokens = sum(
                1
                for item in state.service_tokens
                if item.organization_id == actor.organization_id
                and item.workspace_id == actor.workspace_id
                and item.revoked_at is None
            )

            return {
                "organization_id": actor.organization_id,
                "workspace_id": actor.workspace_id,
                "identity_mode": effective_mode,
                "current_assurance": actor.assurance.value,
                "step_up_active": actor.assurance
                in {
                    AuthenticationAssurance.MFA,
                    AuthenticationAssurance.LOCAL_TRUSTED,
                },
                "session_authentication_supported": True,
                "service_token_authentication_supported": True,
                "external_identity": {
                    "configured": bool(provider_counts),
                    "providers": [
                        {
                            "provider": provider,
                            "linked_identity_count": provider_counts[provider],
                        }
                        for provider in sorted(provider_counts)
                    ],
                    "linked_identity_count": len(linked_identity_ids),
                    "claims_grant_authority": False,
                },
                "recovery": {
                    "configured": active_recovery_factors > 0,
                    "active_factor_count": active_recovery_factors,
                    "providers": [
                        {
                            "provider": provider,
                            "active_factor_count": recovery_counts[provider],
                        }
                        for provider in sorted(recovery_counts)
                    ],
                },
                "active_session_count": active_sessions,
                "active_service_token_count": active_service_tokens,
            }
        except IdentityError as exc:
            raise identity_http_error(exc) from exc

    @router.post("/api/identity/organizations")
    async def create_organization(payload: OrganizationCreate, request: Request) -> dict[str, Any]:
        try:
            require_sensitive_admin(request)
            return service.create_organization(
                name=payload.name,
                organization_id=payload.id,
            ).model_dump(mode="json")
        except IdentityError as exc:
            raise identity_http_error(exc) from exc

    @router.post("/api/identity/workspaces")
    async def create_workspace(payload: WorkspaceCreate, request: Request) -> dict[str, Any]:
        try:
            require_sensitive_admin(request)
            return service.create_workspace(
                organization_id=payload.organization_id,
                name=payload.name,
                workspace_id=payload.id,
            ).model_dump(mode="json")
        except IdentityError as exc:
            raise identity_http_error(exc) from exc

    @router.post("/api/identity/users")
    async def create_human_user(
        payload: HumanUserCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = require_sensitive_admin(request)
            human, membership = service.create_human_user(
                payload,
                actor=actor,
            )
            return {
                "human": human.model_dump(mode="json"),
                "membership": membership.model_dump(mode="json"),
            }
        except IdentityError as exc:
            raise identity_http_error(exc) from exc

    @router.post("/api/identity/humans")
    async def create_human(payload: HumanIdentityCreate, request: Request) -> dict[str, Any]:
        try:
            require_sensitive_admin(request)
            return service.create_human_identity(
                display_name=payload.display_name,
                email=payload.email,
                identity_id=payload.id,
            ).model_dump(mode="json")
        except IdentityError as exc:
            raise identity_http_error(exc) from exc

    @router.post("/api/identity/services")
    async def create_service(payload: ServiceIdentityCreate, request: Request) -> dict[str, Any]:
        try:
            require_sensitive_admin(request)
            return service.create_service_identity(
                payload.name,
                payload.description,
            ).model_dump(mode="json")
        except IdentityError as exc:
            raise identity_http_error(exc) from exc

    @router.post("/api/identity/memberships")
    async def create_membership(payload: MembershipCreate, request: Request) -> dict[str, Any]:
        try:
            actor = require_sensitive_admin(request)
            membership = Membership(
                identity_id=payload.identity_id,
                principal_kind=payload.principal_kind,
                organization_id=payload.organization_id,
                workspace_id=payload.workspace_id,
                roles=payload.roles,
                team_ids=payload.team_ids,
            )
            return service.add_membership(
                membership,
                actor=actor,
            ).model_dump(mode="json")
        except IdentityError as exc:
            raise identity_http_error(exc) from exc

    @router.patch("/api/identity/memberships/{membership_id}")
    async def update_membership(
        membership_id: str,
        payload: MembershipUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = require_sensitive_admin(request)
            return service.update_membership(
                membership_id,
                payload,
                actor=actor,
            ).model_dump(mode="json")
        except IdentityError as exc:
            raise identity_http_error(exc) from exc

    @router.delete("/api/identity/memberships/{membership_id}")
    async def revoke_membership(
        membership_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = require_sensitive_admin(request)
            return service.revoke_membership(
                membership_id,
                actor=actor,
            ).model_dump(mode="json")
        except IdentityError as exc:
            raise identity_http_error(exc) from exc

    @router.post("/api/identity/service-tokens")
    async def create_service_token(payload: ServiceTokenCreate, request: Request) -> dict[str, Any]:
        try:
            actor = require_sensitive_admin(request)
            credentials = service.create_service_token(
                service_identity_id=payload.service_identity_id,
                scope=TenantScope(
                    organization_id=payload.organization_id,
                    workspace_id=payload.workspace_id,
                ),
                scopes=payload.scopes,
                expires_at=payload.expires_at,
                created_by=actor.identity_id,
            )
            # Raw token is returned once at creation and never appears in list APIs.
            return credentials.model_dump(mode="json")
        except IdentityError as exc:
            raise identity_http_error(exc) from exc

    @router.get("/api/identity/sessions")
    async def sessions(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        if actor.principal_kind != PrincipalKind.HUMAN:
            raise HTTPException(status_code=403, detail="human identity required")
        return {
            "items": [
                {
                    "id": item.id,
                    "created_at": item.created_at,
                    "last_seen_at": item.last_seen_at,
                    "idle_expires_at": item.idle_expires_at,
                    "absolute_expires_at": item.absolute_expires_at,
                    "assurance": item.assurance,
                    "step_up_until": item.step_up_until,
                    "revoked_at": item.revoked_at,
                    "current": item.id == actor.session_id,
                }
                for item in service.list_sessions(actor)
            ]
        }

    @router.post("/api/identity/sessions/revoke-others")
    async def revoke_other_sessions(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            return {"revoked": service.revoke_other_sessions(actor)}
        except IdentityError as exc:
            raise identity_http_error(exc) from exc

    @router.delete("/api/identity/sessions/{session_id}")
    async def revoke_session(session_id: str, request: Request) -> dict[str, bool]:
        actor = request_actor(request)
        allowed = session_id == actor.session_id
        if not allowed:
            try:
                IdentityService.require_admin(actor)
                IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
                allowed = True
            except IdentityError as exc:
                raise identity_http_error(exc) from exc
        if not allowed:
            raise HTTPException(status_code=403, detail="session revocation denied")
        try:
            service.revoke_session(session_id, reason=f"revoked-by:{actor.identity_id}")
        except IdentityError as exc:
            raise identity_http_error(exc) from exc
        return {"ok": True}

    @router.post("/api/identity/service-tokens/{token_id}/rotate")
    async def rotate_service_token(token_id: str, request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            IdentityService.require_admin(actor)
            IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
            credentials = service.rotate_service_token(
                token_id,
                rotated_by=actor.identity_id,
            )
            # Rotated raw token is returned once and never appears in list APIs.
            return credentials.model_dump(mode="json")
        except IdentityError as exc:
            raise identity_http_error(exc) from exc

    @router.delete("/api/identity/service-tokens/{token_id}")
    async def revoke_service_token(token_id: str, request: Request) -> dict[str, bool]:
        actor = request_actor(request)
        try:
            IdentityService.require_admin(actor)
            IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
            service.revoke_service_token(
                token_id,
                reason=f"revoked-by:{actor.identity_id}",
                revoked_by=actor.identity_id,
            )
        except IdentityError as exc:
            raise identity_http_error(exc) from exc
        return {"ok": True}

    return router
