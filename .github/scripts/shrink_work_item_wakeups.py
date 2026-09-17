from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LEGACY_CORE = ROOT / "codex_web" / "runtime" / "legacy_core.py"
APPLICATION = ROOT / "codex_web" / "application.py"
CORE_BUDGET = ROOT / "tests" / "test_core_budget.py"
WORKFLOW = ROOT / ".github" / "workflows" / "shrink-work-item-wakeups.yml"
SCRIPT = Path(__file__).resolve()

REMOVED_DEFINITIONS = {
    "_work_item_wakeup_entries",
    "_render_work_item_wakeup_batch",
    "_coalesce_queued_work_item_wakeups",
    "_compact_turn_queues",
}
REMOVED_CONSTANT = "WORK_ITEM_WAKEUP_BATCH_HEADER"


def _assigned_names(node: ast.AST) -> set[str]:
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]

    result: set[str] = set()
    for target in targets:
        if isinstance(target, ast.Name):
            result.add(target.id)
    return result


def remove_legacy_ownership() -> int:
    text = LEGACY_CORE.read_text()
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)
    ranges: list[tuple[int, int]] = []
    found_defs: set[str] = set()
    found_constant = False

    for node in tree.body:
        remove = False
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in REMOVED_DEFINITIONS:
            found_defs.add(node.name)
            remove = True
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and REMOVED_CONSTANT in _assigned_names(node):
            found_constant = True
            remove = True

        if not remove:
            continue
        if node.end_lineno is None:
            raise RuntimeError(f"AST node has no end line: {node!r}")
        start = node.lineno - 1
        end = node.end_lineno
        while end < len(lines) and not lines[end].strip():
            end += 1
        ranges.append((start, end))

    missing_defs = REMOVED_DEFINITIONS - found_defs
    if missing_defs:
        raise RuntimeError(f"Legacy wakeup definitions not found exactly as expected: {sorted(missing_defs)}")
    if not found_constant:
        raise RuntimeError(f"Legacy wakeup constant not found: {REMOVED_CONSTANT}")

    for start, end in sorted(ranges, reverse=True):
        del lines[start:end]

    updated = "".join(lines)
    LEGACY_CORE.write_text(updated)
    return len(updated.encode())


def update_application() -> None:
    text = APPLICATION.read_text()
    import_line = (
        "from codex_web.services.work_item_wakeups import "
        "install_work_item_wakeup_queue_policy\n"
    )
    import_marker = "from codex_web.services.work_item_timing import install_work_item_timing_policy\n"
    if import_line not in text:
        if text.count(import_marker) != 1:
            raise RuntimeError("Unable to locate work-item timing import marker")
        text = text.replace(import_marker, import_marker + import_line)

    install_line = (
        "work_item_wakeup_queue_policy = "
        "install_work_item_wakeup_queue_policy(app, core)\n"
    )
    install_marker = "turn_queue_policy = install_turn_queue_policy(app, core)\n"
    if install_line not in text:
        if text.count(install_marker) != 1:
            raise RuntimeError("Unable to locate turn queue policy install marker")
        text = text.replace(install_marker, install_marker + install_line)

    APPLICATION.write_text(text)


def update_core_budget(new_size: int) -> None:
    text = CORE_BUDGET.read_text()
    text, count = re.subn(
        r"^MAX_LEGACY_CORE_BYTES = \d+$",
        f"MAX_LEGACY_CORE_BYTES = {new_size}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise RuntimeError("Unable to update legacy-core byte budget")

    match = re.search(r"^REMOVED_RUNTIME_DEFINITIONS = (\[.*\])$", text, re.MULTILINE)
    if not match:
        raise RuntimeError("Unable to locate removed-runtime definition ratchet")
    names = ast.literal_eval(match.group(1))
    if not isinstance(names, list):
        raise RuntimeError("Removed-runtime definition ratchet is not a list")
    updated_names = sorted(set(str(name) for name in names) | REMOVED_DEFINITIONS)
    text = text[: match.start(1)] + repr(updated_names) + text[match.end(1) :]
    CORE_BUDGET.write_text(text)


def main() -> None:
    new_size = remove_legacy_ownership()
    update_application()
    update_core_budget(new_size)

    # One-shot migration infrastructure must not survive the generated commit.
    WORKFLOW.unlink()
    SCRIPT.unlink()


if __name__ == "__main__":
    main()
