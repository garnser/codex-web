from __future__ import annotations

import asyncio
import time
from typing import Any

from codex_web.integrations.gitlab_client import GitLabClient


class GitLabService:
    """Async GitLab read workflows that feed the existing domain state machines."""

    def __init__(self, host: Any, gitlab: GitLabClient | None = None) -> None:
        self.host = host
        self.gitlab = gitlab or GitLabClient()

    async def sweep_support_servicedesk(self) -> dict[str, Any]:
        token = self.host._gitlab_api_token()
        if not token:
            raise RuntimeError("GitLab token is not configured for Support ServiceDesk sweep")

        project = self.host._support_servicedesk_sweep_project()
        api_base = f"{self.host._gitlab_api_base_url().rstrip('/')}/api/v4"
        project_payload = await self.gitlab.project(api_base, project, token=token)
        project_path = str(project_payload.get("path_with_namespace") or project)
        project_id = project_payload.get("id") or project

        lookback_hours = self.host._support_servicedesk_sweep_lookback_hours()
        created_after = None
        if lookback_hours:
            created_after = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(time.time() - (lookback_hours * 3600)),
            )
        issues = await self.gitlab.project_issues(
            api_base,
            project,
            token=token,
            params={
                "state": "opened",
                "created_after": created_after,
                "order_by": "created_at",
                "sort": "asc",
                "per_page": 100,
            },
        )
        payloads = [
            self.host._issue_to_support_servicedesk_payload(issue, project_path, project_id)
            for issue in issues
        ]

        results: list[dict[str, Any]] = []
        settings = self.host._load_gitlab_routing_settings()
        for payload in payloads:
            result = await self.host._dispatch_support_servicedesk_ticket(
                payload,
                source="sweep",
                settings=settings,
            )
            results.append(result)

        state = await asyncio.to_thread(self.host._load_support_servicedesk_state)
        state["last_sweep_at"] = time.time()
        await asyncio.to_thread(self.host._save_support_servicedesk_state, state)
        accepted = sum(1 for result in results if result.get("accepted"))
        duplicates = sum(1 for result in results if result.get("reason") == "duplicate_support_ticket")
        return {
            "ok": True,
            "checked": len(payloads),
            "accepted": accepted,
            "duplicates": duplicates,
            "results": results,
        }
