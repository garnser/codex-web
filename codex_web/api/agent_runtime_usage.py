from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Request, Response

from codex_web.api.identity import request_actor
from codex_web.services.agent_runtime_telemetry import AgentRuntimeTelemetryService


def build_agent_runtime_usage_router(
    service: AgentRuntimeTelemetryService,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/agent-runtime-usage",
        tags=["agent-runtime-usage"],
    )

    inflight: dict[tuple[Any, ...], asyncio.Task[bytes]] = {}

    @router.get("", response_model=dict[str, Any])
    async def list_usage(
        request: Request,
        project_id: str | None = None,
        execution_id: str | None = None,
        agent_session_id: str | None = None,
        provider_id: str | None = None,
        runtime_id: str | None = None,
        model_id: str | None = None,
        include_items: bool = True,
    ) -> Response:
        actor = request_actor(request)

        def read_projection() -> bytes:
            filters = dict(
                project_id=project_id,
                execution_id=execution_id,
                agent_session_id=agent_session_id,
                provider_id=provider_id,
                runtime_id=runtime_id,
                model_id=model_id,
            )
            if not include_items:
                result = service.summary(actor, **filters)
            else:
                items = service.list(actor, **filters)
                aggregates = service.aggregate_resources(items)
                result = {
                    "items": [item.model_dump(mode="json") for item in items],
                    "count": len(items),
                    "resources": [item.model_dump(mode="json") for item in aggregates],
                }
            return json.dumps(result, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")).encode("utf-8")

        # Share only overlapping reads, never completed snapshots. Identity,
        # tenant and every filter are part of the key; actor resolution still
        # runs for every request before it can join an existing projection.
        key = (actor.identity_id, actor.organization_id, actor.workspace_id,
               project_id, execution_id, agent_session_id, provider_id,
               runtime_id, model_id, include_items)
        task = inflight.get(key)
        if task is None or task.done():
            task = asyncio.create_task(asyncio.to_thread(read_projection))
            if len(inflight) < 64:
                inflight[key] = task
            def finish(completed):
                if inflight.get(key) is completed:
                    inflight.pop(key, None)
                if not completed.cancelled():
                    completed.exception()  # Consume errors if all waiters left.
            task.add_done_callback(finish)
        # A disconnected browser cannot cancel another request's shared read.
        content = await asyncio.shield(task)
        return Response(content=content, media_type="application/json")

    return router
