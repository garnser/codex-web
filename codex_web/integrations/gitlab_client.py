from __future__ import annotations

from typing import Any
from urllib.parse import quote, quote_plus

import httpx


class GitLabClient:
    """Small async GitLab API transport used by extracted services."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30.0) -> None:
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
        headers = {"PRIVATE-TOKEN": token, "Accept": "application/json"}
        async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout) as client:
            response = await client.request(
                method.upper(),
                url,
                headers=headers,
                params=params,
                json=json_body,
            )
        if response.status_code >= 400:
            raise RuntimeError(f"GitLab API returned HTTP {response.status_code} for {path}")
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(f"GitLab API returned invalid JSON for {path}") from exc

    async def get_json(
        self,
        api_base: str,
        path: str,
        *,
        token: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return await self.request_json(
            "GET",
            api_base,
            path,
            token=token,
            params=params,
        )

    async def group_issues(
        self,
        api_base: str,
        group: str,
        *,
        token: str,
        labels: list[str] | None = None,
        state: str = "opened",
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"state": state, "per_page": 100}
        if labels:
            params["labels"] = ",".join(labels)
        payload = await self.get_json(
            api_base,
            f"groups/{quote_plus(group)}/issues",
            token=token,
            params=params,
        )
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    async def project(
        self,
        api_base: str,
        project: str,
        *,
        token: str,
    ) -> dict[str, Any]:
        payload = await self.get_json(
            api_base,
            f"projects/{quote(project, safe='')}",
            token=token,
        )
        return payload if isinstance(payload, dict) else {}

    async def project_issues(
        self,
        api_base: str,
        project: str,
        *,
        token: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        payload = await self.get_json(
            api_base,
            f"projects/{quote(project, safe='')}/issues",
            token=token,
            params=params,
        )
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    async def project_issue(
        self,
        api_base: str,
        project: str,
        iid: int,
        *,
        token: str,
    ) -> dict[str, Any]:
        payload = await self.get_json(
            api_base,
            f"projects/{quote(project, safe='')}/issues/{iid}",
            token=token,
        )
        return payload if isinstance(payload, dict) else {}

    async def update_project_issue(
        self,
        api_base: str,
        project: str,
        iid: int,
        *,
        token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self.request_json(
            "PUT",
            api_base,
            f"projects/{quote(project, safe='')}/issues/{iid}",
            token=token,
            json_body=payload,
        )
        return response if isinstance(response, dict) else {}
