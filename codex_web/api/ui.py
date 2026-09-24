from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse

from codex_web.services.operator_ui import OperatorUiService

PROJECT_UI_PAGES = frozenset(
    {
        "overview",
        "work-items",
        "runs",
        "chat",
        "agents",
        "automations",
        "attention",
        "operations",
        "project-settings",
    }
)


def _root_path(request: Request) -> str:
    return str(request.scope.get("root_path") or "").rstrip("/")


def build_ui_router(service: OperatorUiService) -> APIRouter:
    router = APIRouter(tags=["ui"])

    @router.get("/")
    async def index(request: Request) -> HTMLResponse:
        return HTMLResponse(service.index_html(base_href=_root_path(request)))

    @router.get("/projects/{project_id}", name="project_landing")
    async def project_landing(project_id: str, request: Request) -> RedirectResponse:
        url = request.url_for(
            "project_page",
            project_id=project_id,
            page="overview",
        )
        return RedirectResponse(url=str(url), status_code=307)

    @router.get("/projects/{project_id}/{page}", name="project_page")
    async def project_page(
        project_id: str,
        page: str,
        request: Request,
    ) -> HTMLResponse:
        if page not in PROJECT_UI_PAGES:
            raise HTTPException(status_code=404, detail="unknown Project UI page")
        return HTMLResponse(service.index_html(base_href=_root_path(request)))

    @router.get("/devstatus")
    async def devstatus() -> HTMLResponse:
        return HTMLResponse(service.devstatus_html())

    @router.get("/devhealth")
    async def devhealth(request: Request) -> HTMLResponse:
        force_refresh = request.query_params.get("refresh") in {
            "1",
            "true",
            "yes",
        }
        return HTMLResponse(
            service.devhealth_html(force_refresh=force_refresh)
        )

    @router.websocket("/ws")
    async def websocket_events(websocket: WebSocket) -> None:
        await service.event_hub.connect(websocket)
        try:
            await websocket.send_json(
                {
                    "type": "hello",
                    "time": time.time(),
                    **service.event_hub.stream_state(),
                }
            )
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            service.event_hub.disconnect(websocket)

    return router
