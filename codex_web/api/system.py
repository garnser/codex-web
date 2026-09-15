from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, FastAPI


def build_system_router(host: Any) -> APIRouter:
    router = APIRouter(tags=["system"])

    @router.get("/api/livez")
    async def livez() -> dict[str, Any]:
        return {
            "ok": True,
            "status": "alive",
            "version": host._static_version(),
            "time": time.time(),
        }

    return router


def install_system_routes(app: FastAPI, host: Any) -> None:
    if getattr(app.state, "system_routes_installed", False):
        return
    app.include_router(build_system_router(host))
    app.state.system_routes_installed = True
