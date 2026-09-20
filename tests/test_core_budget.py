from __future__ import annotations

import ast
import unittest
from pathlib import Path


RUNTIME_DIR = (
    Path(__file__).resolve().parents[1]
    / "codex_web"
    / "runtime"
)
CORE_PATH = RUNTIME_DIR / "core.py"
LEGACY_CORE_PATH = RUNTIME_DIR / "legacy_core.py"
MAX_CORE_BYTES = 4096


class CoreBudgetTests(unittest.TestCase):
    def test_core_is_only_a_thin_compatibility_namespace(self) -> None:
        size = CORE_PATH.stat().st_size
        self.assertLessEqual(
            size,
            MAX_CORE_BYTES,
            (
                f"core.py grew to {size} bytes; budget is "
                f"{MAX_CORE_BYTES}. Runtime behavior belongs in "
                "composed services, not the compatibility namespace."
            ),
        )
        source = CORE_PATH.read_text()
        tree = ast.parse(source)
        top_level_defs = [
            node.name
            for node in tree.body
            if isinstance(
                node,
                (
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                    ast.ClassDef,
                ),
            )
        ]
        self.assertEqual(
            top_level_defs,
            [],
            (
                "core.py must remain definition-free: "
                f"{top_level_defs}"
            ),
        )
        self.assertNotIn("legacy_core", source)

    def test_production_has_no_legacy_core_references(self) -> None:
        production_root = RUNTIME_DIR.parent
        references = []
        for path in production_root.rglob("*.py"):
            if "legacy_core" in path.read_text():
                references.append(str(path.relative_to(production_root)))
        self.assertEqual(
            references,
            [],
            f"legacy_core references remain in production: {references}",
        )

    def test_application_owns_app_and_registers_routes_directly(self) -> None:
        source = (
            RUNTIME_DIR.parent / "application.py"
        ).read_text()
        self.assertNotIn("app = core.app", source)
        self.assertNotIn("replace_routes(", source)

    def test_legacy_core_is_deleted(self) -> None:
        self.assertFalse(
            LEGACY_CORE_PATH.exists(),
            "legacy_core.py must not return after final runtime cutover",
        )


if __name__ == "__main__":
    unittest.main()
