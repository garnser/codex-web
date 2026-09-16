from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import Request

from codex_web.integrations.gitlab_client import GitLabClient


class GitLabService:
    """Own GitLab webhook orchestration and asynchronous API workflows."""

    def __init__(self, host: Any, gitlab: GitLabClient | None = None) -> None:
        self.host = host
        self.gitlab = gitlab or GitLabClient()

    async def handle_event(self, request: Request) -> dict[str, Any]:
        h = self.host
        h._verify_gitlab_webhook(request)
        payload = await request.json()
        settings = h._load_gitlab_routing_settings()
        if not settings.enabled:
            return {"ok": True, "ignored": True, "reason": "gitlab_routing_disabled"}

        kind = str(payload.get("object_kind") or payload.get("event_name") or "").lower()
        if kind in settings.ignored_event_kinds:
            return {"ok": True, "ignored": True, "reason": "noisy_event_kind"}

        event_id = h._gitlab_event_id(request, payload)
        if not h._remember_gitlab_event(event_id):
            return {"ok": True, "ignored": True, "reason": "duplicate", "eventId": event_id}

        if h._is_support_servicedesk_ticket_payload(payload):
            result = await h._dispatch_support_servicedesk_ticket(
                payload,
                source="webhook",
                event_id=event_id,
                settings=settings,
            )
            return {**result, "eventId": event_id, "serviceDesk": True}

        project_id, project_settings = h._gitlab_project_settings_for_payload(payload, settings)
        if not project_id or not project_settings:
            h._append_bot_event(
                {
                    "type": "gitlab_event_ignored",
                    "event_id": event_id,
                    "kind": kind,
                    "reason": "no_matching_project",
                    "project_path": (payload.get("project") or {}).get("path_with_namespace"),
                }
            )
            return {
                "ok": True,
                "ignored": True,
                "reason": "no_matching_project",
                "eventId": event_id,
            }
        if not project_settings.enabled:
            h._append_bot_event(
                {
                    "type": "gitlab_event_ignored",
                    "event_id": event_id,
                    "kind": kind,
                    "reason": "project_routing_disabled",
                    "project_id": project_id,
                    "project_path": (payload.get("project") or {}).get("path_with_namespace"),
                }
            )
            return {
                "ok": True,
                "ignored": True,
                "reason": "project_routing_disabled",
                "eventId": event_id,
            }

        semantic_key = h._gitlab_semantic_key(payload)
        if not h._remember_gitlab_semantic_key(semantic_key, reason="gitlab-webhook"):
            return {
                "ok": True,
                "ignored": True,
                "reason": "semantic_duplicate",
                "eventId": event_id,
            }

        projected_state = h._upsert_work_item_state_from_gitlab_event(
            payload,
            project_id=project_id,
        )
        agents = h._gitlab_event_target_agents(payload, project_settings, projected_state)
        bindings: list[tuple[str | None, Any]] = []
        for agent in agents:
            bindings.extend(
                (agent, binding)
                for binding in h._gitlab_routing_bindings_for_agent(
                    agent,
                    project_id,
                    project_settings,
                )
            )
        if not bindings:
            master_bindings = h._gitlab_routing_bindings_for_master(project_id, project_settings)
            if not master_bindings:
                return {
                    "ok": False,
                    "accepted": False,
                    "reason": "no_matching_binding",
                    "eventId": event_id,
                }
            bindings.extend((None, binding) for binding in master_bindings)

        results: list[dict[str, Any]] = []
        for agent, binding in bindings:
            prompt = h._format_gitlab_event_prompt(payload, agent)
            result = await h._dispatch_event_to_binding(binding, prompt, source="gitlab")
            notice = await h._send_gitlab_event_notice(binding, payload, agent, result)
            results.append(
                {
                    "agent": agent,
                    "threadId": result.get("threadId"),
                    "queued": result.get("queued", False),
                    "ok": result.get("ok", False),
                    "slackNoticeSent": notice.get("sent", False),
                }
            )

        h._append_bot_event(
            {
                "type": "gitlab_event_dispatched",
                "event_id": event_id,
                "kind": kind,
                "project_id": project_id,
                "work_item_ref": projected_state.ref if projected_state else None,
                "agents": agents,
                "targets": results,
            }
        )
        await h.hub.publish(
            {
                "type": "gitlab.event",
                "eventId": event_id,
                "kind": kind,
                "projectId": project_id,
                "targets": results,
            }
        )
        h._schedule_native_recovery_cycles()
        return {"ok": True, "accepted": True, "eventId": event_id, "targets": results}

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
