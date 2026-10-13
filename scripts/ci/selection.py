"""Conservative PR test selection; unknown or unreadable diffs run everything."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess


PROSE_FILES = frozenset({
    "README.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md",
    "LICENSE", "NOTICE", "TRADEMARKS.md", "docs/README.md", "docs/guide-template.md",
    ".github/PULL_REQUEST_TEMPLATE.md", ".github/ISSUE_TEMPLATE/bug_report.md",
    ".github/ISSUE_TEMPLATE/feature_request.md",
})
PROSE_DIRECTORIES = frozenset({
    "getting-started", "core-concepts", "tutorials", "how-to", "administration",
    "operations", "reference", "troubleshooting", "advanced-adoption", "examples",
    "extensions",
})


def is_prose(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if not path or "\\" in path or path.startswith("/") or any(
        part in {"", ".", ".."} for part in path.split("/")
    ):
        return False
    return path in PROSE_FILES or (
        len(parts) >= 3 and parts[0] == "docs"
        and parts[1] in PROSE_DIRECTORIES and path.endswith(".md")
    )


def parse_diff(raw: bytes) -> list[str]:
    """Read --name-status -z, checking both sides of renames and copies."""
    fields = raw.decode("utf-8", errors="strict").split("\0")
    if fields.pop() != "":
        raise ValueError("unterminated diff")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if re.fullmatch(r"[RC][0-9]{1,3}", status):
            count = 2
        elif status in {"A", "D", "M", "T", "U", "X", "B"}:
            count = 1
        else:
            raise ValueError("unknown diff status")
        if index + count > len(fields) or any(not p for p in fields[index:index + count]):
            raise ValueError("incomplete diff")
        # Symlink/type changes and unusual statuses are never prose-only.
        if status in {"T", "U", "X", "B"}:
            raise ValueError("non-prose change type")
        paths.extend(fields[index:index + count])
        index += count
    return paths


def select(event_name: str, paths: list[str] | None) -> dict[str, str]:
    if event_name != "pull_request":
        return {"full": "true", "reason": "Non-PR event: full validation required."}
    if not paths:
        return {"full": "true", "reason": "Missing or empty diff: full validation required."}
    if not all(is_prose(path) for path in paths):
        return {"full": "true", "reason": "Runtime, test, CI or unknown path changed: full validation required."}
    return {"full": "false", "reason": "Only allowlisted prose changed: focused documentation checks; runtime checks not applicable."}


def plan(event_name: str, event: dict) -> dict[str, str]:
    if event_name != "pull_request":
        return select(event_name, None)
    try:
        pr = event["pull_request"]
        base, head = pr["base"]["sha"], pr["head"]["sha"]
        if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (base, head)):
            raise ValueError("invalid revision")
        raw = subprocess.check_output([
            "git", "diff", "--name-status", "-z", "--find-renames",
            f"{base}...{head}", "--",
        ], stderr=subprocess.DEVNULL, timeout=30)
        result = select(event_name, parse_diff(raw))
        if result["full"] == "false":
            # Newly added symlinks also report status A, not T. Do not classify
            # executable or redirected documentation as inert prose.
            tree = subprocess.check_output([
                "git", "ls-tree", "-r", "-z", head, "--", *parse_diff(raw),
            ], stderr=subprocess.DEVNULL, timeout=30)
            if any(record.split(b" ", 1)[0] != b"100644"
                   for record in tree.split(b"\0") if record):
                raise ValueError("non-regular prose")
        result.update(base=base, head=head)
        return result
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
        return {"full": "true", "reason": "Diff unavailable or invalid: full validation required."}


def main() -> None:
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    except (KeyError, OSError, ValueError):
        event = {}
    result = plan(os.environ.get("GITHUB_EVENT_NAME", ""), event)
    tested = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    print(json.dumps(dict(result, tested_revision=tested)))
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        for key, value in result.items():
            output.write(f"{key}={value}\n")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
        summary.write(f"## PR validation selection\n\n{result['reason']}\n\n")
        summary.write(f"Tested revision: `{tested}`.\n\n")
        if "base" in result:
            summary.write(f"Complete PR diff: `{result['base']}...{result['head']}`.\n\n")
        summary.write("All required check names remain present. Main pushes always run full validation.\n")


if __name__ == "__main__":
    main()
