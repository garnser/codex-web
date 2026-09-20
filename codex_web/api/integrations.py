from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.models import (
    AgentChannelPresenceSettings,
    GitLabRoutingSettings,
)


def _agent_channel_presence_payload(
    settings: AgentChannelPresenceSettings,
) -> dict[str, Any]:
    return settings.model_dump()


def _gitlab_integration_payload(
    settings: GitLabRoutingSettings,
) -> dict[str, Any]:
    return {
        **settings.model_dump(),
        "webhookPath": "/bots/gitlab/events",
        "tokenVerification": bool(
            os.environ.get("CODEX_WEB_GITLAB_WEBHOOK_SECRET")
            or os.environ.get("GITLAB_WEBHOOK_SECRET")
        ),
    }


def build_integrations_router(
    host: Any | None = None,
    gitlab_service: Any | None = None,
    *,
    load_agent_presence: Callable[
        [], AgentChannelPresenceSettings
    ] | None = None,
    save_agent_presence: Callable[
        [AgentChannelPresenceSettings],
        AgentChannelPresenceSettings,
    ] | None = None,
    load_gitlab_routing: Callable[
        [], GitLabRoutingSettings
    ] | None = None,
    save_gitlab_routing: Callable[
        [GitLabRoutingSettings],
        GitLabRoutingSettings,
    ] | None = None,
    event_sink: Callable[[dict[str, Any]], None] | None = None,
    publish_event: Callable[
        [dict[str, Any]], Awaitable[Any]
    ] | None = None,
    truncate_text: Callable[[Any, int], str] | None = None,
) -> APIRouter:
    """Build integration APIs from explicit state/event collaborators.

    The optional host path exists only for historical direct construction.
    Production composition supplies the explicit callbacks.
    """

    if host is not None:
        load_agent_presence = (
            load_agent_presence
            or host._load_agent_channel_presence_settings
        )
        save_agent_presence = (
            save_agent_presence
            or host._save_agent_channel_presence_settings
        )
        load_gitlab_routing = (
            load_gitlab_routing
            or host._load_gitlab_routing_settings
        )
        save_gitlab_routing = (
            save_gitlab_routing
            or host._save_gitlab_routing_settings
        )
        event_sink = event_sink or host._append_bot_event
        publish_event = publish_event or host.hub.publish
        truncate_text = (
            truncate_text or host._truncate_text
        )

    if not all(
        (
            load_agent_presence,
            save_agent_presence,
            load_gitlab_routing,
            save_gitlab_routing,
            event_sink,
            publish_event,
            truncate_text,
        )
    ):
        raise TypeError(
            "integration router requires explicit state/event dependencies"
        )

    router = APIRouter(tags=["integrations"])

    @router.get("/api/integrations/agent-presence")
    async def get_agent_channel_presence() -> dict[str, Any]:
        return _agent_channel_presence_payload(
            load_agent_presence()
        )

    @router.post("/api/integrations/agent-presence")
    async def update_agent_channel_presence(
        payload: AgentChannelPresenceSettings,
    ) -> dict[str, Any]:
        settings = save_agent_presence(payload)
        event_sink(
            {
                "type": "agent_channel_presence_updated",
                "settings": settings.model_dump(),
            }
        )
        await publish_event(
            {
                "type": "agent.channels.updated",
                "settings": _agent_channel_presence_payload(
                    settings
                ),
            }
        )
        return {
            "ok": True,
            **_agent_channel_presence_payload(settings),
        }

    @router.get("/api/integrations/gitlab")
    async def get_gitlab_integration() -> dict[str, Any]:
        return _gitlab_integration_payload(
            load_gitlab_routing()
        )

    @router.post("/api/integrations/gitlab")
    async def update_gitlab_integration(
        payload: GitLabRoutingSettings,
    ) -> dict[str, Any]:
        settings = save_gitlab_routing(payload)
        event_sink(
            {
                "type": "gitlab_routing_updated",
                "settings": settings.model_dump(),
            }
        )
        await publish_event(
            {
                "type": "gitlab.routing.updated",
                "settings": _gitlab_integration_payload(
                    settings
                ),
            }
        )
        return {
            "ok": True,
            **_gitlab_integration_payload(settings),
        }

    @router.post(
        "/api/integrations/gitlab/support-servicedesk/sweep"
    )
    async def sweep_support_servicedesk() -> dict[str, Any]:
        if gitlab_service is None or not gitlab_service.api_token():
            raise HTTPException(
                status_code=503,
                detail=(
                    "GitLab token is not configured for "
                    "Support ServiceDesk sweep"
                ),
            )
        try:
            return await gitlab_service.sweep_support_servicedesk()
        except Exception as exc:
            event_sink(
                {
                    "type": "support_servicedesk_sweep_failed",
                    "error": truncate_text(str(exc), 500),
                }
            )
            raise HTTPException(
                status_code=502,
                detail=str(exc),
            ) from exc

    if gitlab_service is not None:

        @router.post("/bots/gitlab/events")
        async def gitlab_events(
            request: Request,
        ) -> dict[str, Any]:
            return await gitlab_service.handle_event(request)

    return router
