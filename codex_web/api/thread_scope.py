from __future__ import annotations

from fastapi import Request

from codex_web.api.identity import request_actor
from codex_web.services.thread_scope import ThreadScopeService


def thread_scope_dependency(service: ThreadScopeService):
    async def authorize(request: Request, project_id: str | None = None) -> None:
        actor = request_actor(request)
        requested = project_id
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if request.method not in {"GET", "HEAD"} and (content_type == "application/json" or content_type.endswith("+json")):
            try:
                payload = await request.json()
            except ValueError:
                payload = None  # FastAPI retains responsibility for body validation.
            body_project = payload.get("project_id") if isinstance(payload, dict) else None
            if body_project is not None:
                if requested is not None and requested != body_project:
                    raise service.unavailable()
                requested = body_project
        thread_id = request.path_params.get("thread_id")
        if not thread_id and request.url.path.endswith("/api/turns/interrupt"):
            thread_id = request.query_params.get("thread_id")
        project = (service.for_thread(thread_id, actor, requested) if thread_id
                   else service.project(requested, actor))
        request.state.thread_project = project
    return authorize
