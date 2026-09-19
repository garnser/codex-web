from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.agent_runtime import AgentRuntimeUnsupportedCapability
from codex_web.api.identity import request_actor
from codex_web.services.agent_runtime import (
    AgentRuntimeError,
    AgentRuntimeRegistry,
    AgentSessionService,
)
from codex_web.services.agent_runtime_telemetry import AgentRuntimeTelemetryService
from codex_web.services.agent_session_trace import AgentSessionTraceService


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="agent session not found")
    if isinstance(exc, AgentRuntimeUnsupportedCapability):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, AgentRuntimeError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_agent_sessions_router(
    service: AgentSessionService,
    runtimes: AgentRuntimeRegistry,
    telemetry: AgentRuntimeTelemetryService | None = None,
    trace_service: AgentSessionTraceService | None = None,
) -> APIRouter:
    router = APIRouter(tags=["agent-sessions"])

    async def project_session(item, actor) -> dict[str, Any]:
        registration = runtimes.registration(item.provider_id, item.runtime_id)
        try:
            health = (
                await runtimes.get(item.provider_id, item.runtime_id).health()
            ).value
        except Exception:
            health = "unavailable"
        usage = []
        if telemetry is not None:
            usage = telemetry.list(actor, agent_session_id=item.id)
        latest_usage = usage[0].model_dump(mode="json") if usage else None
        return {
            **item.model_dump(mode="json"),
            "runtime_registration": registration.model_dump(mode="json"),
            "runtime_health": health,
            "latest_usage": latest_usage,
        }

    @router.get("/api/agent-runtimes")
    async def list_runtimes(request: Request) -> dict[str, Any]:
        request_actor(request)
        items = []
        for registration in runtimes.list_registrations():
            try:
                health = (
                    await runtimes.get(
                        registration.provider_id,
                        registration.runtime_id,
                    ).health()
                ).value
            except Exception:
                health = "unavailable"
            items.append(
                {
                    **registration.model_dump(mode="json"),
                    "health": health,
                }
            )
        return {"items": items}

    @router.get("/api/agent-sessions")
    async def list_sessions(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        items = [
            await project_session(item, actor)
            for item in service.list(actor)
        ]
        return {"items": items, "count": len(items)}

    @router.get("/api/agent-sessions/{session_id}")
    async def get_session(session_id: str, request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = service.get(session_id, actor)
            return {"item": await project_session(item, actor)}
        except Exception as exc:
            raise _error(exc) from exc

    @router.get("/api/agent-sessions/{session_id}/trace")
    async def get_session_trace(
        session_id: str,
        request: Request,
    ) -> dict[str, Any]:
        if trace_service is None:
            raise HTTPException(
                status_code=503,
                detail="agent session execution trace is unavailable",
            )
        try:
            return {
                "trace": trace_service.trace(
                    session_id,
                    actor=request_actor(request),
                )
            }
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/api/agent-sessions/{session_id}/interrupt")
    async def interrupt_session(
        session_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            result = await service.interrupt(
                session_id,
                actor=request_actor(request),
            )
            return {"result": result.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/api/agent-sessions/{session_id}/compact")
    async def compact_session(
        session_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            result = await service.compact(
                session_id,
                actor=request_actor(request),
            )
            return {"result": result.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/api/agent-sessions/{session_id}/close")
    async def close_session(
        session_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = await service.close(
                session_id,
                actor=request_actor(request),
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            raise _error(exc) from exc

    return router
