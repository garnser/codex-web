from __future__ import annotations

import ast
import re
from pathlib import Path

CANDIDATES = {
    "_gitlab_event_target_agents",
    "_gitlab_routing_enabled_for_project",
    "_gitlab_routing_agents",
    "_gitlab_routing_bindings_for_agent",
    "_gitlab_routing_bindings_for_master",
    "_gitlab_event_id",
    "_remember_gitlab_event",
    "_support_servicedesk_project_paths",
    "_support_servicedesk_owner_agent",
    "_support_servicedesk_project_matches",
    "_support_servicedesk_ticket_key",
    "_is_support_servicedesk_ticket_payload",
    "_remember_support_servicedesk_ticket",
    "_support_servicedesk_ticket_seen",
    "_format_support_servicedesk_prompt",
    "_dispatch_support_servicedesk_ticket",
    "_gitlab_api_base_url",
    "_gitlab_api_token",
    "_support_servicedesk_sweep_project",
    "_support_servicedesk_sweep_interval",
    "_support_servicedesk_sweep_lookback_hours",
    "_issue_to_support_servicedesk_payload",
    "_run_support_servicedesk_sweep_once",
    "_support_servicedesk_sweep_loop",
    "_gitlab_semantic_dedupe_seconds",
    "_gitlab_semantic_key_for_state",
    "_gitlab_semantic_key",
    "_remember_gitlab_semantic_key",
    "_remember_gitlab_semantic_issue_state",
    "_gitlab_label_names",
    "_gitlab_owner_agents",
    "_gitlab_project_path_matches",
    "_gitlab_group_path",
    "_gitlab_token_for_project",
    "_gitlab_project_settings_for_payload",
    "_gitlab_reference",
    "_gitlab_url",
}
LEGACY_PATH = Path("codex_web/runtime/legacy_core.py")
BUDGET_PATH = Path("tests/test_core_budget.py")
SELF_PATH = Path("scripts/shrink_legacy_gitlab_service.py")
WORKFLOW_PATH = Path(".github/workflows/shrink-legacy-gitlab-service.yml")


def remove_owned_definitions(text: str) -> tuple[str, set[str]]:
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    ranges: list[tuple[int, int]] = []
    found: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name not in CANDIDATES:
            continue
        start = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)]) - 1
        end = node.end_lineno or node.lineno
        ranges.append((start, end))
        found.add(node.name)
    if len(found) < 10:
        raise SystemExit(f"expected a substantial GitLab duplicate set, found only {sorted(found)}")
    for start, end in sorted(ranges, reverse=True):
        del lines[start:end]
    result = "".join(lines)
    remaining = {
        node.name
        for node in ast.parse(result).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in found
    }
    if remaining:
        raise SystemExit(f"definitions remained after shrink: {sorted(remaining)}")
    return result, found


def update_budget(text: str, size: int, removed: set[str]) -> str:
    text, count = re.subn(
        r"^MAX_LEGACY_CORE_BYTES = \d+$",
        f"MAX_LEGACY_CORE_BYTES = {size}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise SystemExit("could not update MAX_LEGACY_CORE_BYTES")
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not line.startswith("REMOVED_RUNTIME_DEFINITIONS = "):
            continue
        current = ast.literal_eval(line.split("=", 1)[1].strip())
        updated = sorted(set(current) | removed)
        newline = "\n" if line.endswith("\n") else ""
        lines[index] = f"REMOVED_RUNTIME_DEFINITIONS = {updated!r}{newline}"
        break
    else:
        raise SystemExit("could not update REMOVED_RUNTIME_DEFINITIONS")
    return "".join(lines)


def main() -> None:
    legacy, removed = remove_owned_definitions(LEGACY_PATH.read_text(encoding="utf-8"))
    LEGACY_PATH.write_text(legacy, encoding="utf-8")
    budget = update_budget(BUDGET_PATH.read_text(encoding="utf-8"), len(legacy.encode("utf-8")), removed)
    BUDGET_PATH.write_text(budget, encoding="utf-8")
    print(f"removed {len(removed)} GitLab-owned definitions: {', '.join(sorted(removed))}")
    print(f"new legacy_core.py size: {len(legacy.encode('utf-8'))} bytes")
    SELF_PATH.unlink()
    WORKFLOW_PATH.unlink()


if __name__ == "__main__":
    main()
