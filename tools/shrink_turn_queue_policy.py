from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPLICATION = ROOT / "codex_web" / "application.py"
LEGACY = ROOT / "codex_web" / "runtime" / "legacy_core.py"
BUDGET = ROOT / "tests" / "test_core_budget.py"
WORKFLOW = ROOT / ".github" / "workflows" / "shrink-turn-queue-policy.yml"
SELF = Path(__file__)
TARGETS = {
    "_thread_queue",
    "_thread_queue_depth",
    "_max_thread_queue_depth",
    "_steer_window_seconds",
    "_max_steers_per_window",
    "_record_thread_steer",
}

application = APPLICATION.read_text()
import_anchor = "from codex_web.services.turns import TurnService\n"
new_import = "from codex_web.services.turn_queue_policy import install_turn_queue_policy\n"
if new_import not in application:
    if application.count(import_anchor) != 1:
        raise SystemExit("could not find unique turn service import anchor")
    application = application.replace(import_anchor, new_import + import_anchor, 1)

install_anchor = "autonomy_service = install_autonomy_service(app, core)\nturn_service = TurnService(core)\n"
replacement = (
    "autonomy_service = install_autonomy_service(app, core)\n"
    "turn_queue_policy = install_turn_queue_policy(app, core)\n"
    "turn_service = TurnService(core)\n"
)
if "turn_queue_policy = install_turn_queue_policy(app, core)" not in application:
    if application.count(install_anchor) != 1:
        raise SystemExit("could not find unique turn service composition anchor")
    application = application.replace(install_anchor, replacement, 1)
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
budget = budget[: match.start(1)] + repr(names) + budget[match.end(1) :]
BUDGET.write_text(budget)

WORKFLOW.unlink(missing_ok=True)
SELF.unlink(missing_ok=True)
print(f"legacy_core.py -> {new_size} bytes; extracted {len(TARGETS)} turn queue policy helpers")
