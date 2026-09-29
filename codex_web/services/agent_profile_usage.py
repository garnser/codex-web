from __future__ import annotations

from typing import Any

from codex_web.identity import AuthenticationActor


class AgentProfileUsageService:
    """Read-only impact projection over canonical consumers, never a new ledger."""

    MAX_CONSUMERS = 5000
    MAX_DISPLAY = 100

    def __init__(self, *, teams, projects, automations, assignments, queues) -> None:
        self.teams = teams
        self.projects = projects
        self.automations = automations
        self.assignments = assignments
        self.queues = queues

    def snapshot(self, profile_id: str, actor: AuthenticationActor) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        restricted = 0
        blocking = 0
        project_ids: set[str] = set()
        projects = {item.id: item for item in self.projects.list(actor.tenant)}

        def append(kind, object_id, label, *, active, project_id=None, revision=None, visible=True):
            nonlocal blocking, restricted
            blocking += bool(active)
            if len(rows) + restricted >= self.MAX_CONSUMERS:
                raise ValueError("profile consumer projection exceeds its bounded scan")
            if not visible:
                restricted += 1
                return
            if project_id:
                project_ids.add(project_id)
            rows.append({
                "object_type": kind, "object_id": object_id, "label": label,
                "blocking": bool(active), "project_id": project_id, "revision": revision,
            })

        latest = {}
        for team in self.teams.store.list_revisions(
            organization_id=actor.organization_id, workspace_id=actor.workspace_id,
        ):
            if team.team_id not in latest or team.revision > latest[team.team_id].revision:
                latest[team.team_id] = team
        for team in latest.values():
            if profile_id not in {team.leader_profile_id, *(member.profile_id for member in team.members)}:
                continue
            append("team", team.team_id, team.name,
                   active=str(team.lifecycle) == "active", revision=team.revision,
                   visible=self.teams.can_view(team, actor=actor))

        # Resolve effective versions for each tenant-visible Project. A superseded
        # or shadowed Definition must not be mistaken for an active consumer.
        for project_id in projects:
            for automation_id, automation, reference in self.automations.list_effective(
                organization_id=actor.organization_id, workspace_id=actor.workspace_id,
                project_id=project_id,
            ):
                if str(automation.target.kind) != "agent_profile" or automation.target.id != profile_id:
                    continue
                append("automation", automation_id, automation.name,
                       active=str(automation.lifecycle) == "enabled",
                       project_id=project_id, revision=reference.revision)

        for assignment in self.assignments(actor):
            if (assignment.organization_id != actor.organization_id
                    or assignment.workspace_id != actor.workspace_id):
                continue
            binding = getattr(assignment, "agent_profile", None)
            if binding is None or binding.profile_id != profile_id:
                continue
            append("execution", assignment.id, str(assignment.status),
                   active=str(assignment.status) in {"pending", "claimed", "running"},
                   project_id=assignment.project_id, revision=binding.profile_revision)

        for queue in self.queues.list_queues().values():
            for turn in queue:
                if turn.project_id not in projects or turn.agent_profile_id != profile_id:
                    continue
                # Never expose queued message text, reply targets, or credentials.
                append("queued_turn", turn.id, "Queued invocation", active=True,
                       project_id=turn.project_id, revision=turn.agent_profile_revision)

        rows.sort(key=lambda row: (not row["blocking"], row["object_type"], row["object_id"], row["project_id"] or ""))
        return {
            "schema_version": "1.0", "available": True, "profile_id": profile_id,
            "items": rows[:self.MAX_DISPLAY], "count": len(rows) + restricted,
            "restricted_count": restricted, "blocking_count": blocking,
            "truncated": len(rows) > self.MAX_DISPLAY,
            "project_ids": sorted(project_ids),
            "coverage": ["teams", "effective_automations", "execution_assignments", "queued_turns"],
        }
