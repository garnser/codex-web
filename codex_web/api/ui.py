from __future__ import annotations

import time

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from codex_web.services.operator_ui import OperatorUiService


def build_ui_router(service: OperatorUiService) -> APIRouter:
    router = APIRouter(tags=["ui"])

    @router.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse(service.index_html())

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
