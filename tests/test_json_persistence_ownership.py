from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY_CORE_PATH = ROOT / "codex_web" / "runtime" / "legacy_core.py"
APPLICATION_PATH = ROOT / "codex_web" / "application.py"


class JsonPersistenceOwnershipTests(unittest.TestCase):
    def test_legacy_core_imports_storage_owned_primitives(self) -> None:
        tree = ast.parse(LEGACY_CORE_PATH.read_text())
        aliases: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "codex_web.storage.json_files":
                aliases.update({alias.name: alias.asname or alias.name for alias in node.names})
        self.assertEqual(aliases.get("state_file_lock"), "_state_file_lock")
        self.assertEqual(aliases.get("atomic_write_text"), "_atomic_write_text")

    def test_legacy_core_no_longer_owns_persistence_lock_state(self) -> None:
        tree = ast.parse(LEGACY_CORE_PATH.read_text())
        assigned = {
            node.target.id
            for node in tree.body
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        }
        self.assertNotIn("STATE_FILE_LOCKS", assigned)

    def test_application_does_not_rebind_persistence_primitives(self) -> None:
        source = APPLICATION_PATH.read_text()
        self.assertNotIn("core._state_file_lock =", source)
        self.assertNotIn("core._atomic_write_text =", source)


if __name__ == "__main__":
    unittest.main()
