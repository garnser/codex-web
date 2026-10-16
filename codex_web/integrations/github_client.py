from __future__ import annotations

import json
import math
from typing import Any
from urllib.parse import quote

import httpx


class GitHubAPIError(RuntimeError):
    """An HTTP error response, distinct from transport/response uncertainty."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"GitHub API returned HTTP {status_code}")
        self.status_code = status_code
        self.provider_reason: str | None = None


class GitHubReadIncompleteError(RuntimeError):
    """A read cannot establish complete provider facts; never an empty result."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"GitHub read is incomplete: {reason}")


def _decode_bounded_json(content):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError("non-JSON numeric constant")

    def finite_float(text):
        number = float(text)
        if not math.isfinite(number):
            raise ValueError("JSON number exceeds finite range")
        return number

    try:
        value = json.loads(content, object_pairs_hook=unique_object,
                           parse_constant=invalid_constant, parse_float=finite_float)
    except (ValueError, UnicodeError, RecursionError):
        raise GitHubReadIncompleteError("invalid_json") from None
    if not isinstance(value, (dict, list)):
        raise GitHubReadIncompleteError("json_shape")

    def bounded_depth(item, depth=1):
        if depth > 64:
            raise GitHubReadIncompleteError("json_depth")
        if isinstance(item, dict):
            for key in item:
                try:
                    key.encode('utf-8')
                except UnicodeError:
                    raise GitHubReadIncompleteError("invalid_unicode") from None
        children = item.values() if isinstance(item, dict) else item
        for child in children:
            if isinstance(child, (dict, list)):
                bounded_depth(child, depth + 1)
            elif isinstance(child, str):
                try:
                    child.encode('utf-8')
                except UnicodeError:
                    raise GitHubReadIncompleteError("invalid_unicode") from None

    bounded_depth(value)
    return value


async def _error_reason(response, max_bytes):
    """Keep only an explicit protocol reason, never native error text."""
    if (response.status_code != 404
            or response.headers.get('content-type', '').split(';', 1)[0].strip().casefold()
               not in {'application/json', 'application/vnd.github+json'}
            or response.headers.get('content-encoding', '').strip().casefold() not in {'', 'identity'}):
        return None
    content = bytearray()
    try:
        async for chunk in response.aiter_bytes():
            if len(content) + len(chunk) > min(max_bytes, 8192):
                return None
            content.extend(chunk)
        length = response.headers.get('content-length')
        if length is not None and int(length) != len(content):
            return None
        value = _decode_bounded_json(content)
    except (GitHubReadIncompleteError, ValueError, UnicodeError, httpx.HTTPError):
        return None
    if isinstance(value, dict) and value.get('message') == 'Branch not protected':
        return 'branch_not_protected'
    return None


class GitHubClient:
    """Small async GitHub REST transport used by the TaskSource adapter."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30.0) -> None:
        self.transport = transport
        self.timeout = timeout

    async def read_json_bounded(
        self, api_base: str, path: str, *, token: str,
        params: dict[str, Any] | None = None,
        query_body: dict[str, Any] | None = None,
        max_bytes: int = 512 * 1024,
    ) -> dict[str, Any] | list[Any]:
        """Bounded fixed-provider reads, including engine-owned GraphQL queries.

        No API exposes this transport directly. Callers own fixed paths/query
        text and validate endpoint-specific schemas and pagination separately.
        Artifact download redirects retain their separate existing transport.
        """
        if not 1 <= max_bytes <= 512 * 1024:
            raise ValueError("GitHub read byte limit must be between 1 and 524288")
        url = f"{api_base.rstrip('/')}/{path.lstrip('/')}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "Accept-Encoding": "identity",
                   "X-GitHub-Api-Version": "2022-11-28"}
        async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout,
                                     follow_redirects=False) as client:
            async with client.stream("POST" if query_body is not None else "GET", url,
                                     headers=headers, params=params, json=query_body) as response:
                if 300 <= response.status_code < 400:
                    raise GitHubReadIncompleteError("redirect")
                if response.status_code != 200:
                    error = GitHubAPIError(response.status_code)
                    error.provider_reason = await _error_reason(response, max_bytes)
                    raise error
                media = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
                if media not in {"application/json", "application/vnd.github+json"}:
                    raise GitHubReadIncompleteError("media_type")
                if response.headers.get("content-encoding", "").strip().casefold() not in {"", "identity"}:
                    # Reject before httpx inflates a compressed chunk. Checking
                    # decoded length afterward does not bound decompressor work.
                    raise GitHubReadIncompleteError("content_encoding")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > max_bytes:
                        raise GitHubReadIncompleteError("byte_limit")
                    content.extend(chunk)
                length = response.headers.get("content-length")
                if length is not None:
                    try:
                        expected_length = int(length)
                    except ValueError:
                        raise GitHubReadIncompleteError("content_length") from None
                    if expected_length != len(content):
                        raise GitHubReadIncompleteError("content_length")

        return _decode_bounded_json(content)

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
