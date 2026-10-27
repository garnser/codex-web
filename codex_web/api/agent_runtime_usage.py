from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response

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
        native_session_id: Annotated[list[str] | None, Query()] = None,
        native_turn_id: Annotated[list[str] | None, Query()] = None,
    ) -> Response:
        actor = request_actor(request)
        pairs = None
        if native_session_id is not None or native_turn_id is not None:
            if (not project_id or not provider_id or not include_items
                    or execution_id is not None or agent_session_id is not None or model_id is not None
                    or native_session_id is None or native_turn_id is None
                    or not 1 <= len(native_session_id) <= 40
                    or len(native_session_id) != len(native_turn_id)):
                raise HTTPException(status_code=422, detail='bounded native session/turn pairs and project/provider required')
            pairs = tuple(zip(native_session_id, native_turn_id))
            if len(set(pairs)) != len(pairs) or any(not value or len(value) > 200 for pair in pairs for value in pair):
                raise HTTPException(status_code=422, detail='distinct bounded native session/turn identifiers required')

        def read_projection() -> bytes:
            filters = dict(
                project_id=project_id,
                execution_id=execution_id,
                agent_session_id=agent_session_id,
                provider_id=provider_id,
                runtime_id=runtime_id,
                model_id=model_id,
            )
            if pairs is not None:
                result = service.current_native_turns(actor, project_id=project_id,
                    provider_id=provider_id, runtime_id=runtime_id, pairs=pairs)
            elif not include_items:
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
               runtime_id, model_id, include_items, pairs)
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
