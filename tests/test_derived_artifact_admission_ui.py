import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DerivedArtifactAdmissionUiTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required for UI contract tests')
    def test_request_and_admin_contract(self):
        run = subprocess.run([shutil.which('node'), '--test', 'tests/js/derived_artifact_request.test.mjs'],
            cwd=ROOT, capture_output=True, text=True, timeout=20, check=False)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_ui_uses_canonical_endpoint_confirmation_and_scope_fences_with_existing_budgets(self):
        admin = ROOT / 'static/artifact_evidence_admin.js'
        ui = ROOT / 'static/derived_artifact_admission.js'
        request = ROOT / 'static/derived_artifact_request.js'
        source = ui.read_text()
        self.assertLessEqual(admin.stat().st_size, 20000)
        self.assertLessEqual(ui.stat().st_size, 4000)
        self.assertLessEqual(request.stat().st_size, 3000)
        presenters = ROOT / 'static/artifact_evidence_presenters.js'
        self.assertLessEqual(presenters.stat().st_size, 4000)
        self.assertIn('listText(record.source_record_ids)', presenters.read_text())
        self.assertIn('/static/derived_artifact_admission.js', admin.read_text())
        self.assertIn("request('/api/artifacts/derived'", source)
        self.assertIn('confirmAction', source)
        self.assertIn('scope.current()', source)
        self.assertIn("referenceLink('artifact'", source)
        self.assertIn('status.textContent', source)
        self.assertNotIn('fetch(', source)
        self.assertNotIn('localStorage', source)
        self.assertNotIn('JSON.stringify(result', source)
