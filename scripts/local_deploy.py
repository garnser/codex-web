#!/usr/bin/env python3
"""Install an exact origin/main build into the dedicated local live checkout.

This helper is intentionally narrow: it only operates on the two explicitly
provided codex-web checkouts and only restarts codex-web.service.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def run(*args: str, check: bool = True, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(args), flush=True)
    return subprocess.run(args, check=check, text=True, capture_output=True, timeout=timeout)


def git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run("git", "-C", str(path), *args, check=check)


def revision(path: Path, value: str = "HEAD") -> str:
    return git(path, "rev-parse", "--verify", value).stdout.strip()


def version_matches(observed: str, expected: str) -> bool:
    """Accept an unambiguous abbreviated SHA emitted by static assets."""
    normalized = observed.strip().lower()
    return (
        len(normalized) >= 7
        and all(character in "0123456789abcdef" for character in normalized)
        and expected.lower().startswith(normalized)
    )


def health(expected: str) -> bool:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8765/api/livez", timeout=3) as response:
                live = response.status == 200
            with urllib.request.urlopen("http://127.0.0.1:8765/api/readyz", timeout=3) as response:
                ready = response.status == 200
            with urllib.request.urlopen("http://127.0.0.1:8765/api/version", timeout=3) as response:
                version = json.load(response)
            observed = str(version.get("gitRevision") or version.get("git_revision") or "")
            if observed in {"", "unknown"}:
                observed = str(version.get("staticVersion") or "").split("-", 1)[0]
            if live and ready and version_matches(observed, expected):
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def checkout(live: Path, commit: str) -> None:
    git(live, "checkout", "--detach", commit)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--live", type=Path, required=True)
    args = parser.parse_args()
    if len(args.candidate) != 40 or any(c not in "0123456789abcdef" for c in args.candidate):
        raise SystemExit("candidate must be an exact lowercase SHA")
    for path in (args.source, args.live):
        if not (path / ".git").exists():
            raise SystemExit(f"not a Git worktree: {path}")

    lock_path = Path("/home/nbingester/codex-web-native-runtime/local-deploy.lock")
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = revision(args.live)
        print(json.dumps({"event": "start", "candidate": args.candidate, "previous": previous}), flush=True)
        git(args.source, "fetch", "--prune", "origin", "main")
        available = revision(args.source, "origin/main")
        if args.candidate != available:
            raise SystemExit(f"candidate is not current origin/main: {args.candidate} != {available}")
        git(args.source, "merge-base", "--is-ancestor", args.candidate, "origin/main")
        dirty = [line for line in git(args.live, "status", "--porcelain").stdout.splitlines() if ".venv" not in line]
        if dirty:
            raise SystemExit("live checkout has unexpected changes: " + "; ".join(dirty[:10]))

        built = args.candidate
        with tempfile.TemporaryDirectory(prefix="codex-web-deploy-") as temp:
            stage = Path(temp) / "stage"
            git(args.source, "worktree", "add", "--detach", str(stage), args.candidate)
            try:
                # Preserve local runtime integration patches while rebasing their
                # patch-equivalent changes onto the newly fetched main commit.
                overlay_rows = git(args.source, "cherry", args.candidate, previous).stdout.splitlines()
                for row in overlay_rows:
                    if row.startswith("+ "):
                        git(stage, "cherry-pick", row[2:].strip())
                # Run from the staged tree, not from the helper's live checkout.
                subprocess.run(
                    [sys.executable, "-m", "compileall", "-q", "codex_web", "server.py"],
                    cwd=stage,
                    check=True,
                    timeout=300,
                )
                built = revision(stage)
                checkout(args.live, built)
            finally:
                git(args.source, "worktree", "remove", "--force", str(stage), check=False)

        run("sudo", "-n", "systemctl", "restart", "codex-web.service", timeout=120)
        if health(built):
            print(json.dumps({"event": "complete", "candidate": args.candidate, "installed": built}), flush=True)
            return 0

        print(json.dumps({"event": "rollback", "target": previous}), flush=True)
        checkout(args.live, previous)
        run("sudo", "-n", "systemctl", "restart", "codex-web.service", timeout=120)
        if not health(previous):
            raise SystemExit("deployment failed and rollback health verification failed")
        raise SystemExit("deployment failed; previous build restored")


if __name__ == "__main__":
    raise SystemExit(main())
