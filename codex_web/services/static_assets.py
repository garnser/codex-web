from __future__ import annotations

import contextlib
import subprocess
import time
from collections.abc import Callable
from pathlib import Path


class StaticAssetVersionService:
    """Own deterministic cache-busting versions for UI static assets."""

    def __init__(
        self,
        static_dir: Path,
        *,
        repo_root: Path,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.static_dir = static_dir
        self.repo_root = repo_root
        self.clock = clock

    def version(self) -> str:
        mtimes = [
            path.stat().st_mtime
            for path in (
                self.static_dir / "index.html",
                self.static_dir / "app.js",
                self.static_dir / "styles.css",
            )
            if path.exists()
        ]
        mtime_version = str(
            int(max(mtimes) if mtimes else self.clock())
        )
        with contextlib.suppress(Exception):
            commit = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=self.repo_root,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            if commit:
                return f"{commit}-{mtime_version}"
        return mtime_version
