from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.models import AgentChannelPresenceSettings, GitLabRoutingSettings


def _agent_channel_presence_payload(settings: AgentChannelPresenceSettings) -> dict[str, Any]:
    return settings.model_dump()


def _gitlab_integration_payload(settings: GitLabRoutingSettings) -> dict[str, Any]:
    return {
        **settings.model_dump(),
        "webhookPath": "/bots/gitlab/events",
        "tokenVerification": bool(
            os.environ.get("CODEX_WEB_GITLAB_WEBHOOK_SECRET")
            or os.environ.get("GITLAB_WEBHOOK_SECRET")
        ),
    }


def build_integrations_router(host: Any, gitlab_service: Any | None = None) -> APIRouter:
    router = APIRouter(tags=["integrations"])

    @router.get("/api/integrations/agent-presence")
    async def get_agent_channel_presence() -> dict[str, Any]:
        return _agent_channel_presence_payload(host._load_agent_channel_presence_settings())

    @router.post("/api/integrations/agent-presence")
    async def update_agent_channel_presence(payload: AgentChannelPresenceSettings) -> dict[str, Any]:
        settings = host._save_agent_channel_presence_settings(payload)
        host._append_bot_event({"type": "agent_channel_presence_updated", "settings": settings.model_dump()})
        await host.hub.publish(
            {"type": "agent.channels.updated", "settings": _agent_channel_presence_payload(settings)}
        )
        return {"ok": True, **_agent_channel_presence_payload(settings)}

    @router.get("/api/integrations/gitlab")
    async def get_gitlab_integration() -> dict[str, Any]:
        return _gitlab_integration_payload(host._load_gitlab_routing_settings())

    @router.post("/api/integrations/gitlab")
    async def update_gitlab_integration(payload: GitLabRoutingSettings) -> dict[str, Any]:
        settings = host._save_gitlab_routing_settings(payload)
        host._append_bot_event({"type": "gitlab_routing_updated", "settings": settings.model_dump()})
        await host.hub.publish({"type": "gitlab.routing.updated", "settings": _gitlab_integration_payload(settings)})
        return {"ok": True, **_gitlab_integration_payload(settings)}

    @router.post("/api/integrations/gitlab/support-servicedesk/sweep")
    async def sweep_support_servicedesk() -> dict[str, Any]:
        if not host._gitlab_api_token():
            raise HTTPException(
                status_code=503,
                detail="GitLab token is not configured for Support ServiceDesk sweep",
            )
        try:
            return await host._run_support_servicedesk_sweep_once()
        except Exception as exc:
            host._append_bot_event(
                {"type": "support_servicedesk_sweep_failed", "error": host._truncate_text(str(exc), 500)}
            )
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    if gitlab_service is not None:
        @router.post("/bots/gitlab/events")
        async def gitlab_events(request: Request) -> dict[str, Any]:
            return await gitlab_service.handle_event(request)

    return router
