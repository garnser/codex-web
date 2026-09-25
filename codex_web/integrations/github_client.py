from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx


class GitHubClient:
    """Small async GitHub REST transport used by the TaskSource adapter."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30.0) -> None:
        self.transport = transport
        self.timeout = timeout

    async def request_json(self, method: str, api_base: str, path: str, *, token: str,
                           params: dict[str, Any] | None = None,
                           json_body: dict[str, Any] | None = None) -> Any:
        url = f"{api_base.rstrip('/')}/{path.lstrip('/')}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout) as client:
            response = await client.request(method.upper(), url, headers=headers, params=params, json=json_body)
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub API returned HTTP {response.status_code} for {path}")
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(f"GitHub API returned invalid JSON for {path}") from exc

    async def list_issues(self, api_base: str, repo: str, *, token: str, state: str = "open") -> list[dict[str, Any]]:
        payload = await self.request_json("GET", api_base, f"repos/{quote(repo, safe='/')}/issues",
                                          token=token, params={"state": state, "per_page": 100})
        return [item for item in payload if isinstance(item, dict) and "pull_request" not in item] if isinstance(payload, list) else []

    async def issue(self, api_base: str, repo: str, number: int, *, token: str) -> dict[str, Any]:
        payload = await self.request_json("GET", api_base, f"repos/{quote(repo, safe='/')}/issues/{number}", token=token)
        return payload if isinstance(payload, dict) else {}

    async def create_issue(self, api_base: str, repo: str, *, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = await self.request_json("POST", api_base, f"repos/{quote(repo, safe='/')}/issues", token=token, json_body=payload)
        return result if isinstance(result, dict) else {}

    async def update_issue(self, api_base: str, repo: str, number: int, *, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = await self.request_json("PATCH", api_base, f"repos/{quote(repo, safe='/')}/issues/{number}", token=token, json_body=payload)
        return result if isinstance(result, dict) else {}

    async def create_comment(self, api_base: str, repo: str, number: int, *, token: str, body: str) -> dict[str, Any]:
        result = await self.request_json("POST", api_base, f"repos/{quote(repo, safe='/')}/issues/{number}/comments",
                                         token=token, json_body={"body": body})
        return result if isinstance(result, dict) else {}
