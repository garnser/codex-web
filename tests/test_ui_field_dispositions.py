from __future__ import annotations

import importlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "administration" / "ui-field-dispositions.json"


class UiFieldDispositionParityTests(unittest.TestCase):
    def test_declared_models_have_complete_non_overlapping_field_dispositions(self) -> None:
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
        allowed = set(payload["dispositions"])

        for model_name, declaration in payload["models"].items():
            module = importlib.import_module(declaration["module"])
            model = getattr(module, model_name)
            actual = set(model.model_fields)

            declared: list[str] = []
            for disposition, fields in declaration["groups"].items():
                self.assertIn(
                    disposition,
                    allowed,
                    f"{model_name} uses unknown UI disposition {disposition}",
                )
                declared.extend(fields)

            self.assertEqual(
                len(declared),
                len(set(declared)),
                f"{model_name} declares the same field in multiple UI dispositions",
            )
            self.assertEqual(
                actual,
                set(declared),
                (
                    f"{model_name} backend/UI parity changed. "
                    "Every added/removed field must receive an explicit UI disposition "
                    "in docs/administration/ui-field-dispositions.json."
                ),
            )

    def test_manifest_covers_first_class_configuration_resources(self) -> None:
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertGreaterEqual(
            set(payload["models"]),
            {
                "ConfigurationSpec",
                "ConfigurationRecord",
                "AgentProfileRevision",
                "AgentTeamRevision",
                "SkillDefinition",
            },
        )


    def test_capability_matrix_documents_api_only_exceptions(self) -> None:
        matrix = (
            ROOT / "docs" / "administration" / "configuration-capability-matrix.md"
        ).read_text(encoding="utf-8")

        self.assertIn("## Declared API-only exceptions", matrix)
        self.assertIn("Authentication policy and session-policy create/update", matrix)
        self.assertIn("Provider/runtime versioned lifecycle and archive/restore", matrix)
        self.assertIn("Entitlement/quota create, update and retirement", matrix)
        self.assertIn(
            "Recovery/operational policy create, update, archive and retirement",
            matrix,
        )
        self.assertIn(
            "Any new `API` cell added to the matrix must be accompanied by an entry here",
            matrix,
        )


if __name__ == "__main__":
    unittest.main()
