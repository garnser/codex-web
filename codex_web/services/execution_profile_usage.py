from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from codex_web.identity import AuthenticationActor


class ExecutionProfileUsageService:
    """Read-only projection of existing consumers in an explicit Project context."""

    MAX_SCAN = 5000
    MAX_DISPLAY = 100

    def __init__(self, *, catalogs, projects, profiles, assignments, queues,
                 settings, thread_scope) -> None:
        self.catalogs = catalogs
        self.projects = projects
        self.profiles = profiles
        self.assignments = assignments
        self.queues = queues
        self.settings = settings
        self.thread_scope = thread_scope

    def snapshot(self, profile_id: str, project_id: str, actor: AuthenticationActor) -> dict[str, Any]:
        project = self.projects.get(project_id, actor.tenant)
        catalog = self.catalogs.catalog(organization_id=actor.organization_id,
            workspace_id=actor.workspace_id, project_id=project.id)
        profile, reference = self.catalogs.resolve(profile_id, organization_id=actor.organization_id,
            workspace_id=actor.workspace_id, project_id=project.id)
        rows = []
        scanned = 0
        restricted = 0

        def scan():
            nonlocal scanned
            scanned += 1
            if scanned > self.MAX_SCAN:
                raise ValueError("execution profile usage exceeds its bounded scan; impact is unavailable")

        def append(kind, object_id, label, *, project_id=None, revision=None, binding="configured"):
            rows.append({"object_type": kind, "object_id": object_id, "label": label,
                         "project_id": project_id, "revision": revision, "binding": binding})

        if profile.id == catalog.default_profile_id:
            append("project", project.id, "Project default execution profile", project_id=project.id)

        latest = {}
        for item in self.profiles.store.list_revisions(organization_id=actor.organization_id,
                                                       workspace_id=actor.workspace_id):
            scan()
            latest[item.profile_id] = item
        for item in latest.values():
            if (item.execution_profile_id or catalog.default_profile_id) != profile.id:
                continue
            if not self.profiles.can_view(item, actor=actor):
                restricted += 1
                continue
            append("agent_profile", item.profile_id,
                   f"Workspace Agent Profile · {item.name} · {item.lifecycle}", revision=item.revision,
                   binding="configured" if item.execution_profile_id else "inherits_project_default")

        ownership = self.thread_scope.ownership_snapshot()
        for thread_id, settings in self.settings().items():
            scan()
            if (settings.execution_profile_id or catalog.default_profile_id) != profile.id:
                continue
            try:
                self.thread_scope.for_thread(thread_id, actor, project.id, ownership=ownership)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                continue
            append("thread", thread_id, "Thread execution settings", project_id=project.id,
                   binding="configured" if settings.execution_profile_id else "inherits_project_default")

        for queue in self.queues.list_queues().values():
            for turn in queue:
                scan()
                if turn.project_id != project.id or (turn.execution_profile_id or catalog.default_profile_id) != profile.id:
                    continue
                append("queued_turn", turn.id, "Queued invocation · resolves catalog when dispatched",
                       project_id=project.id, binding="pending_resolution")

        for item in self.assignments(actor):
            scan()
            if (item.organization_id != actor.organization_id or item.workspace_id != actor.workspace_id
                    or item.project_id != project.id or item.execution_profile_id != profile.id):
                continue
            pinned = item.execution_profile_definition
            append("execution", item.id, f"Execution · {item.status} · pinned catalog revision",
                   project_id=project.id, revision=pinned.revision if pinned else None, binding="pinned")

        rows.sort(key=lambda row: (row["object_type"], row["object_id"]))
        return {"schema_version": "1.0", "available": True, "profile_id": profile.id,
                "project_id": project.id, "definition": reference.model_dump(mode="json"),
                "items": rows[:self.MAX_DISPLAY], "count": len(rows) + restricted,
                "restricted_count": restricted, "truncated": len(rows) > self.MAX_DISPLAY,
                "coverage": ["project_default", "workspace_agent_profiles", "thread_settings", "queued_turns", "pinned_execution_assignments"]}
