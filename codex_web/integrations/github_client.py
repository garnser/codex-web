from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx


class GitHubClient:
    """Small async GitHub REST transport used by the TaskSource adapter."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30.0) -> None:
        self.transport = transport
        self.timeout = timeout

    async def get_json(self, api_base: str, path: str, *, token: str | None,
                       params: dict[str, Any] | None = None) -> Any:
        """Compatibility read used by the code-host provider."""
        url = f"{api_base.rstrip('/')}/{path.lstrip('/')}"
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout) as client:
                response = await client.get(url, headers=headers, params=params)
        except (httpx.TimeoutException, httpx.NetworkError):
            raise
        if response.status_code >= 400:
            error = RuntimeError(f"GitHub API returned HTTP {response.status_code} for {path}")
            setattr(error, "status_code", response.status_code)
            raise error
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(f"GitHub API returned invalid JSON for {path}") from exc

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

    async def list_issue_comments(self, api_base: str, repo: str, number: int, *, token: str) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        for page in range(1, 101):
            result = await self.request_json(
                "GET", api_base, f"repos/{quote(repo, safe='/')}/issues/{number}/comments",
                token=token, params={"per_page": 100, "page": page},
            )
            if not isinstance(result, list):
                return comments
            comments.extend(item for item in result if isinstance(item, dict))
            if len(result) < 100:
                return comments
        raise RuntimeError(
            "GitHub issue comment reconciliation exceeded the bounded 10000-comment scan"
        )

    async def list_pull_requests(
        self, api_base: str, repo: str, *, token: str, head: str, base: str,
    ) -> list[dict[str, Any]]:
        result = await self.request_json(
            "GET", api_base, f"repos/{quote(repo, safe='/')}/pulls",
            token=token,
            params={"state": "all", "head": head, "base": base, "per_page": 100},
        )
        return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []

    async def create_pull_request(
        self, api_base: str, repo: str, *, token: str, payload: dict[str, Any],
    ) -> dict[str, Any]:
        result = await self.request_json(
            "POST", api_base, f"repos/{quote(repo, safe='/')}/pulls",
            token=token, json_body=payload,
        )
        return result if isinstance(result, dict) else {}

    async def pull_request(
        self, api_base: str, repo: str, number: int, *, token: str,
    ) -> dict[str, Any]:
        result = await self.request_json(
            "GET", api_base, f"repos/{quote(repo, safe='/')}/pulls/{number}",
            token=token,
        )
        return result if isinstance(result, dict) else {}

    async def update_pull_request(
        self,
        api_base: str,
        repo: str,
        number: int,
        *,
        token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        result = await self.request_json(
            "PATCH", api_base, f"repos/{quote(repo, safe='/')}/pulls/{number}",
            token=token, json_body=payload,
        )
        return result if isinstance(result, dict) else {}

    async def merge_pull_request(
        self, api_base: str, repo: str, number: int, *, token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        result = await self.request_json(
            "PUT", api_base, f"repos/{quote(repo, safe='/')}/pulls/{number}/merge",
            token=token, json_body=payload,
        )
        return result if isinstance(result, dict) else {}

    async def branch(
        self, api_base: str, repo: str, branch: str, *, token: str,
    ) -> dict[str, Any]:
        result = await self.request_json(
            "GET",
            api_base,
            f"repos/{quote(repo, safe='/')}/branches/{quote(branch, safe='')}",
            token=token,
        )
        return result if isinstance(result, dict) else {}
