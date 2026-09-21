from __future__ import annotations

import asyncio
import base64
import hmac
import os
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.action_providers import ACTION_PROVIDER_CONTRACT
from codex_web.api.identity import request_actor
from codex_web.compatibility import (
    API_CONTRACT,
    CANONICAL_EVENT_CONTRACT,
    PERSISTED_RECORD_CONTRACT,
    TASK_SOURCE_CONTRACT,
    ContractCompatibilityError,
)
from codex_web.identity import PrincipalKind
from codex_web.models import BotRouteTest
from codex_web.services.bot_routing import BotRoutingService
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.runtime_diagnostics import (
    RuntimeDiagnosticsService,
    RuntimeHealthService,
    StaticAssetVersionService,
)


def _codex_verifier_credentials() -> tuple[str, str] | None:
    user = os.environ.get("CODEX_WEB_VERIFIER_USER", "").strip()
    password = os.environ.get("CODEX_WEB_VERIFIER_PASSWORD", "")
    if not user or not password:
        return None
    return user, password


def _basic_auth_credentials(header_value: str | None) -> tuple[str, str] | None:
    if not header_value:
        return None
    scheme, _, encoded = header_value.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded.encode("ascii"), validate=True).decode("utf-8")
    except Exception:
        return None
    username, separator, password = decoded.partition(":")
    if not separator:
        return None
    return username, password


def build_system_router(
    static_assets: StaticAssetVersionService,
    runtime_health: RuntimeHealthService,
    diagnostics_service: RuntimeDiagnosticsService,
    routing_service: BotRoutingService,
) -> APIRouter:
    router = APIRouter(tags=["system"])

    def require_runtime_reader(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if not any(
                scope in actor.service_scopes
                for scope in ("runtime:read", "runtime:admin")
            ):
                raise HTTPException(
                    status_code=403,
                    detail="runtime:read or runtime:admin service scope required",
                )
            return actor
        try:
            IdentityService.require_admin(actor)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return actor

    def require_project_scope(actor, project_id: str | None) -> None:
        if not project_id:
            return
        try:
            project = diagnostics_service.project_lookup(project_id)
        except Exception as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc
        if (
            getattr(project, "organization_id", None) != actor.organization_id
            or getattr(project, "workspace_id", None) != actor.workspace_id
        ):
            raise HTTPException(status_code=404, detail="Project not found")

    @router.get("/api/livez")
    async def livez() -> dict[str, Any]:
        return {
            "ok": True,
            "status": "alive",
            "version": static_assets.version(),
            "time": time.time(),
        }

    @router.get("/api/compatibility")
    async def compatibility(api_version: str | None = None) -> dict[str, Any]:
        if api_version is not None:
            try:
                API_CONTRACT.require(api_version)
            except (ContractCompatibilityError, ValueError) as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "api_version_unsupported",
                        "received": api_version,
                        "supported": list(API_CONTRACT.supported),
                    },
                ) from exc
        return {
            "api": API_CONTRACT.metadata(),
            "canonical_event": CANONICAL_EVENT_CONTRACT.metadata(),
            "task_source": TASK_SOURCE_CONTRACT.metadata(),
            "action_provider": ACTION_PROVIDER_CONTRACT.metadata(),
            "persisted_record": PERSISTED_RECORD_CONTRACT.metadata(),
        }

    @router.get("/api/auth-verifier")
    async def auth_verifier(request: Request) -> dict[str, Any]:
        expected = _codex_verifier_credentials()
        if not expected:
            raise HTTPException(status_code=404, detail="auth verifier disabled")
        provided = _basic_auth_credentials(request.headers.get("authorization"))
        if (
            not provided
            or not hmac.compare_digest(provided[0], expected[0])
            or not hmac.compare_digest(provided[1], expected[1])
        ):
            raise HTTPException(
                status_code=401,
                detail="authentication required",
                headers={"WWW-Authenticate": 'Basic realm="VeridataOps codex-web verifier"'},
            )
        health = runtime_health.health()
        return {
            "ok": health["ok"],
            "verified": True,
            "mode": "basic-auth-verifier",
            "version": static_assets.version(),
        }

    @router.get("/api/diagnostics")
    async def diagnostics(
        request: Request,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        actor = require_runtime_reader(request)
        require_project_scope(actor, project_id)
        if await request.is_disconnected():
            raise HTTPException(status_code=499, detail="client disconnected")
        result = await asyncio.to_thread(
            diagnostics_service.snapshot,
            project_id,
        )
        if await request.is_disconnected():
            raise HTTPException(status_code=499, detail="client disconnected")
        return result

    @router.get("/api/diagnostics/targets")
    async def diagnostics_targets(
        request: Request,
        kind: str,
        project_id: str | None = None,
        after: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        actor = require_runtime_reader(request)
        require_project_scope(actor, project_id)
        if await request.is_disconnected():
            raise HTTPException(status_code=499, detail="client disconnected")
        try:
            result = await asyncio.to_thread(
                diagnostics_service.target_page,
                kind,
                project_id=project_id,
                after=after,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if await request.is_disconnected():
            raise HTTPException(status_code=499, detail="client disconnected")
        return result

    @router.post("/api/diagnostics/route-test")
    async def diagnostics_route_test(
        payload: BotRouteTest,
        request: Request,
    ) -> dict[str, Any]:
        require_runtime_reader(request)
        return routing_service.preview(payload)

    return router
