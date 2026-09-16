from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from codex_web.services.slack_provider import SlackProviderService


def build_slack_router(service: SlackProviderService) -> APIRouter:
    router = APIRouter(tags=["bots"])

    @router.post("/bots/slack/events")
    async def slack_events(request: Request) -> dict[str, Any]:
        return await service.handle_webhook(request)

    return router
