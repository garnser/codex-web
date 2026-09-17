from __future__ import annotations

import ast
import re
from pathlib import Path

TARGETS = {"recovery_resume", "telegram_webhook"}
LEGACY_PATH = Path("codex_web/runtime/legacy_core.py")
BUDGET_PATH = Path("tests/test_core_budget.py")
SELF_PATH = Path("scripts/shrink_final_legacy_routes.py")
WORKFLOW_PATH = Path(".github/workflows/shrink-final-legacy-routes.yml")


def remove_top_level_definitions(text: str) -> str:
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    ranges: list[tuple[int, int]] = []
    found: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name not in TARGETS:
            continue
        start = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)]) - 1
        end = node.end_lineno or node.lineno
        ranges.append((start, end))
        found.add(node.name)
    missing = TARGETS - found
    if missing:
        raise SystemExit(f"missing target definitions: {sorted(missing)}")
    for start, end in sorted(ranges, reverse=True):
        del lines[start:end]
    return "".join(lines)


def update_budget(text: str, size: int) -> str:
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
        updated = sorted(set(current) | TARGETS)
        newline = "\n" if line.endswith("\n") else ""
        lines[index] = f"REMOVED_RUNTIME_DEFINITIONS = {updated!r}{newline}"
        break
    else:
        raise SystemExit("could not update REMOVED_RUNTIME_DEFINITIONS")
    return "".join(lines)


def main() -> None:
    legacy = remove_top_level_definitions(LEGACY_PATH.read_text(encoding="utf-8"))
    LEGACY_PATH.write_text(legacy, encoding="utf-8")
    budget = update_budget(BUDGET_PATH.read_text(encoding="utf-8"), len(legacy.encode("utf-8")))
    BUDGET_PATH.write_text(budget, encoding="utf-8")
    SELF_PATH.unlink()
    WORKFLOW_PATH.unlink()


if __name__ == "__main__":
    main()
