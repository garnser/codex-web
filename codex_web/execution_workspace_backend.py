from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


class ExecutionWorkspaceBackendError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GitWorkspaceProvision:
    path: Path
    branch_name: str
    base_revision: str
    head_revision: str


@runtime_checkable
class ExecutionWorkspaceBackend(Protocol):
    def provision_git(
        self,
        repository_path: Path,
        workspace_id: str,
        branch_name: str,
        base_revision: str | None,
    ) -> GitWorkspaceProvision: ...

    def cleanup_git(
        self,
        repository_path: Path,
        workspace_path: Path,
        branch_name: str,
        *,
        discard_branch: bool,
    ) -> None: ...

    def head_revision(self, workspace_path: Path) -> str: ...

    def disk_usage(self, workspace_path: Path) -> int: ...


class LocalGitWorkspaceBackend:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    @staticmethod
    def _git(*args: str, cwd: Path | None = None) -> str:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=str(cwd) if cwd else None,
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            raise ExecutionWorkspaceBackendError(str(detail).strip()) from exc
        return completed.stdout.strip()

    def provision_git(
        self,
        repository_path: Path,
        workspace_id: str,
        branch_name: str,
        base_revision: str | None,
    ) -> GitWorkspaceProvision:
        repository_path = repository_path.resolve()
        if not repository_path.is_dir():
            raise ExecutionWorkspaceBackendError("repository path does not exist")
        self._git("rev-parse", "--git-dir", cwd=repository_path)
        base = self._git(
            "rev-parse",
            "--verify",
            base_revision or "HEAD",
            cwd=repository_path,
        )
        target = (self.root / workspace_id).resolve()
        if target.exists():
            raise ExecutionWorkspaceBackendError("execution workspace path already exists")
        try:
            self._git(
                "worktree",
                "add",
                "-b",
                branch_name,
                str(target),
                base,
                cwd=repository_path,
            )
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise
        head = self._git("rev-parse", "HEAD", cwd=target)
        return GitWorkspaceProvision(
            path=target,
            branch_name=branch_name,
            base_revision=base,
            head_revision=head,
        )

    def cleanup_git(
        self,
        repository_path: Path,
        workspace_path: Path,
        branch_name: str,
        *,
        discard_branch: bool,
    ) -> None:
        repository_path = repository_path.resolve()
        workspace_path = workspace_path.resolve()
        try:
            self._git(
                "worktree",
                "remove",
                "--force",
                str(workspace_path),
                cwd=repository_path,
            )
        except ExecutionWorkspaceBackendError:
            shutil.rmtree(workspace_path, ignore_errors=True)
            with suppress_backend_error():
                self._git("worktree", "prune", cwd=repository_path)
        if discard_branch:
            with suppress_backend_error():
                self._git("branch", "-D", branch_name, cwd=repository_path)

    def head_revision(self, workspace_path: Path) -> str:
        return self._git("rev-parse", "HEAD", cwd=workspace_path.resolve())

    @staticmethod
    def disk_usage(workspace_path: Path) -> int:
        total = 0
        root = workspace_path.resolve()
        if not root.exists():
            return 0
        for path in root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
        return total


class suppress_backend_error:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is ExecutionWorkspaceBackendError
