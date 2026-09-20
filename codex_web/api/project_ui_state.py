from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response

from codex_web.api.identity import request_actor
from codex_web.services.project_ui_state import ProjectUiStateService


def build_project_ui_state_router(
    service: ProjectUiStateService,
) -> APIRouter:
    router = APIRouter(tags=["project-ui"])

    @router.get("/api/projects/{project_id}/ui-state")
    async def project_ui_state(
        project_id: str,
        request: Request,
        response: Response,
        search: str | None = None,
        thread_limit: int | None = None,
        thread_cursor: str | None = None,
        include_static: bool = True,
    ) -> Any:
        payload = await service.state(
            project_id,
            actor=request_actor(request),
            search=search,
            thread_limit=thread_limit,
            thread_cursor=thread_cursor,
            include_static=include_static,
        )
        etag = service.etag(payload)
        headers = {
            "ETag": etag,
            "Cache-Control": "private, no-cache",
        }
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        for name, value in headers.items():
            response.headers[name] = value
        return payload

    return router
