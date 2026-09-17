from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "codex_web" / "runtime" / "legacy_core.py"
APPLICATION = ROOT / "codex_web" / "application.py"
BUDGET = ROOT / "tests" / "test_core_budget.py"
OWNERSHIP_TEST = ROOT / "tests" / "test_json_persistence_ownership.py"
WORKFLOW = ROOT / ".github" / "workflows" / "shrink-json-persistence-primitives.yml"
SELF = Path(__file__)
TARGETS = {"_state_file_lock", "_atomic_write_text"}
CANONICAL_IMPORT = """from codex_web.storage.json_files import (\n    atomic_write_text as _atomic_write_text,\n    state_file_lock as _state_file_lock,\n)\n"""


def _delete_legacy_ownership(source: str) -> str:
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    ranges: list[tuple[int, int, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in TARGETS:
            ranges.append((node.lineno - 1, node.end_lineno, node.name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "STATE_FILE_LOCKS":
            ranges.append((node.lineno - 1, node.end_lineno, "STATE_FILE_LOCKS"))

    expected = TARGETS | {"STATE_FILE_LOCKS"}
    found = {name for _, _, name in ranges}
    missing = expected - found
    if missing:
        raise SystemExit(f"missing legacy persistence ownership: {sorted(missing)}")

    for start, end, _ in sorted(ranges, reverse=True):
        del lines[start:end]
    source = "".join(lines)

    anchor = """from codex_web.providers import (\n    get_json as _get_json,\n    post_slack_message as _post_slack_message,\n    post_telegram_message as _post_telegram_message,\n    slack_socket_url as _slack_socket_url,\n    update_slack_message as _update_slack_message,\n)\n"""
    if CANONICAL_IMPORT not in source:
        if anchor not in source:
            raise SystemExit("could not find providers import anchor")
        source = source.replace(anchor, anchor + CANONICAL_IMPORT, 1)

    parsed = ast.parse(source)
    used_names = {node.id for node in ast.walk(parsed) if isinstance(node, ast.Name)}
    if "tempfile" not in used_names:
        source = source.replace("import tempfile\n", "", 1)
    if "threading" not in used_names:
        source = source.replace("import threading\n", "", 1)

    source = re.sub(r"\n{5,}", "\n\n\n", source)
    return source


def _remove_application_rebinding(source: str) -> str:
    source = source.replace("from codex_web.storage.json_files import atomic_write_text, state_file_lock\n", "", 1)
    block = """# Shared persistence primitives are owned outside the legacy runtime. Existing\n# unextracted state helpers resolve these globals at call time, so they use the\n# same atomic implementation without maintaining a second persistence path.\ncore._state_file_lock = state_file_lock\ncore._atomic_write_text = atomic_write_text\n\n"""
    if block not in source:
        raise SystemExit("could not find application persistence rebinding block")
    return source.replace(block, "", 1)


def _write_ownership_test() -> None:
    OWNERSHIP_TEST.write_text(
        '''from __future__ import annotations\n\nimport ast\nimport unittest\nfrom pathlib import Path\n\nROOT = Path(__file__).resolve().parents[1]\nLEGACY_CORE_PATH = ROOT / "codex_web" / "runtime" / "legacy_core.py"\nAPPLICATION_PATH = ROOT / "codex_web" / "application.py"\n\n\nclass JsonPersistenceOwnershipTests(unittest.TestCase):\n    def test_legacy_core_imports_storage_owned_primitives(self) -> None:\n        tree = ast.parse(LEGACY_CORE_PATH.read_text())\n        aliases: dict[str, str] = {}\n        for node in tree.body:\n            if isinstance(node, ast.ImportFrom) and node.module == "codex_web.storage.json_files":\n                aliases.update({alias.name: alias.asname or alias.name for alias in node.names})\n        self.assertEqual(aliases.get("state_file_lock"), "_state_file_lock")\n        self.assertEqual(aliases.get("atomic_write_text"), "_atomic_write_text")\n\n    def test_legacy_core_no_longer_owns_persistence_lock_state(self) -> None:\n        tree = ast.parse(LEGACY_CORE_PATH.read_text())\n        assigned = {\n            node.target.id\n            for node in tree.body\n            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)\n        }\n        self.assertNotIn("STATE_FILE_LOCKS", assigned)\n\n    def test_application_does_not_rebind_persistence_primitives(self) -> None:\n        source = APPLICATION_PATH.read_text()\n        self.assertNotIn("core._state_file_lock =", source)\n        self.assertNotIn("core._atomic_write_text =", source)\n\n\nif __name__ == "__main__":\n    unittest.main()\n'''
    )


legacy = _delete_legacy_ownership(LEGACY.read_text())
LEGACY.write_text(legacy)
APPLICATION.write_text(_remove_application_rebinding(APPLICATION.read_text()))
_write_ownership_test()

budget = BUDGET.read_text()
new_size = LEGACY.stat().st_size
budget = re.sub(r"MAX_LEGACY_CORE_BYTES = \d+", f"MAX_LEGACY_CORE_BYTES = {new_size}", budget, count=1)
match = re.search(r"REMOVED_RUNTIME_DEFINITIONS = (\[[^\n]*\])", budget)
if not match:
    raise SystemExit("could not find REMOVED_RUNTIME_DEFINITIONS")
names = sorted(set(ast.literal_eval(match.group(1))) | TARGETS)
budget = budget[:match.start(1)] + repr(names) + budget[match.end(1):]
BUDGET.write_text(budget)

# Validate the generated ownership shape before committing it.
legacy_tree = ast.parse(LEGACY.read_text())
legacy_defs = {
    node.name
    for node in legacy_tree.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
}
returned = TARGETS & legacy_defs
if returned:
    raise SystemExit(f"persistence definitions still present: {sorted(returned)}")
if "core._state_file_lock =" in APPLICATION.read_text() or "core._atomic_write_text =" in APPLICATION.read_text():
    raise SystemExit("application still rebinds persistence primitives")

WORKFLOW.unlink(missing_ok=True)
SELF.unlink(missing_ok=True)
print(f"legacy_core.py -> {new_size} bytes; canonicalized {sorted(TARGETS)}")
