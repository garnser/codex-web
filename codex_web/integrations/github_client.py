from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx


class GitHubAPIError(RuntimeError):
    """An HTTP error response, distinct from transport/response uncertainty."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"GitHub API returned HTTP {status_code}")
        self.status_code = status_code


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
            raise GitHubAPIError(response.status_code)
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(f"GitHub API returned invalid JSON for {path}") from exc

    async def request_bytes(
        self, method: str, api_base: str, path: str, *, token: str,
        max_bytes: int, json_body: dict[str, Any] | None = None,
    ) -> tuple[bytes, bool, str]:
        if max_bytes < 1 or max_bytes > 512 * 1024:
            raise ValueError("GitHub response byte limit must be between 1 and 524288")
        url = f"{api_base.rstrip('/')}/{path.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(
            transport=self.transport, timeout=self.timeout, follow_redirects=True
        ) as client:
            async with client.stream(
                method.upper(), url, headers=headers, json=json_body
            ) as response:
                if response.status_code >= 400:
                    error = RuntimeError(
                        f"GitHub API returned HTTP {response.status_code} for {path}"
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
                return b"".join(chunks), truncated, response.headers.get(
                    "content-type", "application/octet-stream"
                ).split(";", 1)[0]

    async def request_empty(
        self, method: str, api_base: str, path: str, *, token: str,
    ) -> None:
        url = f"{api_base.rstrip('/')}/{path.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout) as client:
            response = await client.request(method.upper(), url, headers=headers)
        if response.status_code >= 400:
            error = RuntimeError(
                f"GitHub API returned HTTP {response.status_code} for {path}"
            )
            setattr(error, "status_code", response.status_code)
            raise error

    async def mark_pull_request_ready_for_review(
        self, api_base: str, *, node_id: str, mutation_id: str, token: str,
    ) -> dict[str, Any]:
        """Only this fixed official mutation is exposed; callers cannot send GraphQL."""
        base = urlsplit(api_base.rstrip("/"))
        if base.scheme != "https" or not base.hostname or base.username or base.password or base.query or base.fragment:
            raise ValueError("GitHub GraphQL base must be a credential-free HTTPS API base")
        path = base.path.rstrip("/")
        path = path[:-3] + "/graphql" if path.endswith("/api/v3") else path + "/graphql"
        endpoint = urlunsplit((base.scheme, base.netloc, path, "", ""))
        query = """mutation ReadyForReview($input: MarkPullRequestReadyForReviewInput!) {
          markPullRequestReadyForReview(input: $input) {
            clientMutationId
            pullRequest { id number isDraft headRefOid baseRefOid baseRefName repository { nameWithOwner } }
          }
        }"""
        async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout) as client:
            response = await client.post(endpoint,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                json={"query": query, "variables": {"input": {
                    "pullRequestId": node_id, "clientMutationId": mutation_id}}})
        if response.status_code >= 400:
            raise GitHubAPIError(response.status_code)
        data = response.json()
        if not isinstance(data, dict) or data.get("errors"):
            raise RuntimeError("GitHub readiness response is uncertain; inspect provider state")
        result = (data.get("data") or {}).get("markPullRequestReadyForReview")
        if not isinstance(result, dict) or result.get("clientMutationId") != mutation_id:
            raise RuntimeError("GitHub readiness response is incomplete; inspect provider state")
        return result

    async def readiness_rules(self, api_base: str, repo: str, base_ref: str, *, token: str) -> dict[str, Any]:
        """Read current effective protection, never alter or bypass it."""
        path = f"repos/{quote(repo, safe='/')}/branches/{quote(base_ref, safe='')}"
        branch = await self.request_json("GET", api_base, path, token=token)
        if not isinstance(branch, dict) or not isinstance(branch.get("protected"), bool):
            raise ValueError("GitHub branch protection visibility is unavailable")
        try:
            protection = await self.request_json("GET", api_base, path + "/protection", token=token)
        except GitHubAPIError as exc:
            if exc.status_code != 404 or branch["protected"]:
                raise
            protection = None  # GitHub's documented unprotected-branch response.
        rules = await self.request_json("GET", api_base,
            f"repos/{quote(repo, safe='/')}/rules/branches/{quote(base_ref, safe='')}", token=token)
        if not isinstance(rules, list) or protection is not None and not isinstance(protection, dict):
            raise ValueError("GitHub readiness protection/rules response is malformed")
        return {"protection": protection, "rules": rules}

    async def actions_job(self, api_base: str, repo: str, job_id: int, *, token: str) -> dict[str, Any]:
        result = await self.request_json(
            "GET", api_base, f"repos/{quote(repo, safe='/')}/actions/jobs/{job_id}", token=token
        )
        return result if isinstance(result, dict) else {}

    async def rerun_actions_job(self, api_base: str, repo: str, job_id: int, *, token: str) -> None:
        await self.request_empty(
            "POST", api_base, f"repos/{quote(repo, safe='/')}/actions/jobs/{job_id}/rerun", token=token
        )

    async def actions_run(self, api_base: str, repo: str, run_id: int, *, token: str) -> dict[str, Any]:
        result = await self.request_json(
            "GET", api_base, f"repos/{quote(repo, safe='/')}/actions/runs/{run_id}", token=token
        )
        return result if isinstance(result, dict) else {}

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
