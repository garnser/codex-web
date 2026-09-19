from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx


class JiraClient:
    """Small async Jira REST transport kept outside canonical task-source code."""

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
        username: str | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{api_base.rstrip('/')}/{path.lstrip('/')}"
        headers = {"Accept": "application/json"}
        auth: httpx.BasicAuth | None = None
        if username:
            auth = httpx.BasicAuth(username, token)
        else:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=self.timeout,
            auth=auth,
        ) as client:
            response = await client.request(
                method.upper(),
                url,
                headers=headers,
                params=params,
                json=json_body,
            )
        if response.status_code >= 400:
            raise RuntimeError(f"Jira API returned HTTP {response.status_code} for {path}")
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(f"Jira API returned invalid JSON for {path}") from exc

    async def search_issues(
        self,
        api_base: str,
        *,
        token: str,
        username: str | None,
        jql: str,
        start_at: int = 0,
        max_results: int = 100,
    ) -> dict[str, Any]:
        payload = await self.request_json(
            "GET",
            api_base,
            "rest/api/3/search",
            token=token,
            username=username,
            params={
                "jql": jql,
                "startAt": start_at,
                "maxResults": max_results,
                "fields": "summary,description,status,assignee,labels,priority,issuetype,parent,updated",
            },
        )
        return payload if isinstance(payload, dict) else {}

    async def issue(
        self,
        api_base: str,
        key: str,
        *,
        token: str,
        username: str | None,
    ) -> dict[str, Any]:
        payload = await self.request_json(
            "GET",
            api_base,
            f"rest/api/3/issue/{quote(key, safe='')}",
            token=token,
            username=username,
            params={
                "fields": "summary,description,status,assignee,labels,priority,issuetype,parent,updated",
            },
        )
        return payload if isinstance(payload, dict) else {}

    async def create_issue(
        self,
        api_base: str,
        *,
        token: str,
        username: str | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self.request_json(
            "POST",
            api_base,
            "rest/api/3/issue",
            token=token,
            username=username,
            json_body=payload,
        )
        return response if isinstance(response, dict) else {}

    async def update_issue(
        self,
        api_base: str,
        key: str,
        *,
        token: str,
        username: str | None,
        payload: dict[str, Any],
    ) -> None:
        await self.request_json(
            "PUT",
            api_base,
            f"rest/api/3/issue/{quote(key, safe='')}",
            token=token,
            username=username,
            json_body=payload,
        )

    async def add_comment(
        self,
        api_base: str,
        key: str,
        *,
        token: str,
        username: str | None,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        payload = await self.request_json(
            "POST",
            api_base,
            f"rest/api/3/issue/{quote(key, safe='')}/comment",
            token=token,
            username=username,
            json_body={"body": body},
        )
        return payload if isinstance(payload, dict) else {}

    async def transitions(
        self,
        api_base: str,
        key: str,
        *,
        token: str,
        username: str | None,
    ) -> tuple[dict[str, Any], ...]:
        payload = await self.request_json(
            "GET",
            api_base,
            f"rest/api/3/issue/{quote(key, safe='')}/transitions",
            token=token,
            username=username,
        )
        values = payload.get("transitions", []) if isinstance(payload, dict) else []
        return tuple(item for item in values if isinstance(item, dict))

    async def transition_issue(
        self,
        api_base: str,
        key: str,
        transition_id: str,
        *,
        token: str,
        username: str | None,
    ) -> None:
        await self.request_json(
            "POST",
            api_base,
            f"rest/api/3/issue/{quote(key, safe='')}/transitions",
            token=token,
            username=username,
            json_body={"transition": {"id": transition_id}},
        )
