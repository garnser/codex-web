from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "codex_web" / "runtime" / "legacy_core.py"
BUDGET = ROOT / "tests" / "test_core_budget.py"
WORKFLOW = ROOT / ".github" / "workflows" / "remove-dead-legacy-persistence-leftovers.yml"
SELF = Path(__file__)
TARGET = "_save_json_private"

source = LEGACY.read_text()
tree = ast.parse(source)
lines = source.splitlines(keepends=True)
node = next(
    (
        item
        for item in tree.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == TARGET
    ),
    None,
)
if node is None:
    raise SystemExit(f"missing target definition: {TARGET}")
del lines[node.lineno - 1 : node.end_lineno]
source = "".join(lines)

constant_line = "DEFAULT_THREAD_MESSAGE_LIMIT = 100\n"
if source.count(constant_line) != 1:
    raise SystemExit("expected exactly one DEFAULT_THREAD_MESSAGE_LIMIT assignment")
source = source.replace(constant_line, "", 1)

import_line = "    THREAD_INDEX_FILE,\n"
if source.count(import_line) != 1:
    raise SystemExit("expected exactly one THREAD_INDEX_FILE import")
source = source.replace(import_line, "", 1)
LEGACY.write_text(source)

budget = BUDGET.read_text()
new_size = LEGACY.stat().st_size
budget = re.sub(r"MAX_LEGACY_CORE_BYTES = \d+", f"MAX_LEGACY_CORE_BYTES = {new_size}", budget, count=1)
match = re.search(r"REMOVED_RUNTIME_DEFINITIONS = (\[[^\n]*\])", budget)
if not match:
    raise SystemExit("could not find REMOVED_RUNTIME_DEFINITIONS")
names = sorted(set(ast.literal_eval(match.group(1))) | {TARGET})
budget = budget[: match.start(1)] + repr(names) + budget[match.end(1) :]
BUDGET.write_text(budget)

WORKFLOW.unlink(missing_ok=True)
SELF.unlink(missing_ok=True)
print(f"legacy_core.py -> {new_size} bytes; removed dead persistence helper/constant/import")
