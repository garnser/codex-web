from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from codex_web.api.identity import request_actor
from codex_web.services.agent_runtime_telemetry import AgentRuntimeTelemetryService


def build_agent_runtime_usage_router(
    service: AgentRuntimeTelemetryService,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/agent-runtime-usage",
        tags=["agent-runtime-usage"],
    )

    @router.get("")
    async def list_usage(
        request: Request,
        project_id: str | None = None,
        execution_id: str | None = None,
        agent_session_id: str | None = None,
    ) -> dict[str, Any]:
        items = service.list(
            request_actor(request),
            project_id=project_id,
            execution_id=execution_id,
            agent_session_id=agent_session_id,
        )
        return {
            "items": [item.model_dump(mode="json") for item in items],
            "count": len(items),
        }

    return router
