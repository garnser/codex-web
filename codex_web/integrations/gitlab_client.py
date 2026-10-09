from __future__ import annotations

import asyncio
import json
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

    async def request_bytes(
        self,
        method: str,
        api_base: str,
        path: str,
        *,
        token: str,
        max_bytes: int,
    ) -> tuple[bytes, bool, str]:
        if max_bytes < 1 or max_bytes > 512 * 1024:
            raise ValueError("GitLab response byte limit must be between 1 and 524288")
        url = f"{api_base.rstrip('/')}/{path.lstrip('/')}"
        headers = {"PRIVATE-TOKEN": token, "Accept": "text/plain"}
        origin = httpx.URL(url)

        async def protect_credential(request: httpx.Request) -> None:
            if origin.scheme == "https" and request.url.scheme != "https":
                raise RuntimeError("GitLab byte download refused an insecure redirect")
            if (request.url.scheme, request.url.host, request.url.port) != (
                origin.scheme, origin.host, origin.port
            ):
                request.headers.pop("PRIVATE-TOKEN", None)

        async with httpx.AsyncClient(
            transport=self.transport, timeout=self.timeout, follow_redirects=True,
            event_hooks={"request": [protect_credential]},
        ) as client:
            async with client.stream(method.upper(), url, headers=headers) as response:
                if response.status_code >= 400:
                    error = RuntimeError(
                        f"GitLab API returned HTTP {response.status_code} for {path}"
                    )
                    setattr(error, "status_code", response.status_code)
                    raise error
                chunks: list[bytes] = []
                observed = 0
                truncated = False
                async for chunk in response.aiter_bytes():
                    remaining = max_bytes - observed
                    if remaining <= 0:
                        truncated = True
                        break
                    chunks.append(chunk[:remaining])
                    observed += min(len(chunk), remaining)
                    if len(chunk) > remaining:
                        truncated = True
                        break
                length = response.headers.get("content-length")
                if length:
                    try:
                        truncated = truncated or int(length) > observed
                    except ValueError:
                        truncated = True
                return (
                    b"".join(chunks),
                    truncated,
                    response.headers.get("content-type", "text/plain").split(";", 1)[0],
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

    async def create_project_issue(
        self,
        api_base: str,
        project: str,
        *,
        token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self.request_json(
            "POST",
            api_base,
            f"projects/{quote(project, safe='')}/issues",
            token=token,
            json_body=payload,
        )
        return response if isinstance(response, dict) else {}

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

    async def create_project_issue_note(
        self,
        api_base: str,
        project: str,
        iid: int,
        *,
        token: str,
        body: str,
    ) -> dict[str, Any]:
        response = await self.request_json(
            "POST",
            api_base,
            f"projects/{quote(project, safe='')}/issues/{iid}/notes",
            token=token,
            json_body={"body": body},
        )
        return response if isinstance(response, dict) else {}

    async def project_issue_notes(
        self,
        api_base: str,
        project: str,
        iid: int,
        *,
        token: str,
    ) -> list[dict[str, Any]]:
        notes: list[dict[str, Any]] = []
        for page in range(1, 101):
            response = await self.get_json(
                api_base,
                f"projects/{quote(project, safe='')}/issues/{iid}/notes",
                token=token,
                params={"per_page": 100, "page": page, "sort": "asc"},
            )
            if not isinstance(response, list):
                return notes
            notes.extend(item for item in response if isinstance(item, dict))
            if len(response) < 100:
                return notes
        raise RuntimeError(
            "GitLab issue-note reconciliation exceeded the bounded 10000-note scan"
        )

    async def merge_requests(
        self,
        api_base: str,
        project: str,
        *,
        token: str,
        source_branch: str,
        target_branch: str,
    ) -> list[dict[str, Any]]:
        response = await self.get_json(
            api_base,
            f"projects/{quote(project, safe='')}/merge_requests",
            token=token,
            params={
                "scope": "all",
                "state": "all",
                "source_branch": source_branch,
                "target_branch": target_branch,
                "per_page": 100,
            },
        )
        return (
            [item for item in response if isinstance(item, dict)]
            if isinstance(response, list)
            else []
        )

    async def merge_request(
        self,
        api_base: str,
        project: str,
        iid: int,
        *,
        token: str,
    ) -> dict[str, Any]:
        response = await self.get_json(
            api_base,
            f"projects/{quote(project, safe='')}/merge_requests/{iid}",
            token=token,
        )
        return response if isinstance(response, dict) else {}

    async def merge_request_closes_issues(
        self, api_base: str, project: str, iid: int, *, token: str,
    ) -> list[dict[str, Any]]:
        response = await self.get_json(
            api_base,
            f"projects/{quote(project, safe='')}/merge_requests/{iid}/closes_issues",
            token=token,
        )
        return [item for item in response if isinstance(item, dict)] if isinstance(response, list) else []

    async def issue_related_merge_requests(
        self, api_base: str, project: str, iid: int, *, token: str,
    ) -> list[dict[str, Any]]:
        # Only a complete enumeration can replace verified relation state.
        # Continuations are page numbers on this fixed credential-bound URL,
        # never provider-supplied URLs. Bounds fail visibly, not as absence.
        path = f"projects/{quote(project, safe='')}/issues/{iid}/related_merge_requests"
        url = f"{api_base.rstrip('/')}/{path}"
        headers = {"PRIVATE-TOKEN": token, "Accept": "application/json"}
        items: list[dict[str, Any]] = []
        seen: set[tuple[int | None, int]] = set()
        observed_bytes = 0
        expected_total: int | None = None
        try:
            async with asyncio.timeout(60), httpx.AsyncClient(
                transport=self.transport, timeout=self.timeout,
            ) as client:
                for page in range(1, 11):
                    async with client.stream(
                        "GET", url, headers=headers,
                        params={"per_page": 100, "page": page},
                    ) as response:
                        if response.status_code != 200:
                            raise RuntimeError(
                                f"GitLab API returned HTTP {response.status_code} for {path}"
                            )
                        chunks: list[bytes] = []
                        page_bytes = 0
                        async for chunk in response.aiter_bytes():
                            page_bytes += len(chunk)
                            observed_bytes += len(chunk)
                            if page_bytes > 512 * 1024 or observed_bytes > 4 * 1024 * 1024:
                                raise RuntimeError("GitLab related MR response byte limit exceeded")
                            chunks.append(chunk)
                        try:
                            payload = json.loads(b"".join(chunks))
                        except (ValueError, UnicodeError) as exc:
                            raise RuntimeError("GitLab related MR response is invalid JSON") from exc
                        if not isinstance(payload, list) or len(payload) > 100:
                            raise RuntimeError("GitLab related MR page is invalid")
                        for item in payload:
                            if not isinstance(item, dict) or type(item.get("iid")) is not int or item["iid"] < 1:
                                raise RuntimeError("GitLab related MR item is invalid")
                            target = item.get("target_project_id")
                            key = (target if type(target) is int else None, item["iid"])
                            if key in seen:
                                raise RuntimeError("GitLab related MR pagination repeated an item")
                            seen.add(key)
                            items.append(item)
                        if len(items) > 1000:
                            raise RuntimeError("GitLab related MR item limit exceeded")
                        total = response.headers.get("x-total")
                        if total is not None:
                            if not total.isascii() or not total.isdecimal() or len(total) > 10:
                                raise RuntimeError("GitLab related MR total is invalid")
                            count = int(total)
                            if count > 1000 or (expected_total is not None and count != expected_total):
                                raise RuntimeError("GitLab related MR total exceeds bounds or changed")
                            expected_total = count
                        next_page = response.headers.get("x-next-page")
                        if next_page:
                            if (
                                not payload or not next_page.isascii()
                                or not next_page.isdecimal() or len(next_page) > 10
                                or int(next_page) != page + 1
                            ):
                                raise RuntimeError("GitLab related MR next page is invalid")
                        # Missing pagination headers require an explicit empty
                        # page. A short page alone is not proof of completeness.
                        elif next_page == "" or not payload:
                            if expected_total is not None and len(items) != expected_total:
                                raise RuntimeError("GitLab related MR enumeration is incomplete")
                            return items
                raise RuntimeError("GitLab related MR page limit exceeded")
        except TimeoutError as exc:
            raise RuntimeError("GitLab related MR enumeration timed out") from exc

    async def create_merge_request(
        self,
        api_base: str,
        project: str,
        *,
        token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self.request_json(
            "POST",
            api_base,
            f"projects/{quote(project, safe='')}/merge_requests",
            token=token,
            json_body=payload,
        )
        return response if isinstance(response, dict) else {}

    async def update_merge_request(
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
            f"projects/{quote(project, safe='')}/merge_requests/{iid}",
            token=token,
            json_body=payload,
        )
        return response if isinstance(response, dict) else {}

    async def accept_merge_request(
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
            f"projects/{quote(project, safe='')}/merge_requests/{iid}/merge",
            token=token,
            json_body=payload,
        )
        return response if isinstance(response, dict) else {}

    async def branch(
        self,
        api_base: str,
        project: str,
        branch: str,
        *,
        token: str,
    ) -> dict[str, Any]:
        response = await self.get_json(
            api_base,
            (
                f"projects/{quote(project, safe='')}/repository/branches/"
                f"{quote(branch, safe='')}"
            ),
            token=token,
        )
        return response if isinstance(response, dict) else {}
