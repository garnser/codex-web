from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "codex_web" / "runtime" / "legacy_core.py"
BUDGET = ROOT / "tests" / "test_core_budget.py"
WORKFLOW = ROOT / ".github" / "workflows" / "shrink-configuration-normalizers.yml"
SELF = Path(__file__)
TARGETS = {
    "_normalize_agent_channel_mapping",
    "_normalize_agent_channel_presence_project_settings",
    "_normalize_agent_channel_presence_settings",
    "_migrate_agent_channel_presence_settings",
    "_legacy_agent_channel_presence_from_gitlab_file",
    "_normalize_string_list",
    "_normalize_gitlab_project_settings",
    "_normalize_gitlab_routing_settings",
    "_migrate_gitlab_routing_settings",
}
source = LEGACY.read_text()
tree = ast.parse(source)
lines = source.splitlines(keepends=True)
ranges = [
    (node.lineno - 1, node.end_lineno, node.name)
    for node in tree.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in TARGETS
]
found = {name for _, _, name in ranges}
missing = TARGETS - found
if missing:
    raise SystemExit(f"missing target definitions: {sorted(missing)}")
for start, end, _ in sorted(ranges, reverse=True):
    del lines[start:end]
LEGACY.write_text("".join(lines))
budget = BUDGET.read_text()
new_size = LEGACY.stat().st_size
budget = re.sub(r"MAX_LEGACY_CORE_BYTES = \d+", f"MAX_LEGACY_CORE_BYTES = {new_size}", budget, count=1)
match = re.search(r"REMOVED_RUNTIME_DEFINITIONS = (\[[^\n]*\])", budget)
if not match:
    raise SystemExit("could not find REMOVED_RUNTIME_DEFINITIONS")
names = sorted(set(ast.literal_eval(match.group(1))) | TARGETS)
budget = budget[:match.start(1)] + repr(names) + budget[match.end(1):]
BUDGET.write_text(budget)
WORKFLOW.unlink(missing_ok=True)
SELF.unlink(missing_ok=True)
print(f"legacy_core.py -> {new_size} bytes; deleted {len(TARGETS)} definitions")
