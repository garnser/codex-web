from __future__ import annotations

import uuid

from codex_web.models import Project, ProjectCreate, TaskSourceConfiguration
from codex_web.storage.projects import ProjectRepository
from codex_web.workspaces import WorkspacePathError


class ProjectNotFoundError(LookupError):
    pass


class InvalidProjectPathError(ValueError):
    pass


class LastProjectDeletionError(ValueError):
    pass


class ProjectService:
    def __init__(self, repository: ProjectRepository) -> None:
        self.repository = repository

    def list(self) -> list[Project]:
        return self.repository.load()

    def get(self, project_id: str | None) -> Project:
        projects = self.repository.load()
        if project_id is None:
            if not projects:
                raise ProjectNotFoundError("Project not found")
            return projects[0]
        for project in projects:
            if project.id == project_id:
                return project
        raise ProjectNotFoundError("Project not found")

    def create(self, payload: ProjectCreate) -> Project:
        try:
            path = self.repository.resolve_path(payload.path)
        except WorkspacePathError as exc:
            raise InvalidProjectPathError(str(exc)) from exc
        if not path.exists() or not path.is_dir():
            raise InvalidProjectPathError("Project path must be an existing directory")
        projects = self.repository.load()
        project = Project(
            id=uuid.uuid4().hex[:12],
            name=payload.name,
            path=str(path),
            model=payload.model,
            sandbox=payload.sandbox,
            approval_policy=payload.approval_policy,
            authoritative_task_source=payload.authoritative_task_source,
        )
        projects.append(project)
        self.repository.save(projects)
        return project

    def set_authoritative_task_source(
        self,
        project_id: str,
        source: TaskSourceConfiguration | None,
    ) -> Project:
        """Set or clear the one authoritative external task source for a project.

        The singular canonical field makes multiple simultaneous mutable sources
        unrepresentable. Provider credentials and provider-specific routing
        settings remain outside this project-level authority binding.
        """

        projects = self.repository.load()
        for index, project in enumerate(projects):
            if project.id != project_id:
                continue
            updated = project.model_copy(update={"authoritative_task_source": source})
            projects[index] = updated
            self.repository.save(projects)
            return updated
        raise ProjectNotFoundError("Project not found")

    def delete(self, project_id: str) -> None:
        projects = self.repository.load()
        kept = [project for project in projects if project.id != project_id]
        if len(kept) == len(projects):
            raise ProjectNotFoundError("Project not found")
        if not kept:
            raise LastProjectDeletionError("At least one project is required")
        self.repository.save(kept)
