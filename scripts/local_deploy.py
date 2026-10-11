#!/usr/bin/env python3
"""Install an exact origin/main build into the dedicated local live checkout.

This helper is intentionally narrow: it only operates on the two explicitly
provided codex-web checkouts and only restarts codex-web.service.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.parse
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


def maintenance_plan(qualification: dict[str, object]) -> dict[str, object]:
    plan_id = urllib.parse.quote(str(qualification["upgrade_plan_id"]), safe="")
    with urllib.request.urlopen("http://127.0.0.1:8765/api/upgrades/" + plan_id, timeout=3) as response:
        plan = json.load(response)["item"]
    if not (
        plan.get("id") == qualification["upgrade_plan_id"]
        and plan.get("maintenance_mode") is True
        and plan.get("maintenance_scope") == "service"
        and plan.get("service_instance_id") == qualification["service_instance_id"]
        and plan.get("deployment_source_revision") == qualification["source_revision"]
        and plan.get("deployment_target_revision") == qualification["target_revision"]
    ):
        raise RuntimeError("installation maintenance plan no longer matches qualification")
    return plan


def health(expected: str, qualification: dict[str, object], *, rollback: bool = False) -> bool:
    """Verify installation under the still-active canonical maintenance plan.

    Readiness is deliberately false until the operator verifies the Upgrade
    Plan and restores worker/autonomy controls. It is not an install gate.
    """
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8765/api/livez", timeout=3) as response:
                live = response.status == 200 and json.load(response).get("ok") is True
            plan = maintenance_plan(qualification)
            with urllib.request.urlopen("http://127.0.0.1:8765/api/version", timeout=3) as response:
                version = json.load(response)
            observed = str(version.get("gitRevision") or version.get("git_revision") or "")
            if observed in {"", "unknown"}:
                observed = str(version.get("staticVersion") or "").split("-", 1)[0]
            field = "source_revision" if rollback else "target_revision"
            if (
                live and version_matches(observed, expected)
                and qualification[field] == expected
                and (not rollback or plan.get("rollback_available") is True)
            ):
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def checkout(live: Path, commit: str) -> None:
    git(live, "checkout", "--detach", commit)


def set_service_revision(revision_value: str) -> None:
    """Keep the fixed service's build metadata aligned with its checkout."""
    if len(revision_value) != 40 or any(c not in "0123456789abcdef" for c in revision_value):
        raise ValueError("service revision must be an exact lowercase SHA")
    with tempfile.NamedTemporaryFile(mode="w", prefix="codex-web-revision-", encoding="utf-8") as staged:
        staged.write("[Service]\nEnvironment=CODEX_WEB_GIT_REVISION=" + revision_value + "\n")
        staged.flush()
        run("sudo", "-n", "install", "-m", "0644", staged.name,
            "/etc/systemd/system/codex-web.service.d/zz-local-deploy-revision.conf")
    run("sudo", "-n", "systemctl", "daemon-reload")


def verify_canonical_guard(fd: int) -> dict[str, object]:
    """Perform the one-shot canonical recheck immediately before switching."""
    channel = socket.socket(fileno=fd)
    try:
        channel.settimeout(30)
        channel.sendall(b"verify\n")
        chunks = bytearray()
        while b"\n" not in chunks and len(chunks) < 65536:
            chunk = channel.recv(4096)
            if not chunk:
                break
            chunks.extend(chunk)
        response = json.loads(bytes(chunks).split(b"\n", 1)[0] or b"{}")
    finally:
        channel.close()
    if response.get("ok") is not True:
        raise RuntimeError(
            "canonical deployment guard denied the switch: "
            + str(response.get("error") or "no verified response")
        )
    qualification = response.get("qualification")
    if not isinstance(qualification, dict):
        raise RuntimeError("canonical deployment guard returned no qualification")
    return qualification


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--live", type=Path, required=True)
    parser.add_argument("--expected-live", required=True)
    parser.add_argument("--guard-fd", type=int, required=True)
    args = parser.parse_args()
    if len(args.candidate) != 40 or any(c not in "0123456789abcdef" for c in args.candidate):
        raise SystemExit("candidate must be an exact lowercase SHA")
    if len(args.expected_live) != 40 or any(
        c not in "0123456789abcdef" for c in args.expected_live
    ):
        raise SystemExit("expected-live must be an exact lowercase SHA")
    for path in (args.source, args.live):
        if not (path / ".git").exists():
            raise SystemExit(f"not a Git worktree: {path}")

    lock_path = Path("/home/nbingester/codex-web-native-runtime/local-deploy.lock")
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = revision(args.live)
        if previous != args.expected_live:
            raise SystemExit(
                f"live revision changed before deployment: {previous} != {args.expected_live}"
            )
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
                if built != args.candidate:
                    raise SystemExit("local overlays changed the qualified immutable candidate")
                if revision(args.source, "origin/main") != args.candidate:
                    raise SystemExit("candidate changed while deployment was staged")
                if revision(args.live) != previous:
                    raise SystemExit("live revision changed while deployment was staged")
                qualification = verify_canonical_guard(args.guard_fd)
                print(
                    json.dumps(
                        {
                            "event": "qualified",
                            "upgrade_plan_id": qualification.get("upgrade_plan_id"),
                            "preflight_evidence_id": qualification.get(
                                "preflight_evidence_id"
                            ),
                            "service_instance_id": qualification.get(
                                "service_instance_id"
                            ),
                        }
                    ),
                    flush=True,
                )
                checkout(args.live, built)
            finally:
                git(args.source, "worktree", "remove", "--force", str(stage), check=False)

        installed = False
        try:
            set_service_revision(built)
            run("sudo", "-n", "systemctl", "restart", "codex-web.service", timeout=120)
            installed = health(built, qualification)
        except Exception as exc:
            print(json.dumps({"event": "installation_error", "error_type": type(exc).__name__}), flush=True)
        if installed:
            print(json.dumps({"event": "complete", "candidate": args.candidate, "installed": built}), flush=True)
            return 0

        if qualification.get("rollback_available") is not True:
            raise SystemExit("installation verification failed; canonical plan does not permit automatic rollback; maintenance retained")
        try:
            rollback_plan = maintenance_plan(qualification)
        except Exception as exc:
            raise SystemExit("cannot qualify rollback; maintenance retained") from exc
        if rollback_plan.get("rollback_available") is not True:
            raise SystemExit("canonical rollback permission changed; maintenance retained")
        print(json.dumps({"event": "rollback", "target": previous}), flush=True)
        checkout(args.live, previous)
        set_service_revision(previous)
        run("sudo", "-n", "systemctl", "restart", "codex-web.service", timeout=120)
        if not health(previous, qualification, rollback=True):
            raise SystemExit("deployment failed and rollback health verification failed")
        raise SystemExit("deployment failed; previous build restored")


if __name__ == "__main__":
    raise SystemExit(main())
