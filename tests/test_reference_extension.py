from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from codex_web.extension_builder import build_extension_package
from codex_web.extension_packages import LocalExtensionPackageCatalog
from codex_web.services.task_sources import TaskSource


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "extensions" / "reference_task_source"


class ReferenceExtensionTests(unittest.TestCase):
    def test_sample_implements_task_source_contract(self) -> None:
        module_path = SAMPLE / "reference_task_source.py"
        spec = importlib.util.spec_from_file_location("reference_task_source_sample", module_path)
        self.assertIsNotNone(spec)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = module.create()
        self.assertIsInstance(source, TaskSource)

    def test_sample_builds_and_round_trips_through_catalog(self) -> None:
        manifest = json.loads((SAMPLE / "manifest.template.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "reference"
            built = build_extension_package(
                manifest,
                SAMPLE / "reference_task_source.py",
                package,
            )
            discovery = LocalExtensionPackageCatalog(Path(temp)).discover()

        self.assertEqual(discovery.errors, ())
        self.assertEqual(len(discovery.candidates), 1)
        self.assertEqual(discovery.candidates[0].manifest, built.manifest)
        self.assertTrue(discovery.candidates[0].verification.digest_verified)


if __name__ == "__main__":
    unittest.main()
