from __future__ import annotations

import asyncio
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
        provider_id: str | None = None,
        runtime_id: str | None = None,
        model_id: str | None = None,
        include_items: bool = True,
    ) -> dict[str, Any]:
        actor = request_actor(request)

        def read_projection() -> dict[str, Any]:
            filters = dict(
                project_id=project_id,
                execution_id=execution_id,
                agent_session_id=agent_session_id,
                provider_id=provider_id,
                runtime_id=runtime_id,
                model_id=model_id,
            )
            if not include_items:
                return service.summary(actor, **filters)
            items = service.list(actor, **filters)
            aggregates = service.aggregate_resources(items)
            return {
                "items": [item.model_dump(mode="json") for item in items],
                "count": len(items),
                "resources": [item.model_dump(mode="json") for item in aggregates],
            }

        # The existing validated catalog read, aggregation and serialization
        # stay together, preserving request context without blocking ASGI.
        return await asyncio.to_thread(read_projection)

    return router
