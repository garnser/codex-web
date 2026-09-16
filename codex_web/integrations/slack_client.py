from __future__ import annotations

from typing import Any

import httpx


class SlackClient:
    """Minimal async Slack Web API client used by runtime and discovery paths."""

    def __init__(self, *, timeout: float = 20.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.timeout = timeout
        self.transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout, transport=self.transport)

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

    async def _post(
        self,
        client: httpx.AsyncClient,
        method: str,
        token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await client.post(
            f"https://slack.com/api/{method}",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    async def list_channels(self, token: str) -> list[dict[str, str]]:
        channels: list[dict[str, str]] = []
        cursor = ""
        async with self._client() as client:
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
        async with self._client() as client:
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

    async def socket_url(self, app_token: str) -> str:
        async with self._client() as client:
            payload = await self._post(client, "apps.connections.open", app_token, {})
        url = str(payload.get("url") or "").strip()
        if not payload.get("ok") or not url:
            raise RuntimeError(f"Slack Socket Mode connection failed: {payload}")
        return url

    async def post_message(
        self,
        token: str,
        channel: str,
        text: str,
        *,
        username: str | None = None,
        icon_emoji: str | None = None,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"channel": channel, "text": text}
        if username:
            payload["username"] = username[:80]
        if icon_emoji:
            payload["icon_emoji"] = icon_emoji
        if thread_ts:
            payload["thread_ts"] = thread_ts
        if blocks:
            payload["blocks"] = blocks

        async with self._client() as client:
            response = await self._post(client, "chat.postMessage", token, payload)
            if not response.get("ok") and (username or icon_emoji):
                fallback: dict[str, Any] = {"channel": channel, "text": text}
                if thread_ts:
                    fallback["thread_ts"] = thread_ts
                if blocks:
                    fallback["blocks"] = blocks
                response = await self._post(client, "chat.postMessage", token, fallback)
        return {"sent": bool(response.get("ok")), "providerResponse": response}

    async def update_message(
        self,
        token: str,
        channel: str,
        message_ts: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"channel": channel, "ts": message_ts, "text": text}
        if blocks:
            payload["blocks"] = blocks
        async with self._client() as client:
            response = await self._post(client, "chat.update", token, payload)
        return {"sent": bool(response.get("ok")), "providerResponse": response}
