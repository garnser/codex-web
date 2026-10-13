"""Required check wrappers reject missing, cancelled or failed selected jobs."""

from __future__ import annotations

import json
import os


OWNERS = {
    "unit-static": "unit",
    "gitlab-sync-qualification": "gitlab-sync-qualification",
    "explicit-repository-target-qualification": "explicit-repository-target-qualification",
    "distributed-backends": "distributed",
    "chromium-functional": "functional-shards",
    "chromium-visual": "browser-suite",
    "chromium-screenshots": "browser-suite",
    "docker-smoke": "docker",
    "rootless-podman-worker": "podman-worker",
}


def validate(check: str, needs: dict) -> str:
    selection = needs.get("changes", {})
    if selection.get("result") != "success":
        raise ValueError("Test selection did not succeed")
    full = selection.get("outputs", {}).get("full")
    if full not in {"true", "false"}:
        raise ValueError("Missing/invalid test selection")
    expected = "success" if full == "true" else "skipped"
    # Check every owner, not just this wrapper, so missing coverage cannot hide
    # behind an otherwise-green check or a permissive skipped dependency.
    for owner in set(OWNERS.values()):
        if needs.get(owner, {}).get("result") != expected:
            raise ValueError(f"{owner}: expected {expected}, got {needs.get(owner, {}).get('result', 'missing')}")
    docs_expected = "skipped" if full == "true" else "success"
    if needs.get("documentation", {}).get("result") != docs_expected:
        raise ValueError(f"Documentation checks: expected {docs_expected}")
    if check not in OWNERS and check != "chromium":
        raise ValueError("Unknown required check")
    if full == "true":
        return "Full validation passed; every selected test job succeeded."
    return "Runtime checks not applicable: allowlisted prose-only PR; focused documentation checks passed."


def main() -> None:
    message = validate(os.environ["CHECK_NAME"], json.loads(os.environ["NEEDS_JSON"]))
    print(message)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
        summary.write(f"## {os.environ['CHECK_NAME']}\n\n{message}\n")


if __name__ == "__main__":
    main()
