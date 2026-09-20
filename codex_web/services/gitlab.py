from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from fastapi import Request

from codex_web.autonomy import (
    AutonomyCycleOutcome,
    AutonomyObservation,
    AutonomyReasoningResult,
)
from codex_web.canonical_events import CanonicalEventType
from codex_web.code_hosts import CodeHostCapability, CodeHostProviderBinding
from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.services.canonical_events import CanonicalEventIngestionService
from codex_web.services.gitlab_dependencies import (
    GitLabOperationalDependencies,
    GitLabRoutingDependencies,
    GitLabWorkItemRuntimeDependencies,
)
from codex_web.services.autonomy_controller import AutonomyController
from codex_web.services.gitlab_task_source_events import GitLabWebhookTaskSource
from codex_web.services.gitlab_code_host import GitLabCodeHostProvider
from codex_web.services.work_item_dependencies import (
    gitlab_project_issue_ref,
    gitlab_token_for_project,
)
from codex_web.models import GitLabProjectRoutingSettings, GitLabRoutingSettings, WorkItemState


class GitLabService:
    """Own GitLab routing, webhook, ServiceDesk and semantic-dedupe workflows."""

    def __init__(
        self,
        host: Any | None = None,
        gitlab: GitLabClient | None = None,
        *,
        canonical_events: CanonicalEventIngestionService | None = None,
        autonomy_controller: AutonomyController | None = None,
        routing: GitLabRoutingDependencies | None = None,
        work_items: GitLabWorkItemRuntimeDependencies | None = None,
        operations: GitLabOperationalDependencies | None = None,
    ) -> None:
        async def _publish_noop(_event: dict[str, Any]) -> None:
            return None

        async def _dispatch_noop(
            _binding: Any,
            _text: str,
            _source: str,
        ) -> dict[str, Any]:
            return {"ok": False, "queued": False}

        async def _notice_noop(
            _binding: Any,
            _payload: dict[str, Any],
            _agent: str | None,
            _result: dict[str, Any],
        ) -> dict[str, Any]:
            return {"sent": False}

        if routing is None:
            if host is None:
                raise TypeError(
                    "GitLabService requires routing dependencies"
                )
            routing = GitLabRoutingDependencies(
                load_settings=getattr(
                    host,
                    "_load_gitlab_routing_settings",
                    lambda: GitLabRoutingSettings(),
                ),
                normalize_strings=getattr(
                    host,
                    "_normalize_string_list",
                    lambda values: list(
                        dict.fromkeys(
                            str(value).strip()
                            for value in (values or [])
                            if str(value).strip()
                        )
                    ),
                ),
                binding_for_agent=getattr(
                    host,
                    "_binding_for_agent",
                    lambda _agent, _project_id: None,
                ),
                preferred_agent_conversations=getattr(
                    host,
                    "_preferred_agent_conversations",
                    lambda _agent, _project_id, channels: channels,
                ),
                clone_binding_to_known_channel=getattr(
                    host,
                    "_clone_binding_to_known_channel",
                    lambda binding, _channel: binding,
                ),
                master_binding=getattr(
                    host,
                    "_master_binding",
                    lambda _project_id: None,
                ),
            )
        if work_items is None:
            if host is None:
                raise TypeError(
                    "GitLabService requires work-item dependencies"
                )
            work_items = GitLabWorkItemRuntimeDependencies(
                split_brain_findings=getattr(
                    host,
                    "_work_item_split_brain_findings",
                    lambda _state: [],
                ),
                coerce_owner=getattr(
                    host,
                    "_coerce_owner",
                    lambda owner: (
                        str(owner).strip().lower()
                        if owner
                        else None
                    ),
                ),
                project_event=getattr(
                    host,
                    "_upsert_work_item_state_from_gitlab_event",
                    lambda _payload, **_kwargs: None,
                ),
                project_lookup=getattr(
                    host,
                    "_project",
                    lambda _project_id: None,
                ),
                load_projects=getattr(
                    host,
                    "_load_projects",
                    lambda: [],
                ),
            )
        if operations is None:
            if host is None:
                raise TypeError(
                    "GitLabService requires operational dependencies"
                )
            hub = getattr(host, "hub", None)
            operations = GitLabOperationalDependencies(
                api_base_url=getattr(
                    host,
                    "GITLAB_API_BASE",
                    os.environ.get(
                        "CODEX_WEB_GITLAB_API_BASE",
                        "https://gitlab.example/api/v4",
                    ),
                ),
                load_support_state=getattr(
                    host,
                    "_load_support_servicedesk_state",
                    lambda: {"tickets": {}, "last_sweep_at": None},
                ),
                save_support_state=getattr(
                    host,
                    "_save_support_servicedesk_state",
                    lambda _state: None,
                ),
                load_semantic_events=getattr(
                    host,
                    "_load_gitlab_semantic_events",
                    lambda: {},
                ),
                save_semantic_events=getattr(
                    host,
                    "_save_gitlab_semantic_events",
                    lambda _events: None,
                ),
                verify_webhook=getattr(
                    host,
                    "_verify_gitlab_webhook",
                    lambda _request: None,
                ),
                append_event=getattr(
                    host,
                    "_append_bot_event",
                    lambda _event: None,
                ),
                publish_event=getattr(
                    hub,
                    "publish",
                    _publish_noop,
                ),
                truncate_text=getattr(
                    host,
                    "_truncate_text",
                    lambda value, limit: str(value)[:limit],
                ),
                dispatch_event=getattr(
                    host,
                    "_dispatch_event_to_binding",
                    _dispatch_noop,
                ),
                format_event_prompt=getattr(
                    host,
                    "_format_gitlab_event_prompt",
                    lambda payload, agent: (
                        f"GitLab event for {agent or 'orchestrator'}: "
                        f"{payload.get('object_kind') or payload.get('event_name') or 'event'}"
                    ),
                ),
                send_event_notice=getattr(
                    host,
                    "_send_gitlab_event_notice",
                    _notice_noop,
                ),
                schedule_recovery=getattr(
                    host,
                    "_schedule_native_recovery_cycles",
                    lambda **_kwargs: False,
                ),
            )

        self.routing = routing
        self.work_items = work_items
        self.operations = operations
        self.gitlab = gitlab or GitLabClient()
        self.code_host = GitLabCodeHostProvider(self.gitlab)
        self.canonical_events = canonical_events
        self.autonomy_controller = autonomy_controller
        self._event_ids: dict[str, float] = {}
        self._compat_event_id = (
            getattr(host, "_gitlab_event_id", None)
            if host is not None
            else None
        )
        self._compat_remember_event = (
            getattr(host, "_remember_gitlab_event", None)
            if host is not None
            else None
        )

    def event_target_agents(
        self,
        payload: dict[str, Any],
        project_settings: GitLabProjectRoutingSettings,
        projected_state: WorkItemState | None,
    ) -> list[str]:
        if projected_state:
            findings = self.work_items.split_brain_findings(projected_state)
            if findings:
                return ["orchestrator"]
            if projected_state.handoff and projected_state.handoff.status == "pending":
                recipient = self.work_items.coerce_owner(projected_state.handoff.to_agent)
                if recipient:
                    return [recipient]
            if projected_state.current_stage == "failed_with_action_owner":
                owner = self.work_items.coerce_owner(projected_state.current_owner or projected_state.next_owner)
                if owner:
                    return [owner]
            owner = self.work_items.coerce_owner(projected_state.current_owner)
            if owner and projected_state.current_stage in {
                "implementation_active",
                "ready_for_validation",
                "validation_running",
                "ready_to_close",
            }:
                return [owner]
        return self.routing_agents(payload, project_settings)

    def routing_enabled_for_project(self, project_id: str | None) -> bool:
        if not project_id:
            return False
        settings = self.routing.load_settings()
        if not settings.enabled:
            return False
        project_settings = settings.projects.get(project_id)
        return bool(project_settings and project_settings.enabled)

    def routing_agents(
        self,
        payload: dict[str, Any],
        project_settings: GitLabProjectRoutingSettings,
    ) -> list[str]:
        explicit = self.routing.normalize_strings(project_settings.route_agents)
        return explicit or self.owner_agents(payload, project_settings)

    def routing_bindings_for_agent(
        self,
        agent: str,
        project_id: str,
        project_settings: GitLabProjectRoutingSettings,
    ) -> list[Any]:
        binding = self.routing.binding_for_agent(agent, project_id)
        if not binding:
            return []
        route_channels = self.routing.normalize_strings(project_settings.channel_ids)
        if not route_channels:
            return [binding]
        preferred_channels = self.routing.preferred_agent_conversations(agent, project_id, route_channels)
        if not preferred_channels:
            return []
        return [self.routing.clone_binding_to_known_channel(binding, channel_id) for channel_id in preferred_channels]

    def routing_bindings_for_master(
        self,
        project_id: str,
        project_settings: GitLabProjectRoutingSettings,
    ) -> list[Any]:
        master = self.routing.master_binding(project_id)
        if not master:
            return []
        route_channels = h._normalize_string_list(project_settings.channel_ids)
        if not route_channels or master.provider != "slack":
            return [master]
        return [self.routing.clone_binding_to_known_channel(master, channel_id) for channel_id in route_channels]

    def event_id(self, request: Request, payload: dict[str, Any]) -> str:
        for header in ("x-gitlab-event-uuid", "x-request-id"):
            value = request.headers.get(header)
            if value:
                return value
        attrs = payload.get("object_attributes") or {}
        project = payload.get("project") or {}
        parts = [
            str(payload.get("object_kind") or payload.get("event_name") or "gitlab"),
            str(project.get("id") or project.get("path_with_namespace") or ""),
            str(attrs.get("id") or attrs.get("iid") or attrs.get("sha") or attrs.get("commit_id") or ""),
            str(attrs.get("updated_at") or attrs.get("finished_at") or attrs.get("created_at") or ""),
            str(attrs.get("action") or attrs.get("state") or attrs.get("status") or ""),
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    def remember_event(self, event_id: str) -> bool:
        now = time.time()
        for key, seen_at in list(self._event_ids.items()):
            if now - seen_at > 3600:
                self._event_ids.pop(key, None)
        if event_id in self._event_ids:
            return False
        self._event_ids[event_id] = now
        return True

    def support_servicedesk_project_paths(self) -> list[str]:
        raw = os.environ.get("CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_PATHS") or os.environ.get(
            "CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_PATH"
        )
        values = raw.split(",") if raw else ["veridataops/support"]
        return [value.strip().lower().strip("/") for value in values if value.strip()]

    def support_servicedesk_owner_agent(self) -> str:
        return (os.environ.get("CODEX_WEB_SUPPORT_SERVICEDESK_OWNER_AGENT") or "james").strip().lower() or "james"

    def support_servicedesk_project_matches(self, project_path: str) -> bool:
        normalized = project_path.strip().lower().strip("/")
        return bool(normalized and normalized in self.support_servicedesk_project_paths())

    def support_servicedesk_ticket_key(self, payload: dict[str, Any]) -> str | None:
        attrs = payload.get("object_attributes") or {}
        project = payload.get("project") or {}
        project_id = project.get("id") or project.get("path_with_namespace")
        iid = attrs.get("iid")
        if project_id is None or iid is None:
            return None
        return f"{project_id}:{iid}"

    def is_support_servicedesk_ticket_payload(self, payload: dict[str, Any]) -> bool:
        kind = str(payload.get("object_kind") or payload.get("event_name") or "").lower()
        if kind != "issue":
            return False
        project_path = str((payload.get("project") or {}).get("path_with_namespace") or "")
        if not self.support_servicedesk_project_matches(project_path):
            return False
        attrs = payload.get("object_attributes") or {}
        state = str(attrs.get("state") or payload.get("state") or "").lower()
        action = str(attrs.get("action") or "").lower()
        if action and action not in {"open", "reopen", "sweep"}:
            return False
        return bool(attrs.get("iid")) and state not in {"closed", "merged"}

    def remember_support_servicedesk_ticket(
        self,
        payload: dict[str, Any],
        source: str,
        event_id: str | None = None,
    ) -> bool:
        ticket_key = self.support_servicedesk_ticket_key(payload)
        if not ticket_key:
            return False
        state = self.operations.load_support_state()
        tickets = state.setdefault("tickets", {})
        now = time.time()
        attrs = payload.get("object_attributes") or {}
        project = payload.get("project") or {}
        if ticket_key in tickets:
            tickets[ticket_key]["last_seen_at"] = now
            tickets[ticket_key]["last_source"] = source
            self.operations.save_support_state(state)
            return False
        tickets[ticket_key] = {
            "first_seen_at": now,
            "last_seen_at": now,
            "first_source": source,
            "last_source": source,
            "event_id": event_id,
            "project": project.get("path_with_namespace") or project.get("id"),
            "iid": attrs.get("iid"),
            "title": attrs.get("title"),
            "url": attrs.get("url") or attrs.get("web_url"),
        }
        self.operations.save_support_state(state)
        return True

    def support_servicedesk_ticket_seen(self, payload: dict[str, Any]) -> bool:
        ticket_key = self.support_servicedesk_ticket_key(payload)
        if not ticket_key:
            return False
        return ticket_key in self.operations.load_support_state().get(
            "tickets",
            {},
        )

    def format_support_servicedesk_prompt(self, payload: dict[str, Any], source: str, agent: str) -> str:
        attrs = payload.get("object_attributes") or {}
        labels = self.label_names(payload)
        url = self.url(payload)
        lines = [
            f"Support ServiceDesk ticket intake for {agent}: {self.reference(payload)}",
            f"Intake source: {source}",
        ]
        if labels:
            lines.append("Labels: " + ", ".join(labels))
        if url:
            lines.append(f"URL: {url}")
        if attrs.get("description"):
            lines.append("Ticket description is available in GitLab; inspect the linked ticket only as needed.")
        lines.extend(
            [
                "",
                "Handle Support intake triage for this new ticket. Do not change the ticket-response workflow.",
                "Do not poll generic queues. Do not use Slack tools, Slack connectors, MCP Slack apps, or direct Slack API calls.",
                "Keep any GitLab update concise and avoid repeating prior evidence.",
            ]
        )
        return "\n".join(lines)

    async def dispatch_support_servicedesk_ticket(
        self,
        payload: dict[str, Any],
        *,
        source: str,
        event_id: str | None = None,
        settings: GitLabRoutingSettings | None = None,
    ) -> dict[str, Any]:
        if not self.is_support_servicedesk_ticket_payload(payload):
            return {"ok": True, "ignored": True, "reason": "not_support_servicedesk_ticket"}
        if self.support_servicedesk_ticket_seen(payload):
            return {
                "ok": True,
                "ignored": True,
                "reason": "duplicate_support_ticket",
                "ticketKey": self.support_servicedesk_ticket_key(payload),
            }

        project_id, project_settings = self.project_settings_for_payload(
            payload,
            settings,
        )
        if not project_id or not project_settings or not project_settings.enabled:
            self.operations.append_event(
                {
                    "type": "support_servicedesk_ticket_ignored",
                    "source": source,
                    "event_id": event_id,
                    "reason": "no_enabled_gitlab_project_route",
                    "ticket_key": self.support_servicedesk_ticket_key(payload),
                }
            )
            return {"ok": True, "accepted": False, "reason": "no_enabled_gitlab_project_route"}

        agent = next(iter(self.owner_agents(payload, project_settings)), self.support_servicedesk_owner_agent())
        binding = (
            self.routing.binding_for_agent(agent, project_id)
            or self.routing.master_binding(project_id)
        )
        if not binding:
            self.operations.append_event(
                {
                    "type": "support_servicedesk_ticket_ignored",
                    "source": source,
                    "event_id": event_id,
                    "reason": "no_matching_binding",
                    "agent": agent,
                    "ticket_key": self.support_servicedesk_ticket_key(payload),
                }
            )
            return {"ok": False, "accepted": False, "reason": "no_matching_binding", "agent": agent}

        self.remember_support_servicedesk_ticket(payload, source, event_id)
        result = await self.operations.dispatch_event(
            binding,
            self.format_support_servicedesk_prompt(
                payload,
                source,
                agent,
            ),
            f"gitlab:servicedesk:{source}",
        )
        target = {
            "agent": agent,
            "threadId": result.get("threadId"),
            "queued": result.get("queued", False),
            "ok": result.get("ok", False),
        }
        self.operations.append_event(
            {
                "type": "support_servicedesk_ticket_dispatched",
                "source": source,
                "event_id": event_id,
                "project_id": project_id,
                "ticket_key": self.support_servicedesk_ticket_key(payload),
                "target": target,
            }
        )
        await self.operations.publish_event(
            {
                "type": "support.servicedesk.ticket",
                "eventId": event_id,
                "projectId": project_id,
                "ticketKey": self.support_servicedesk_ticket_key(payload),
                "target": target,
            }
        )
        return {
            "ok": True,
            "accepted": True,
            "eventId": event_id,
            "ticketKey": self.support_servicedesk_ticket_key(payload),
            "targets": [target],
        }

    def api_base_url(self) -> str:
        base = (
            os.environ.get("CODEX_WEB_GITLAB_BASE_URL")
            or os.environ.get("GITLAB_BASE_URL")
            or "https://dev.veridataops.com/gitlab"
        )
        return base.rstrip("/")

    def api_token(self) -> str | None:
        return os.environ.get("CODEX_WEB_GITLAB_TOKEN") or os.environ.get("GITLAB_TOKEN")

    def support_servicedesk_sweep_project(self) -> str:
        return (
            os.environ.get("CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_ID")
            or os.environ.get("CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_PATH")
            or "veridataops/support"
        ).strip()

    def support_servicedesk_sweep_interval(self) -> int:
        raw = os.environ.get("CODEX_WEB_SUPPORT_SERVICEDESK_SWEEP_INTERVAL_SECONDS", "3600")
        with contextlib.suppress(ValueError):
            return max(0, int(raw))
        return 3600

    def support_servicedesk_sweep_lookback_hours(self) -> int:
        raw = os.environ.get("CODEX_WEB_SUPPORT_SERVICEDESK_SWEEP_LOOKBACK_HOURS", "0")
        with contextlib.suppress(ValueError):
            return max(0, int(raw))
        return 0

    def issue_to_support_servicedesk_payload(
        self,
        issue: dict[str, Any],
        project_path: str,
        project_id: Any,
    ) -> dict[str, Any]:
        labels = issue.get("labels") or []
        return {
            "object_kind": "issue",
            "event_name": "issue",
            "project": {
                "id": project_id,
                "path_with_namespace": project_path,
                "web_url": issue.get("references", {}).get("full"),
            },
            "object_attributes": {
                "id": issue.get("id"),
                "iid": issue.get("iid"),
                "title": issue.get("title"),
                "description": issue.get("description"),
                "state": issue.get("state"),
                "action": "sweep",
                "created_at": issue.get("created_at"),
                "updated_at": issue.get("updated_at"),
                "url": issue.get("web_url"),
                "web_url": issue.get("web_url"),
            },
            "labels": [{"title": label} for label in labels if isinstance(label, str)],
        }

    async def support_servicedesk_sweep_loop(self) -> None:
        interval = self.support_servicedesk_sweep_interval()
        if interval <= 0 or not self.api_token():
            return
        while True:
            try:
                result = await self.sweep_support_servicedesk()
                self.operations.append_event(
                    {
                        "type": "support_servicedesk_sweep_completed",
                        **{key: value for key, value in result.items() if key != "results"},
                    }
                )
            except Exception as exc:
                self.operations.append_event(
                    {
                        "type": "support_servicedesk_sweep_failed",
                        "error": self.operations.truncate_text(str(exc), 500),
                    }
                )
            await asyncio.sleep(interval)

    def semantic_dedupe_seconds(self) -> float:
        try:
            seconds = float(os.environ.get("CODEX_WEB_GITLAB_SEMANTIC_DEDUPE_SECONDS") or "300")
        except ValueError:
            return 300.0
        return max(30.0, seconds)

    def semantic_key_for_state(
        self,
        ref: str | None,
        *,
        kind: str,
        labels: list[str],
        state: str | None,
    ) -> str | None:
        if not ref:
            return None
        payload = {
            "ref": ref,
            "kind": (kind or "issue").strip().lower(),
            "labels": sorted({label.strip() for label in labels if label and label.strip()}),
            "state": (state or "opened").strip().lower(),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def semantic_key(self, payload: dict[str, Any]) -> str | None:
        ref = gitlab_project_issue_ref(payload)
        if not ref:
            return None
        attrs = payload.get("object_attributes") or {}
        kind = str(payload.get("object_kind") or payload.get("event_name") or "issue")
        state = str(attrs.get("state") or attrs.get("status") or "opened")
        return self.semantic_key_for_state(
            ref,
            kind=kind,
            labels=self.label_names(payload),
            state=state,
        )

    def remember_semantic_key(self, key: str | None, *, reason: str) -> bool:
        if not key:
            return True
        now = time.time()
        ttl = self.semantic_dedupe_seconds()
        events = {
            stored_key: seen_at
            for stored_key, seen_at
            in self.operations.load_semantic_events().items()
            if now - seen_at <= max(ttl, 3600.0)
        }
        seen_at = events.get(key)
        if seen_at is not None and now - seen_at < ttl:
            self.operations.append_event(
                {
                    "type": "gitlab_semantic_duplicate_ignored",
                    "reason": reason,
                    "semantic_key": key,
                    "age_seconds": now - seen_at,
                }
            )
            self.operations.save_semantic_events(events)
            return False
        events[key] = now
        self.operations.save_semantic_events(events)
        return True

    def remember_semantic_issue_state(
        self,
        ref: str,
        *,
        labels: list[str],
        state: str | None,
        reason: str,
    ) -> None:
        key = self.semantic_key_for_state(ref, kind="issue", labels=labels, state=state)
        if not key:
            return
        events = self.operations.load_semantic_events()
        events[key] = time.time()
        self.operations.save_semantic_events(events)
        self.operations.append_event({"type": "gitlab_semantic_state_recorded", "reason": reason, "ref": ref})

    def label_names(self, payload: dict[str, Any]) -> list[str]:
        labels: list[str] = []

        def add(value: Any) -> None:
            if isinstance(value, str) and value:
                labels.append(value)
            elif isinstance(value, dict):
                name = value.get("title") or value.get("name")
                if name:
                    labels.append(str(name))

        attrs = payload.get("object_attributes") or {}
        for source in (
            payload.get("labels"),
            attrs.get("labels"),
            (payload.get("changes") or {}).get("labels", {}).get("current"),
        ):
            if isinstance(source, list):
                for item in source:
                    add(item)
        for key in ("labels", "label_names"):
            source = attrs.get(key)
            if isinstance(source, list):
                for item in source:
                    add(item)
        return sorted({label.strip() for label in labels if label and label.strip()})

    def owner_agents(
        self,
        payload: dict[str, Any],
        project_settings: GitLabProjectRoutingSettings,
    ) -> list[str]:
        owners: list[str] = []
        for label in self.label_names(payload):
            match = re.match(r"owner::(.+)", label.strip(), re.IGNORECASE)
            if match:
                owners.append(match.group(1).strip().lower())
        if owners:
            return sorted(set(owners))
        kind = str(payload.get("object_kind") or payload.get("event_name") or "").lower()
        return project_settings.fallback_agents_by_kind.get(kind, [])

    def project_path_matches(self, project_path: str, configured_path: str) -> bool:
        project_path = project_path.strip().lower().strip("/")
        configured_path = configured_path.strip().lower().strip("/")
        if not project_path or not configured_path:
            return False
        return project_path == configured_path or project_path.startswith(f"{configured_path}/")

    def group_path(self, project_settings: GitLabProjectRoutingSettings) -> str | None:
        for path in project_settings.project_paths:
            normalized = (path or "").strip().strip("/")
            if normalized:
                return normalized.split("/", 1)[0]
        return None

    def token_for_project(self, project_id: str) -> str | None:
        return gitlab_token_for_project(
            project_id,
            project_lookup=self.work_items.project_lookup,
        )

    def project_settings_for_payload(
        self,
        payload: dict[str, Any],
        settings: GitLabRoutingSettings | None = None,
    ) -> tuple[str | None, GitLabProjectRoutingSettings | None]:
        project_path = ((payload.get("project") or {}).get("path_with_namespace") or "").lower()
        settings = settings or self.routing.load_settings()
        for project_id, project_settings in settings.projects.items():
            if any(self.project_path_matches(project_path, path) for path in project_settings.project_paths):
                return project_id, project_settings
        return None, None

    def reference(self, payload: dict[str, Any]) -> str:
        attrs = payload.get("object_attributes") or {}
        project = payload.get("project") or {}
        kind = str(payload.get("object_kind") or payload.get("event_name") or "event").replace("_", " ")
        project_name = project.get("path_with_namespace") or project.get("name") or "unknown project"
        iid = attrs.get("iid")
        title = attrs.get("title") or attrs.get("name") or attrs.get("ref") or attrs.get("status") or ""
        if iid:
            return f"{project_name} {kind} !/#{iid}: {title}".strip()
        return f"{project_name} {kind}: {title}".strip()

    def url(self, payload: dict[str, Any]) -> str | None:
        attrs = payload.get("object_attributes") or {}
        return attrs.get("url") or attrs.get("web_url") or (payload.get("project") or {}).get("web_url")

    def _project_scope(
        self,
        project_id: str,
    ) -> tuple[str | None, str | None]:
        project = None
        with contextlib.suppress(Exception):
            project = self.work_items.project_lookup(project_id)
        if project is None:
            with contextlib.suppress(Exception):
                project = next(
                    (
                        item
                        for item in self.work_items.load_projects()
                        if getattr(item, "id", None) == project_id
                    ),
                    None,
                )
        return (
            getattr(project, "organization_id", None),
            getattr(project, "workspace_id", None),
        )

    @staticmethod
    def _canonical_gitlab_type(
        kind: str,
        payload: dict[str, Any],
    ) -> CanonicalEventType:
        normalized = str(kind or "").strip().casefold().replace(" ", "_")
        if normalized in {"merge_request", "merge request"}:
            return CanonicalEventType.PULL_REQUEST
        if normalized in {"pipeline", "build", "job"}:
            return CanonicalEventType.CI_PIPELINE
        if normalized in {"deployment", "deployment_status"}:
            return CanonicalEventType.DEPLOYMENT
        if normalized in {"incident"}:
            return CanonicalEventType.INCIDENT
        attrs = payload.get("object_attributes") or {}
        outcome = str(
            attrs.get("status")
            or attrs.get("state")
            or payload.get("status")
            or ""
        ).strip().casefold()
        if outcome in {"failed", "failure", "error"}:
            return CanonicalEventType.FAILURE
        return CanonicalEventType.TASK_SOURCE

    async def _ingest_canonical_event(
        self,
        payload: dict[str, Any],
        *,
        project_id: str,
        event_id: str,
        kind: str,
    ):
        if self.canonical_events is None:
            return None
        organization_id, workspace_id = self._project_scope(project_id)
        task_source = GitLabWebhookTaskSource(
            self.operations.api_base_url,
            client=self.gitlab,
        )
        normalized = task_source.normalize_event_sync(payload)
        if normalized is not None:
            return await self.canonical_events.ingest_task_source(
                normalized,
                project_id=project_id,
                tenant_id=organization_id,
                workspace_id=workspace_id,
                event_cursor=event_id,
            )

        attrs = payload.get("object_attributes") or {}
        project = payload.get("project") or {}
        if organization_id and workspace_id:
            binding = CodeHostProviderBinding(
                id=f"gitlab-webhook:{project_id}",
                organization_id=organization_id,
                workspace_id=workspace_id,
                provider_type="gitlab",
                provider_instance=self.operations.api_base_url.rstrip("/"),
                base_url=f"{self.operations.api_base_url.rstrip('/')}/api/v4",
                capabilities=(CodeHostCapability.WEBHOOK_NORMALIZE,),
            )
            fact = self.code_host.normalize_webhook(
                binding,
                payload,
                event_kind=kind,
                event_id=event_id,
            )
            return await self.canonical_events.ingest(
                event_type=fact.canonical_event_type,
                source=f"code-host:gitlab:{binding.provider_instance}",
                idempotency_key=f"{binding.provider_instance}:{fact.event_id}",
                payload={
                    "project_id": project_id,
                    "provider_event_type": fact.event_kind,
                    "repository_external_id": fact.repository_external_id,
                    "subject_external_id": fact.subject_external_id,
                    "action": fact.action,
                    "state": fact.state,
                    "web_url": fact.web_url,
                    "provider_payload": fact.provider_payload,
                },
                occurred_at=fact.occurred_at,
                tenant_id=organization_id,
                workspace_id=workspace_id,
            )

        minimal = {
            "project_id": project_id,
            "provider_event_type": kind,
            "project_path": project.get("path_with_namespace"),
            "action": attrs.get("action"),
            "status": attrs.get("status"),
            "state": attrs.get("state"),
            "iid": attrs.get("iid"),
            "ref": attrs.get("ref"),
            "url": attrs.get("url") or attrs.get("web_url"),
        }
        return await self.canonical_events.ingest(
            event_type=self._canonical_gitlab_type(kind, payload),
            source=f"gitlab:{self.operations.api_base_url.rstrip('/')}",
            idempotency_key=event_id,
            payload={key: value for key, value in minimal.items() if value is not None},
            tenant_id=organization_id,
            workspace_id=workspace_id,
        )

    async def handle_event(self, request: Request) -> dict[str, Any]:
        self.operations.verify_webhook(request)
        payload = await request.json()
        settings = self.routing.load_settings()
        if not settings.enabled:
            return {
                "ok": True,
                "ignored": True,
                "reason": "gitlab_routing_disabled",
            }

        kind = str(
            payload.get("object_kind")
            or payload.get("event_name")
            or ""
        ).lower()
        if kind in settings.ignored_event_kinds:
            return {
                "ok": True,
                "ignored": True,
                "reason": "noisy_event_kind",
            }

        event_id = (
            self._compat_event_id(request, payload)
            if callable(self._compat_event_id)
            else self.event_id(request, payload)
        )
        remembered = (
            self._compat_remember_event(event_id)
            if callable(self._compat_remember_event)
            else self.remember_event(event_id)
        )
        if not remembered:
            return {
                "ok": True,
                "ignored": True,
                "reason": "duplicate",
                "eventId": event_id,
            }

        if self.is_support_servicedesk_ticket_payload(payload):
            result = await self.dispatch_support_servicedesk_ticket(
                payload,
                source="webhook",
                event_id=event_id,
                settings=settings,
            )
            return {
                **result,
                "eventId": event_id,
                "serviceDesk": True,
            }

        project_id, project_settings = self.project_settings_for_payload(
            payload,
            settings,
        )
        if not project_id or not project_settings:
            self.operations.append_event(
                {
                    "type": "gitlab_event_ignored",
                    "event_id": event_id,
                    "kind": kind,
                    "reason": "no_matching_project",
                    "project_path": (
                        payload.get("project") or {}
                    ).get("path_with_namespace"),
                }
            )
            return {
                "ok": True,
                "ignored": True,
                "reason": "no_matching_project",
                "eventId": event_id,
            }
        if not project_settings.enabled:
            self.operations.append_event(
                {
                    "type": "gitlab_event_ignored",
                    "event_id": event_id,
                    "kind": kind,
                    "reason": "project_routing_disabled",
                    "project_id": project_id,
                    "project_path": (
                        payload.get("project") or {}
                    ).get("path_with_namespace"),
                }
            )
            return {
                "ok": True,
                "ignored": True,
                "reason": "project_routing_disabled",
                "eventId": event_id,
            }

        semantic_key = self.semantic_key(payload)
        if not self.remember_semantic_key(
            semantic_key,
            reason="gitlab-webhook",
        ):
            return {
                "ok": True,
                "ignored": True,
                "reason": "semantic_duplicate",
                "eventId": event_id,
            }

        canonical_delivery = await self._ingest_canonical_event(
            payload,
            project_id=project_id,
            event_id=event_id,
            kind=kind,
        )
        if (
            canonical_delivery is not None
            and not canonical_delivery.inserted
        ):
            return {
                "ok": True,
                "ignored": True,
                "reason": "canonical_duplicate",
                "eventId": event_id,
                "canonicalEventId": (
                    canonical_delivery.event.event_id
                ),
            }

        projected_state = self.work_items.project_event(
            payload,
            project_id=project_id,
        )
        agents = self.event_target_agents(
            payload,
            project_settings,
            projected_state,
        )
        bindings: list[tuple[str | None, Any]] = []
        for agent in agents:
            bindings.extend(
                (agent, binding)
                for binding in self.routing_bindings_for_agent(
                    agent,
                    project_id,
                    project_settings,
                )
            )
        if not bindings:
            master_bindings = self.routing_bindings_for_master(
                project_id,
                project_settings,
            )
            if not master_bindings:
                return {
                    "ok": False,
                    "accepted": False,
                    "reason": "no_matching_binding",
                    "eventId": event_id,
                }
            bindings.extend(
                (None, binding)
                for binding in master_bindings
            )

        results: list[dict[str, Any]] = []

        async def dispatch_reasoning(
            *_args: Any,
        ) -> AutonomyReasoningResult:
            for agent, binding in bindings:
                prompt = self.operations.format_event_prompt(
                    payload,
                    agent,
                )
                result = await self.operations.dispatch_event(
                    binding,
                    prompt,
                    "gitlab",
                )
                notice = await self.operations.send_event_notice(
                    binding,
                    payload,
                    agent,
                    result,
                )
                results.append(
                    {
                        "agent": agent,
                        "threadId": result.get("threadId"),
                        "queued": result.get("queued", False),
                        "ok": result.get("ok", False),
                        "slackNoticeSent": notice.get(
                            "sent",
                            False,
                        ),
                    }
                )
            return AutonomyReasoningResult(
                summary=(
                    "GitLab event routed through bounded autonomy"
                )
            )

        if (
            self.autonomy_controller is not None
            and canonical_delivery is not None
        ):
            autonomy_cycle = await self.autonomy_controller.process(
                canonical_delivery.event,
                AutonomyObservation(
                    deterministic_resolved=False,
                    reasoning_score=1.0,
                    reason=(
                        "configured GitLab routing requires "
                        "agent reasoning"
                    ),
                ),
                cycle_key=(
                    f"gitlab-route:{project_id}:"
                    f"{canonical_delivery.event.event_type}"
                ),
                reasoner=dispatch_reasoning,
            )
            if (
                autonomy_cycle.outcome
                != AutonomyCycleOutcome.COMPLETED
            ):
                return {
                    "ok": True,
                    "accepted": False,
                    "ignored": True,
                    "reason": (
                        f"autonomy_{autonomy_cycle.outcome.value}"
                    ),
                    "autonomyReason": autonomy_cycle.reason,
                    "autonomyCycleId": autonomy_cycle.id,
                    "eventId": event_id,
                    "canonicalEventId": (
                        canonical_delivery.event.event_id
                    ),
                }
        else:
            await dispatch_reasoning()

        self.operations.append_event(
            {
                "type": "gitlab_event_dispatched",
                "event_id": event_id,
                "kind": kind,
                "project_id": project_id,
                "work_item_ref": (
                    projected_state.ref
                    if projected_state
                    else None
                ),
                "agents": agents,
                "targets": results,
            }
        )
        await self.operations.publish_event(
            {
                "type": "gitlab.event",
                "eventId": event_id,
                "kind": kind,
                "projectId": project_id,
                "targets": results,
            }
        )
        self.operations.schedule_recovery()
        return {
            "ok": True,
            "accepted": True,
            "eventId": event_id,
            "targets": results,
        }

    async def sweep_support_servicedesk(self) -> dict[str, Any]:
        token = self.api_token()
        if not token:
            raise RuntimeError("GitLab token is not configured for Support ServiceDesk sweep")

        project = self.support_servicedesk_sweep_project()
        api_base = f"{self.api_base_url().rstrip('/')}/api/v4"
        project_payload = await self.gitlab.project(api_base, project, token=token)
        project_path = str(project_payload.get("path_with_namespace") or project)
        project_id = project_payload.get("id") or project

        lookback_hours = self.support_servicedesk_sweep_lookback_hours()
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
            self.issue_to_support_servicedesk_payload(
                issue,
                project_path,
                project_id,
            )
            for issue in issues
        ]

        results: list[dict[str, Any]] = []
        settings = self.routing.load_settings()
        for payload in payloads:
            result = await self.dispatch_support_servicedesk_ticket(
                payload,
                source="sweep",
                settings=settings,
            )
            results.append(result)

        state = await asyncio.to_thread(self.operations.load_support_state)
        state["last_sweep_at"] = time.time()
        await asyncio.to_thread(self.operations.save_support_state, state)
        accepted = sum(1 for result in results if result.get("accepted"))
        duplicates = sum(1 for result in results if result.get("reason") == "duplicate_support_ticket")
        return {
            "ok": True,
            "checked": len(payloads),
            "accepted": accepted,
            "duplicates": duplicates,
            "results": results,
        }


def install_gitlab_service(
    app: Any,
    gitlab: GitLabClient | None = None,
    *,
    canonical_events: CanonicalEventIngestionService | None = None,
    autonomy_controller: AutonomyController | None = None,
    routing: GitLabRoutingDependencies,
    work_items: GitLabWorkItemRuntimeDependencies,
    operations: GitLabOperationalDependencies,
) -> GitLabService:
    """Compose the GitLab domain from explicit runtime dependencies."""

    existing = getattr(app.state, "gitlab_service", None)
    if isinstance(existing, GitLabService):
        service = existing
        service.routing = routing
        service.work_items = work_items
        service.operations = operations
        if canonical_events is not None:
            service.canonical_events = canonical_events
        if autonomy_controller is not None:
            service.autonomy_controller = autonomy_controller
        return service

    service = GitLabService(
        None,
        gitlab,
        canonical_events=canonical_events,
        autonomy_controller=autonomy_controller,
        routing=routing,
        work_items=work_items,
        operations=operations,
    )
    app.state.gitlab_service = service
    return service


def install_gitlab_compatibility(
    host: Any,
    service: GitLabService,
) -> None:
    """Expose the historical GitLab helper surface at the legacy edge only."""

    bindings = {
        "_gitlab_event_target_agents": service.event_target_agents,
        "_gitlab_routing_enabled_for_project": (
            service.routing_enabled_for_project
        ),
        "_gitlab_routing_agents": service.routing_agents,
        "_gitlab_routing_bindings_for_agent": (
            service.routing_bindings_for_agent
        ),
        "_gitlab_routing_bindings_for_master": (
            service.routing_bindings_for_master
        ),
        "_gitlab_event_id": service.event_id,
        "_remember_gitlab_event": service.remember_event,
        "_support_servicedesk_project_paths": (
            service.support_servicedesk_project_paths
        ),
        "_support_servicedesk_owner_agent": (
            service.support_servicedesk_owner_agent
        ),
        "_support_servicedesk_project_matches": (
            service.support_servicedesk_project_matches
        ),
        "_support_servicedesk_ticket_key": (
            service.support_servicedesk_ticket_key
        ),
        "_is_support_servicedesk_ticket_payload": (
            service.is_support_servicedesk_ticket_payload
        ),
        "_remember_support_servicedesk_ticket": (
            service.remember_support_servicedesk_ticket
        ),
        "_support_servicedesk_ticket_seen": (
            service.support_servicedesk_ticket_seen
        ),
        "_format_support_servicedesk_prompt": (
            service.format_support_servicedesk_prompt
        ),
        "_dispatch_support_servicedesk_ticket": (
            service.dispatch_support_servicedesk_ticket
        ),
        "_gitlab_api_base_url": service.api_base_url,
        "_gitlab_api_token": service.api_token,
        "_support_servicedesk_sweep_project": (
            service.support_servicedesk_sweep_project
        ),
        "_support_servicedesk_sweep_interval": (
            service.support_servicedesk_sweep_interval
        ),
        "_support_servicedesk_sweep_lookback_hours": (
            service.support_servicedesk_sweep_lookback_hours
        ),
        "_issue_to_support_servicedesk_payload": (
            service.issue_to_support_servicedesk_payload
        ),
        "_run_support_servicedesk_sweep_once": (
            service.sweep_support_servicedesk
        ),
        "_support_servicedesk_sweep_loop": (
            service.support_servicedesk_sweep_loop
        ),
        "_gitlab_semantic_dedupe_seconds": (
            service.semantic_dedupe_seconds
        ),
        "_gitlab_semantic_key_for_state": (
            service.semantic_key_for_state
        ),
        "_gitlab_semantic_key": service.semantic_key,
        "_remember_gitlab_semantic_key": (
            service.remember_semantic_key
        ),
        "_remember_gitlab_semantic_issue_state": (
            service.remember_semantic_issue_state
        ),
        "_gitlab_label_names": service.label_names,
        "_gitlab_owner_agents": service.owner_agents,
        "_gitlab_project_path_matches": service.project_path_matches,
        "_gitlab_group_path": service.group_path,
        "_gitlab_token_for_project": service.token_for_project,
        "_gitlab_project_settings_for_payload": (
            service.project_settings_for_payload
        ),
        "_gitlab_reference": service.reference,
        "_gitlab_url": service.url,
    }
    for name, value in bindings.items():
        setattr(host, name, value)

