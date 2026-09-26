from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
INVENTORY = ROOT / "docs" / "ui" / "legacy-migration-inventory.md"


class UiMigrationInventoryTests(unittest.TestCase):
    def test_inventory_exists_and_tracks_compatibility_surfaces(self):
        text = INVENTORY.read_text(encoding="utf-8")
        for marker in (
            "static/styles.css",
            "canonical Project switcher",
            "#threads",
            ".developer-card",
            "static/work_items_ui.js",
            "static/agent_provider_admin.js",
            "static/workspace_components.js",
        ):
            self.assertIn(marker, text)

    def test_static_tree_contains_no_obvious_backup_or_copy_artifacts(self):
        offenders = []
        backup_suffixes = (".bak", ".old", ".orig", ".rej", ".tmp", "~")
        copy_pattern = re.compile(r"(?:^|[-_.])(copy|backup|obsolete)(?:[-_.]|$)", re.I)
        for path in STATIC.rglob("*"):
            if not path.is_file():
                continue
            name = path.name
            if name.endswith(backup_suffixes) or copy_pattern.search(name):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(
            offenders,
            [],
            "Obvious orphan/backup UI artifacts must be removed rather than kept beside the active implementation",
        )


if __name__ == "__main__":
    unittest.main()
