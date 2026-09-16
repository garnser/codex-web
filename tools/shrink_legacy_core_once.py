from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "codex_web" / "runtime" / "legacy_core.py"
BUDGET_TEST = ROOT / "tests" / "test_core_budget.py"

REMOVED = {
    "_archive_replaced_bot_thread",
    "_forget_approval_messages",
    "_latest_bot_detail",
    "_logical_binding_name",
    "_preferred_binding_for_replacement",
    "_raise_if_thread_replaced",
    "_record_bot_detail",
    "_remember_approval_message",
    "_replace_stale_bot_thread",
    "_replace_stale_web_thread",
    "_replacement_thread_id",
    "_retarget_active_turn",
    "_retarget_bot_details",
    "_retarget_bot_thread_state",
    "_retarget_logical_bot_bindings",
    "_retarget_slack_thread_icon",
    "_retarget_thread_settings",
    "_retarget_turn_queue",
    "_same_logical_binding",
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
        raise SystemExit(f"Expected extracted definitions were not found in legacy_core.py: {missing}")

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
    print(f"legacy_core.py shrunk to {new_size} bytes; removed {len(REMOVED)} extracted definitions")
