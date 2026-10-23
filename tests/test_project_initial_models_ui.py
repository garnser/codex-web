"""Dependency-free production refresh regression in the canonical unit gate."""
from pathlib import Path
import shutil
import subprocess
import unittest

@unittest.skipUnless(shutil.which("node"), "Node is required for UI contracts")
class ProjectInitialModelsUiTests(unittest.TestCase):
    def test_initialization(self):
        root=Path(__file__).resolve().parents[1]
        result=subprocess.run([shutil.which("node"),"--test","tests/js/project_initial_models.test.mjs"],cwd=root,capture_output=True,text=True,timeout=20,check=False)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
