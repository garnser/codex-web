from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from codex_web.approval_requests import TERMINAL_APPROVAL_REQUEST_STATUSES
from codex_web.attention import TERMINAL_ATTENTION_STATUSES
from codex_web.identity import AuthenticationActor, MembershipRole, PrincipalKind
from codex_web.incidents import IncidentStatus
from codex_web.services.agent_runtime import AgentSessionService
from codex_web.services.approval_requests import ApprovalRequestService
from codex_web.services.attention import AttentionService
from codex_web.services.goals import GoalService
from codex_web.services.incidents import IncidentService
from codex_web.services.projects import ProjectService
from codex_web.services.project_readiness import ProjectReadinessService
from codex_web.services.scheduler import SchedulerService
from codex_web.services.work_items import WorkItemService


class HomeOverviewService:
    """Bounded read-only projection over canonical workspace domains."""

    def __init__(
        self,
        *,
        projects: ProjectService,
        work_items: WorkItemService,
        attention: AttentionService,
        approvals: ApprovalRequestService,
        incidents: IncidentService,
        agent_sessions: AgentSessionService,
        goals: GoalService,
        schedules: SchedulerService | None = None,
        readiness: ProjectReadinessService | None = None,
        clock: Callable[[], float] = time.time,
        section_limit: int = 5,
    ) -> None:
        self.projects = projects
        self.work_items = work_items
        self.attention = attention
        self.approvals = approvals
        self.incidents = incidents
        self.agent_sessions = agent_sessions
        self.goals = goals
        self.schedules = schedules
        self.readiness = readiness
        self.clock = clock
        self.section_limit = max(1, min(int(section_limit), 10))

    def _section(
        self,
        items: list[dict[str, Any]],
        *,
        total: int | None = None,
        status: str = "current",
        detail: str | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "fresh_at": float(self.clock()),
            "items": items[: self.section_limit],
            "count": len(items) if total is None else total,
            "detail": detail,
        }

    def _failed_section(self, exc: Exception) -> dict[str, Any]:
        return self._section(
            [],
            total=0,
            status="degraded",
            detail=f"{type(exc).__name__}: {exc}",
        )

    @staticmethod
    def _work_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": item.get("ref"),
            "title": item.get("title") or item.get("ref"),
            "status": item.get("status_label") or item.get("current_stage"),
            "stage": item.get("current_stage"),
            "owner": item.get("current_owner") or item.get("next_owner"),
            "next_action": item.get("next_action"),
            "blocked_reason": item.get("blocker"),
            "updated_at": item.get("updated_at"),
            "href": "#workspace/work",
        }

    async def overview(
        self,
        *,
        actor: AuthenticationActor,
        project_id: str,
    ) -> dict[str, Any]:
        scope = actor.tenant
        project = self.projects.get(project_id, scope)
        sections: dict[str, Any] = {}

        if self.readiness is not None:
            try:
                snapshot = self.readiness.evaluate(project.id, actor=actor, record=False)
                checks = [
                    {
                        "id": check.id,
                        "domain": check.domain,
                        "status": check.status.value,
                        "code": check.code,
                        "message": check.message,
                        "remediation": check.remediation,
                        "remediation_route": check.remediation_route,
                    }
                    for check in snapshot.checks
                ]
                sections["project_readiness"] = self._section(
                    [{
                        "id": project.id,
                        "title": project.name,
                        "semantic_ready": snapshot.semantic_ready,
                        "execution_ready": snapshot.execution_ready,
                        "status": snapshot.status.value,
                        "checks": checks,
                    }],
                )
            except Exception as exc:
                sections["project_readiness"] = self._failed_section(exc)

        try:
            active_page = await self.work_items.list(
                project_id=project.id,
                owner=None,
                stage=None,
                release_gate=None,
                scope=scope,
                limit=max(self.section_limit * 2, 10),
                cursor=None,
            )
            rows = list(active_page.get("items", ()))
            active = [
                self._work_item(item)
                for item in rows
                if item.get("current_stage") != "closed"
            ]
            blocked = [
                self._work_item(item)
                for item in rows
                if item.get("blocker")
                or item.get("current_stage") == "failed_with_action_owner"
            ]
            sections["active_work"] = self._section(active, total=len(active))
            sections["blocked_work"] = self._section(blocked, total=len(blocked))
        except Exception as exc:
            sections["active_work"] = self._failed_section(exc)
            sections["blocked_work"] = self._failed_section(exc)

        try:
            completed_page = await self.work_items.list(
                project_id=project.id,
                owner=None,
                stage="closed",
                release_gate=None,
                scope=scope,
                limit=self.section_limit,
                cursor=None,
            )
            completed = [
                self._work_item(item)
                for item in completed_page.get("items", ())
            ]
            sections["recently_completed"] = self._section(
                completed,
                total=len(completed),
            )
        except Exception as exc:
            sections["recently_completed"] = self._failed_section(exc)

        try:
            items = [
                item
                for item in self.attention.list(actor)
                if item.status not in TERMINAL_ATTENTION_STATUSES
            ]
            items.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
            sections["attention"] = self._section(
                [
                    {
                        "id": item.id,
                        "title": item.reason,
                        "status": item.status.value,
                        "severity": item.severity.value,
                        "owner": item.owner_identity_id,
                        "updated_at": item.updated_at,
                        "href": item.deep_link or "#workspace/inbox",
                    }
                    for item in items
                ],
                total=len(items),
            )
        except Exception as exc:
            sections["attention"] = self._failed_section(exc)

        try:
            items = [
                item
                for item in self.approvals.list(actor)
                if item.status not in TERMINAL_APPROVAL_REQUEST_STATUSES
                and (item.project_id is None or item.project_id == project.id)
            ]
            items.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
            sections["approvals"] = self._section(
                [
                    {
                        "id": item.id,
                        "title": item.reason,
                        "status": item.status.value,
                        "owner": item.requester_identity_id,
                        "updated_at": item.updated_at,
                        "href": "#approvals",
                    }
                    for item in items
                ],
                total=len(items),
            )
        except Exception as exc:
            sections["approvals"] = self._failed_section(exc)

        try:
            items = [
                item
                for item in self.incidents.list(actor)
                if item.status != IncidentStatus.CLOSED
                and (item.project_id is None or item.project_id == project.id)
            ]
            items.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
            sections["incidents"] = self._section(
                [
                    {
                        "id": item.id,
                        "title": item.title,
                        "status": item.status.value,
                        "severity": item.severity.value,
                        "owner": item.commander_identity_id
                        or next(iter(item.owner_identity_ids), None),
                        "updated_at": item.updated_at,
                        "href": "#workspace/operations",
                    }
                    for item in items
                ],
                total=len(items),
            )
        except Exception as exc:
            sections["incidents"] = self._failed_section(exc)

        try:
            items = [
                item
                for item in self.agent_sessions.list(actor)
                if item.project_id == project.id
                and item.status.value not in {"closed", "failed"}
            ]
            items.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
            sections["agents"] = self._section(
                [
                    {
                        "id": item.id,
                        "title": item.model or f"{item.provider_id}/{item.runtime_id}",
                        "status": item.status.value,
                        "owner": item.worker_id,
                        "updated_at": item.updated_at,
                        "href": "#workspace/agents",
                    }
                    for item in items
                ],
                total=len(items),
            )
        except Exception as exc:
            sections["agents"] = self._failed_section(exc)

        try:
            matching = [
                goal
                for goal in self.goals.list(scope=scope)
                if any(
                    binding.project_id == project.id
                    for binding in goal.work_graph_bindings
                )
            ]
            matching.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
            summaries: list[dict[str, Any]] = []
            for goal in matching[: self.section_limit]:
                snapshot = self.goals.snapshot(goal.id, scope=scope)
                summaries.append(
                    {
                        "id": goal.id,
                        "title": goal.title,
                        "status": goal.status.value,
                        "owner": goal.owner_identity_id,
                        "health": snapshot.health.health.value,
                        "completion_fraction": snapshot.progress.completion_fraction,
                        "updated_at": goal.updated_at,
                        "href": "#workspace/goals",
                    }
                )
            sections["goals"] = self._section(summaries, total=len(matching))
        except Exception as exc:
            sections["goals"] = self._failed_section(exc)

        try:
            can_read_schedules = (
                actor.principal_kind == PrincipalKind.SERVICE
                and any(
                    scope in actor.service_scopes
                    for scope in ("scheduler:read", "scheduler:admin")
                )
            ) or (
                actor.principal_kind != PrincipalKind.SERVICE
                and actor.has_role(MembershipRole.OWNER, MembershipRole.ADMIN)
            )
            if self.schedules is None:
                sections["automations"] = self._section(
                    [],
                    total=0,
                    status="degraded",
                    detail="Scheduler is unavailable.",
                )
            elif not can_read_schedules:
                sections["automations"] = self._section(
                    [],
                    total=0,
                    status="denied",
                    detail="Scheduler visibility requires an administrator role.",
                )
            else:
                schedules = [
                    item
                    for item in self.schedules.list()
                    if item.tenant_id == actor.organization_id
                    and item.workspace_id in (None, actor.workspace_id)
                    and (
                        item.payload.get("project_id") is None
                        or item.payload.get("project_id") == project.id
                    )
                ]
                schedules.sort(
                    key=lambda item: (item.updated_at, item.id),
                    reverse=True,
                )
                sections["automations"] = self._section(
                    [
                        {
                            "id": item.id,
                            "title": item.name,
                            "status": item.status.value,
                            "updated_at": item.updated_at,
                            "next_run_at": item.next_run_at,
                            "href": "#workspace/autonomy",
                        }
                        for item in schedules
                    ],
                    total=len(schedules),
                )
        except Exception as exc:
            sections["automations"] = self._failed_section(exc)

        degraded = [
            name for name, section in sections.items()
            if section["status"] == "degraded"
        ]
        return {
            "project": {
                "id": project.id,
                "name": project.name,
                "path": project.path,
            },
            "generated_at": float(self.clock()),
            "status": "partial" if degraded else "current",
            "degraded_sections": degraded,
            "sections": sections,
        }
