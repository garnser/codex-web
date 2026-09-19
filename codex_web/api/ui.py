from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from codex_web.devhealth import build_context as build_devhealth_context, render_html as render_devhealth_html
from codex_web.devstatus import build_context as build_devstatus_context, render_html as render_devstatus_html
from codex_web.paths import STATIC_DIR


def build_ui_router(host: Any) -> APIRouter:
    router = APIRouter(tags=["ui"])

    @router.get("/")
    async def index() -> HTMLResponse:
        version = host._static_version()
        html = (STATIC_DIR / "index.html").read_text()
        html = html.replace('href="static/styles.css"', f'href="static/styles.css?v={version}"')
        html = html.replace('src="static/app.js"', f'src="static/app.js?v={version}"')
        html = html.replace(
            "</body>",
            (
                f'<script src="static/work_items_ui.js?v={version}" type="module"></script>\n'
                f'  <script src="static/orchestration_ui.js?v={version}" type="module"></script>\n'
                f'  <script src="static/approval_requests_ui.js?v={version}" type="module"></script>\n'
                f'  <script src="static/attention_ui.js?v={version}" type="module"></script>\n'
                "  </body>"
            ),
        )
        return HTMLResponse(html)

    @router.get("/devstatus")
    async def devstatus() -> HTMLResponse:
        return HTMLResponse(render_devstatus_html(build_devstatus_context()))

    @router.get("/devhealth")
    async def devhealth(request: Request) -> HTMLResponse:
        force_refresh = request.query_params.get("refresh") in {"1", "true", "yes"}
        queued_turns = sum(len(items) for items in host._load_turn_queues().values())
        status_context = build_devstatus_context(force_refresh=force_refresh)
        return HTMLResponse(
            render_devhealth_html(
                build_devhealth_context(
                    host._daemon_health(),
                    active_turns=len(host._load_active_turns()),
                    queued_turns=queued_turns,
                    status_context=status_context,
                    work_item_stats=host._devhealth_work_item_stats(),
                    refresh_url="/devhealth?refresh=1",
                )
            )
        )

    @router.websocket("/ws")
    async def websocket_events(websocket: WebSocket) -> None:
        await host.hub.connect(websocket)
        try:
            await websocket.send_json({"type": "hello", "time": time.time()})
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            host.hub.disconnect(websocket)

    return router
