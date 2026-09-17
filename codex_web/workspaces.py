from __future__ import annotations

import os
from pathlib import Path


class WorkspacePathError(ValueError):
    """Raised when a project path escapes the configured workspace boundary."""


class WorkspaceMapper:
    """Translate portable project paths to runtime filesystem paths.

    When ``root`` is configured, relative project paths are persisted relative to
    that root and resolved beneath it at runtime. ``source_root`` optionally
    identifies a previous/native absolute workspace prefix so existing project
    records can be migrated when the same repositories are mounted elsewhere
    (for example, ``/srv/development`` on the host -> ``/workspace`` in Docker).

    With no configured root the mapper preserves the historical native behavior:
    project paths resolve as normal absolute filesystem paths.
    """

    def __init__(self, root: Path | None = None, source_root: Path | None = None) -> None:
        self.root = self._absolute(root) if root is not None else None
        self.source_root = self._absolute(source_root) if source_root is not None else None
        if self.root is None:
            self.source_root = None

    @classmethod
    def from_environment(cls) -> "WorkspaceMapper":
        root_value = (os.environ.get("CODEX_WEB_WORKSPACE_ROOT") or "").strip()
        source_value = (os.environ.get("CODEX_WEB_WORKSPACE_SOURCE_ROOT") or "").strip()
        root = Path(root_value).expanduser() if root_value else None
        source = Path(source_value).expanduser() if source_value and Path(source_value).expanduser().is_absolute() else None
        return cls(root=root, source_root=source)

    @staticmethod
    def _absolute(path: Path) -> Path:
        expanded = path.expanduser()
        if not expanded.is_absolute():
            raise WorkspacePathError(f"Workspace roots must be absolute: {path}")
        return expanded.resolve(strict=False)

    @staticmethod
    def _relative_to(path: Path, root: Path) -> Path | None:
        try:
            return path.relative_to(root)
        except ValueError:
            return None

    def _under_runtime_root(self, relative: Path) -> Path:
        if self.root is None:
            raise WorkspacePathError("No workspace root is configured")
        candidate = (self.root / relative).resolve(strict=False)
        if self._relative_to(candidate, self.root) is None:
            raise WorkspacePathError(
                f"Project path escapes configured workspace root {self.root}: {relative}"
            )
        return candidate

    def runtime_path(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        if self.root is None:
            return path.resolve(strict=False)

        if path.is_absolute():
            resolved = path.resolve(strict=False)
            runtime_relative = self._relative_to(resolved, self.root)
            if runtime_relative is not None:
                return self._under_runtime_root(runtime_relative)
            if self.source_root is not None:
                source_relative = self._relative_to(resolved, self.source_root)
                if source_relative is not None:
                    return self._under_runtime_root(source_relative)
            raise WorkspacePathError(
                f"Absolute project path is outside configured workspace root {self.root}: {path}. "
                "Set CODEX_WEB_WORKSPACE_SOURCE_ROOT to migrate an existing native workspace path."
            )

        return self._under_runtime_root(path)

    def storage_path(self, value: str | Path) -> str:
        path = Path(value).expanduser()
        if self.root is None:
            return str(path.resolve(strict=False))

        runtime = self.runtime_path(path)
        relative = runtime.relative_to(self.root)
        return relative.as_posix() if relative.parts else "."

    def default_project_path(self) -> Path:
        return self.root if self.root is not None else Path.home().resolve(strict=False)
