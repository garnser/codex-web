from __future__ import annotations

from typing import Any

import httpx


class SlackClient:
    """Minimal async Slack Web API client used by discovery paths."""

    def __init__(self, *, timeout: float = 20.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.timeout = timeout
        self.transport = transport

    async def _get(
        self,
        client: httpx.AsyncClient,
        method: str,
        token: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = await client.get(
            f"https://slack.com/api/{method}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    async def list_channels(self, token: str) -> list[dict[str, str]]:
        channels: list[dict[str, str]] = []
        cursor = ""
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            for _ in range(20):
                params = {
                    "exclude_archived": "true",
                    "limit": "200",
                    "types": "public_channel,private_channel",
                }
                if cursor:
                    params["cursor"] = cursor
                payload = await self._get(client, "conversations.list", token, params)
                if not payload.get("ok"):
                    break
                for channel in payload.get("channels") or []:
                    channel_id = str(channel.get("id") or "").strip()
                    if not channel_id:
                        continue
                    name = str(channel.get("name") or channel.get("name_normalized") or channel_id)
                    channels.append(
                        {
                            "provider": "slack",
                            "id": channel_id,
                            "name": name,
                            "label": name if name.startswith("#") else f"#{name}",
                        }
                    )
                cursor = str((payload.get("response_metadata") or {}).get("next_cursor") or "").strip()
                if not cursor:
                    break
        return channels

    async def channel_info(self, token: str, channel_id: str) -> dict[str, str] | None:
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            payload = await self._get(
                client,
                "conversations.info",
                token,
                {"channel": channel_id},
            )
        if not payload.get("ok"):
            return None
        channel = payload.get("channel") or {}
        name = str(channel.get("name") or channel.get("name_normalized") or "").strip()
        if not name:
            return None
        return {
            "provider": "slack",
            "id": channel_id,
            "name": name,
            "label": name if name.startswith("#") else f"#{name}",
        }
