from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "codex_web" / "runtime" / "legacy_core.py"
BUDGET_TEST = ROOT / "tests" / "test_core_budget.py"

REMOVED = {
    "_reply_target_key",
    "_external_target_key",
    "_conversation_target_for_binding",
    "_remember_bot_reply_target",
    "_reply_target_for_binding",
    "_delivery_target_for_binding",
    "_active_reply_target_for_binding",
    "_active_reply_target_for_thread_provider",
    "_target_for_external_thread",
    "_remember_bot_delivery_target",
    "_master_reply_target_for_binding",
    "_thread_target_for_outbound",
    "_outbound_bindings_for_thread",
    "_forget_bot_reply_target",
    "_retarget_bot_targets",
}


def delete_extracted_definitions() -> int:
    source = LEGACY.read_text()
    lines = source.splitlines(keepends=True)
    tree = ast.parse(source)
    found: dict[str, tuple[int, int]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name not in REMOVED:
            continue
        start = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
        found[node.name] = (start, node.end_lineno)

    missing = sorted(REMOVED - found.keys())
    if missing:
        raise SystemExit(f"Expected BotTargetService definitions were not found in legacy_core.py: {missing}")

    drop: set[int] = set()
    for start, end in found.values():
        drop.update(range(start, end + 1))
    rewritten = "".join(line for number, line in enumerate(lines, start=1) if number not in drop)
    ast.parse(rewritten)
    LEGACY.write_text(rewritten)
    return len(rewritten.encode())


def tighten_budget(size: int) -> None:
    source = BUDGET_TEST.read_text()
    source, count = re.subn(
        r"^MAX_LEGACY_CORE_BYTES = \d+$",
        f"MAX_LEGACY_CORE_BYTES = {size}",
        source,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise SystemExit("Could not update MAX_LEGACY_CORE_BYTES")

    match = re.search(r"^REMOVED_RUNTIME_DEFINITIONS = (.+)$", source, flags=re.MULTILINE)
    if not match:
        raise SystemExit("Could not locate REMOVED_RUNTIME_DEFINITIONS")
    existing = ast.literal_eval(match.group(1))
    combined = sorted(set(existing) | REMOVED)
    source = source[: match.start(1)] + repr(combined) + source[match.end(1) :]
    BUDGET_TEST.write_text(source)


if __name__ == "__main__":
    new_size = delete_extracted_definitions()
    tighten_budget(new_size)
    print(f"legacy_core.py shrunk to {new_size} bytes; removed {len(REMOVED)} BotTargetService definitions")
