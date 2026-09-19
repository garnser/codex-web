from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx


class ServiceNowClient:
    """Async ServiceNow Table API transport for TaskSource adapters."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.transport = transport
        self.timeout = timeout

    async def request_json(
        self,
        method: str,
        api_base: str,
        path: str,
        *,
        token: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{api_base.rstrip('/')}/{path.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=self.timeout,
        ) as client:
            response = await client.request(
                method.upper(),
                url,
                headers=headers,
                params=params,
                json=json_body,
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"ServiceNow API returned HTTP {response.status_code} for {path}"
            )
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"ServiceNow API returned invalid JSON for {path}"
            ) from exc

    async def list_records(
        self,
        api_base: str,
        table: str,
        *,
        token: str,
        query: str,
        fields: tuple[str, ...],
        limit: int,
        offset: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        payload = await self.request_json(
            "GET",
            api_base,
            f"api/now/table/{quote(table, safe='')}",
            token=token,
            params={
                "sysparm_query": query,
                "sysparm_fields": ",".join(fields),
                "sysparm_limit": limit,
                "sysparm_offset": offset,
                "sysparm_display_value": "all",
            },
        )
        values = payload.get("result", []) if isinstance(payload, dict) else []
        return tuple(item for item in values if isinstance(item, dict))

    async def record(
        self,
        api_base: str,
        table: str,
        sys_id: str,
        *,
        token: str,
        fields: tuple[str, ...],
    ) -> dict[str, Any]:
        payload = await self.request_json(
            "GET",
            api_base,
            f"api/now/table/{quote(table, safe='')}/{quote(sys_id, safe='')}",
            token=token,
            params={
                "sysparm_fields": ",".join(fields),
                "sysparm_display_value": "all",
            },
        )
        result = payload.get("result") if isinstance(payload, dict) else None
        return result if isinstance(result, dict) else {}

    async def create_record(
        self,
        api_base: str,
        table: str,
        *,
        token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self.request_json(
            "POST",
            api_base,
            f"api/now/table/{quote(table, safe='')}",
            token=token,
            json_body=payload,
        )
        result = response.get("result") if isinstance(response, dict) else None
        return result if isinstance(result, dict) else {}

    async def update_record(
        self,
        api_base: str,
        table: str,
        sys_id: str,
        *,
        token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self.request_json(
            "PATCH",
            api_base,
            f"api/now/table/{quote(table, safe='')}/{quote(sys_id, safe='')}",
            token=token,
            json_body=payload,
        )
        result = response.get("result") if isinstance(response, dict) else None
        return result if isinstance(result, dict) else {}
