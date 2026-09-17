from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LEGACY_CORE = ROOT / "codex_web" / "runtime" / "legacy_core.py"
APPLICATION = ROOT / "codex_web" / "application.py"
CORE_BUDGET = ROOT / "tests" / "test_core_budget.py"
WORKFLOW = ROOT / ".github" / "workflows" / "shrink-watchdog-dispatch.yml"
SCRIPT = Path(__file__).resolve()

REMOVED_DEFINITIONS = {
    "_watchdog_dispatch_cooldown_seconds",
    "_watchdog_dispatch_allowed",
    "_record_watchdog_dispatch",
}


def remove_legacy_ownership() -> int:
    text = LEGACY_CORE.read_text()
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)
    ranges: list[tuple[int, int]] = []
    found: set[str] = set()

    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in REMOVED_DEFINITIONS:
            continue
        if node.end_lineno is None:
            raise RuntimeError(f"AST node has no end line: {node.name}")
        found.add(node.name)
        start = node.lineno - 1
        end = node.end_lineno
        while end < len(lines) and not lines[end].strip():
            end += 1
        ranges.append((start, end))

    missing = REMOVED_DEFINITIONS - found
    if missing:
        raise RuntimeError(f"Legacy watchdog dispatch definitions not found: {sorted(missing)}")

    for start, end in sorted(ranges, reverse=True):
        del lines[start:end]

    updated = "".join(lines)
    LEGACY_CORE.write_text(updated)
    return len(updated.encode())


def update_application() -> None:
    text = APPLICATION.read_text()
    import_line = (
        "from codex_web.services.watchdog_dispatch import install_watchdog_dispatch_policy\n"
    )
    import_marker = "from codex_web.services.autonomy import install_autonomy_service\n"
    if import_line not in text:
        if text.count(import_marker) != 1:
            raise RuntimeError("Unable to locate autonomy import marker")
        text = text.replace(import_marker, import_marker + import_line)

    install_line = "watchdog_dispatch_policy = install_watchdog_dispatch_policy(app, core)\n"
    install_marker = "work_item_timing_policy = install_work_item_timing_policy(app, core)\n"
    if install_line not in text:
        if text.count(install_marker) != 1:
            raise RuntimeError("Unable to locate work item timing composition marker")
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

    WORKFLOW.unlink()
    SCRIPT.unlink()


if __name__ == "__main__":
    main()
