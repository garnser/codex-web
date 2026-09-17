from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "codex_web" / "runtime" / "legacy_core.py"
APPLICATION = ROOT / "codex_web" / "application.py"
BUDGET = ROOT / "tests" / "test_core_budget.py"
WORKFLOW = ROOT / ".github" / "workflows" / "shrink-thread-execution-settings.yml"
SELF = Path(__file__)
TARGETS = {
    "_remember_thread_run_settings",
    "_thread_run_settings",
    "_codex_web_internal_base_url",
    "_work_item_contract_binding",
    "_work_item_contract_instructions",
    "_effective_developer_instructions",
    "_base_developer_instructions",
    "_sync_bot_binding_settings",
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

application = APPLICATION.read_text()
import_anchor = "from codex_web.services.threads import ThreadService\n"
import_line = "from codex_web.services.thread_execution_settings import install_thread_execution_settings_service\n"
if import_line not in application:
    if import_anchor not in application:
        raise SystemExit("thread service import anchor not found")
    application = application.replace(import_anchor, import_line + import_anchor, 1)

compose_anchor = "codex_runtime = install_codex_runtime(app, core)\n"
compose_block = compose_anchor + "thread_execution_settings_service = install_thread_execution_settings_service(app, core)\n"
if "thread_execution_settings_service = install_thread_execution_settings_service(app, core)" not in application:
    if compose_anchor not in application:
        raise SystemExit("codex runtime composition anchor not found")
    application = application.replace(compose_anchor, compose_block, 1)
APPLICATION.write_text(application)

budget = BUDGET.read_text()
new_size = LEGACY.stat().st_size
budget = re.sub(
    r"MAX_LEGACY_CORE_BYTES = \d+",
    f"MAX_LEGACY_CORE_BYTES = {new_size}",
    budget,
    count=1,
)
match = re.search(r"REMOVED_RUNTIME_DEFINITIONS = (\[[^\n]*\])", budget)
if not match:
    raise SystemExit("could not find REMOVED_RUNTIME_DEFINITIONS")
names = sorted(set(ast.literal_eval(match.group(1))) | TARGETS)
budget = budget[:match.start(1)] + repr(names) + budget[match.end(1):]
BUDGET.write_text(budget)

WORKFLOW.unlink(missing_ok=True)
SELF.unlink(missing_ok=True)
print(f"legacy_core.py -> {new_size} bytes; deleted {len(TARGETS)} thread execution setting helpers")
