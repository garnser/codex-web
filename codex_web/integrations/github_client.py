from __future__ import annotations

from typing import Any

import httpx


class GitHubClient:
    """Small async GitHub API transport for provider-neutral code-host reads."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.transport = transport
        self.timeout = timeout

    async def get_json(
        self,
        api_base: str,
        path: str,
        *,
        token: str | None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = f"{api_base.rstrip('/')}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.timeout,
            ) as client:
                response = await client.get(url, headers=headers, params=params)
        except (httpx.TimeoutException, httpx.NetworkError):
            raise
        if response.status_code >= 400:
            error = RuntimeError(
                f"GitHub API returned HTTP {response.status_code} for {path}"
            )
            setattr(error, "status_code", response.status_code)
            raise error
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(f"GitHub API returned invalid JSON for {path}") from exc
