from __future__ import annotations

from typing import Any

import httpx


class TelegramClient:
    """Small async Telegram Bot API client for runtime polling and delivery."""

    def __init__(self, *, timeout: float = 20.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.timeout = timeout
        self.transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout, transport=self.transport)

    async def _request(
        self,
        token: str,
        method: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{token}/{method}"
        async with self._client() as client:
            if payload is not None:
                response = await client.post(url, json=payload)
            else:
                response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    async def get_updates(
        self,
        token: str,
        *,
        offset: int | None = None,
        timeout: int = 0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        return await self._request(token, "getUpdates", params=params)

    async def send_message(self, token: str, chat_id: str, text: str) -> dict[str, Any]:
        response = await self._request(
            token,
            "sendMessage",
            payload={"chat_id": chat_id, "text": text},
        )
        return {"sent": bool(response.get("ok")), "providerResponse": response}
