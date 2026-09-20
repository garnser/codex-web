from __future__ import annotations

from fastapi import HTTPException

from codex_web.models import Project
from codex_web.services.projects import ProjectNotFoundError, ProjectService


class ProjectRuntimeService:
    """Thread/turn-facing project lookup and runtime parameter composition."""

    def __init__(self, projects: ProjectService) -> None:
        self.projects = projects

    def get(self, project_id: str | None) -> Project:
        try:
            return self.projects.get(project_id)
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc

    def find_by_cwd(self, cwd: str | None) -> Project | None:
        if not cwd:
            return None
        return next(
            (project for project in self.projects.list() if project.path == cwd),
            None,
        )

    @staticmethod
    def params(
        project: Project,
        overrides: dict[str, object] | None = None,
    ) -> dict[str, object]:
        values: dict[str, object] = {
            "cwd": project.path,
            "sandbox": project.sandbox,
            "approvalPolicy": project.approval_policy,
            "approvalsReviewer": "user",
        }
        if project.model:
            values["model"] = project.model
        if overrides:
            for key, value in overrides.items():
                if value is not None:
                    values[key] = value
        return values

    @staticmethod
    def sandbox_policy(mode: str, cwd: str) -> dict[str, object]:
        if mode == "danger-full-access":
            return {"type": "dangerFullAccess"}
        if mode == "read-only":
            return {"type": "readOnly", "networkAccess": False}
        return {
            "type": "workspaceWrite",
            "writableRoots": [cwd],
            "networkAccess": False,
            "excludeTmpdirEnvVar": False,
            "excludeSlashTmp": False,
        }
