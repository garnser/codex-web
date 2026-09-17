from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPLICATION = ROOT / "codex_web" / "application.py"
LEGACY = ROOT / "codex_web" / "runtime" / "legacy_core.py"
BUDGET = ROOT / "tests" / "test_core_budget.py"
WORKFLOW = ROOT / ".github" / "workflows" / "shrink-thread-index-helpers.yml"
SELF = Path(__file__)
TARGETS = {"_upsert_indexed_thread", "_remove_indexed_thread"}

application = APPLICATION.read_text()
application = application.replace(
    "    STATE_DB_FILE,\n    THREAD_SETTINGS_FILE,",
    "    STATE_DB_FILE,\n    THREAD_INDEX_FILE,\n    THREAD_SETTINGS_FILE,",
    1,
)
application = application.replace(
    "from codex_web.storage.sqlite_state import SQLiteStateStore\n",
    "from codex_web.storage.sqlite_state import SQLiteStateStore\n"
    "from codex_web.storage.thread_index import install_thread_index_repository\n",
    1,
)
runtime_state_block = """runtime_state = RuntimeStateRepositories(
    state_store,
    thread_settings_file=THREAD_SETTINGS_FILE,
    active_turns_file=ACTIVE_TURNS_FILE,
    work_item_states_file=WORK_ITEM_STATES_FILE,
)
"""
if runtime_state_block not in application:
    raise SystemExit("could not find runtime-state composition block")
application = application.replace(
    runtime_state_block,
    runtime_state_block
    + "thread_index_repository = install_thread_index_repository(\n"
    + "    app,\n"
    + "    core,\n"
    + "    store=state_store,\n"
    + "    legacy_path=THREAD_INDEX_FILE,\n"
    + ")\n",
    1,
)
if application.count("install_thread_index_repository(") != 1:
    raise SystemExit("thread index repository composition was not inserted exactly once")
APPLICATION.write_text(application)

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
print(f"legacy_core.py -> {new_size} bytes; installed thread index repository and deleted {len(TARGETS)} helpers")
