"""Required check wrappers reject missing, cancelled or failed selected jobs."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


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


def expected_functional_shard_names() -> frozenset[str]:
    """Read the shipped workflow's literal matrix; unsupported evolution fails closed."""
    try:
        workflow = (Path(__file__).resolve().parents[2] / '.github/workflows/tests.yml').read_text()
    except (OSError, UnicodeError) as exc:
        raise ValueError('Canonical CI protocol is missing or unreadable; inspect the packaged workflow metadata') from exc
    job = re.search(r'^  functional-shards:\n(.*?)(?=^  [a-zA-Z][\w-]*:|\Z)', workflow, re.M | re.S)
    strategy_matrix = re.search(r'^      matrix:\n(.*?)(?=^    steps:|\Z)', job.group(1) if job else '', re.M | re.S)
    matrix = re.fullmatch(r'        shard:\n((?:          - [0-9]+\n)+)',
                          strategy_matrix.group(1) if strategy_matrix else '')
    if not matrix:
        raise ValueError('Canonical functional shard matrix is missing or unsupported; inspect packaged CI protocol compatibility')
    shards = re.findall(r'          - ([0-9]+)', matrix.group(1))
    if len(shards) != len(set(shards)) or not shards:
        raise ValueError('Canonical functional shard matrix is ambiguous')
    return frozenset(f'chromium-functional-shard-{shard}' for shard in shards)


def validate(check: str, needs: dict, *, selection_only: bool = False) -> str:
    selection = needs.get("changes", {})
    if selection.get("result") != "success":
        raise ValueError("Test selection did not succeed")
    full = selection.get("outputs", {}).get("full")
    if full not in {"true", "false"}:
        raise ValueError("Missing/invalid test selection")
    docs_expected = "skipped" if full == "true" else "success"
    if needs.get("documentation", {}).get("result") != docs_expected:
        raise ValueError(f"Documentation checks: expected {docs_expected}")
    if check not in OWNERS and check != "chromium":
        raise ValueError("Unknown required check")
    if selection_only:
        return (
            "Full validation selected; this job's runtime checks must pass."
            if full == "true" else
            "Runtime checks not applicable: allowlisted prose-only PR; focused documentation checks passed."
        )
    owners = set(OWNERS.values()) if check == "chromium" else {OWNERS[check]}
    for owner in owners:
        expected = "skipped" if full == "false" and owner == "functional-shards" else "success"
        if needs.get(owner, {}).get("result") != expected:
            raise ValueError(f"{owner}: expected {expected}, got {needs.get(owner, {}).get('result', 'missing')}")
    if full == "true":
        return "Full validation passed; every selected test job succeeded."
    return "Runtime checks not applicable: allowlisted prose-only PR; focused documentation checks passed."


def main() -> None:
    message = validate(
        os.environ["CHECK_NAME"], json.loads(os.environ["NEEDS_JSON"]),
        selection_only=os.environ.get("SELECTION_ONLY") == "true",
    )
    print(message)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
        summary.write(f"## {os.environ['CHECK_NAME']}\n\n{message}\n")


if __name__ == "__main__":
    main()
