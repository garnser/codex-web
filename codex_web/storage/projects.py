from __future__ import annotations

import json
from pathlib import Path

from codex_web.models import Project
from codex_web.storage.json_files import atomic_write_text


class ProjectRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[Project]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            projects = [Project(id="home", name="Home", path=str(Path.home()))]
            self.save(projects)
            return projects
        payload = json.loads(self.path.read_text())
        return [Project.model_validate(item) for item in payload]

    def save(self, projects: list[Project]) -> None:
        atomic_write_text(
            self.path,
            json.dumps([project.model_dump() for project in projects], indent=2) + "\n",
        )
