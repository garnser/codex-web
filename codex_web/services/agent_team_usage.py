from __future__ import annotations

from typing import Any

from codex_web.identity import AuthenticationActor


class AgentTeamUsageService:
    """Derive lifecycle impact from canonical definitions and delegation links."""

    MAX_CONSUMERS = 5000
    MAX_DISPLAY = 100

    def __init__(self, *, teams, projects, automations, assignments, queues) -> None:
        self.teams = teams
        self.projects = projects
        self.automations = automations
        self.assignments = assignments
        self.queues = queues

    def snapshot(self, team_id: str, actor: AuthenticationActor) -> dict[str, Any]:
        rows = []
        projects = {project.id for project in self.projects.list(actor.tenant)}

        def append(kind, object_id, label, *, active, project_id=None, revision=None):
            if len(rows) >= self.MAX_CONSUMERS:
                raise ValueError("team consumer projection exceeds its bounded scan")
            rows.append({
                "object_type": kind, "object_id": object_id, "label": label,
                "blocking": bool(active), "project_id": project_id, "revision": revision,
            })

        for project_id in projects:
            for automation_id, automation, reference in self.automations.list_effective(
                organization_id=actor.organization_id, workspace_id=actor.workspace_id,
                project_id=project_id,
            ):
                if str(automation.target.kind) != "team" or automation.target.id != team_id:
                    continue
                append("automation", automation_id, automation.name,
                       active=str(automation.lifecycle) == "enabled",
                       project_id=project_id, revision=reference.revision)

        execution_ids = set()
        latest_work = {}
        for delegation in self.teams.store.list_delegations(
            organization_id=actor.organization_id, workspace_id=actor.workspace_id, team_id=team_id,
        ):
            if delegation.coordinator_execution_id:
                execution_ids.add(delegation.coordinator_execution_id)
            execution_ids.update(delegation.member_execution_ids.values())
            latest_work[delegation.work_item_id] = delegation
        for work_id, delegation in latest_work.items():
            append("work_item", work_id, "Delegation history", active=False,
                   project_id=delegation.project_id, revision=delegation.team_revision)

        for assignment in self.assignments(actor):
            if (assignment.organization_id != actor.organization_id
                    or assignment.workspace_id != actor.workspace_id
                    or assignment.execution_id not in execution_ids):
                continue
            append("execution", assignment.id, str(assignment.status),
                   active=str(assignment.status) in {"pending", "claimed", "running"},
                   project_id=assignment.project_id)

        for queue in self.queues.list_queues().values():
            for turn in queue:
                if turn.project_id not in projects or turn.execution_id not in execution_ids:
                    continue
                append("queued_turn", turn.id, "Queued delegated invocation", active=True,
                       project_id=turn.project_id)

        rows.sort(key=lambda row: (not row["blocking"], row["object_type"], row["object_id"], row["project_id"] or ""))
        return {
            "schema_version": "1.0", "available": True, "team_id": team_id,
            "items": rows[:self.MAX_DISPLAY], "count": len(rows), "restricted_count": 0,
            "blocking_count": sum(row["blocking"] for row in rows),
            "truncated": len(rows) > self.MAX_DISPLAY,
            "project_ids": sorted({row["project_id"] for row in rows if row["project_id"]}),
            "coverage": ["effective_automations", "delegation_history", "linked_execution_assignments", "linked_queued_turns"],
        }
