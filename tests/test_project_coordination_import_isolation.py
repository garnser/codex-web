"""Shared coordination fixtures must not compose application state at import."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class ProjectCoordinationImportIsolationTests(unittest.TestCase):
    def test_fixture_import_denies_application_composition(self):
        code = """
import importlib.abc
import sys
class DenyApplication(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ('codex_web.application', 'codex_web.server', 'server'):
            raise ImportError('fixture must not import ' + fullname)
sys.meta_path.insert(0, DenyApplication())
import tests.test_project_coordination
assert 'codex_web.application' not in sys.modules
"""
        env = {key: value for key, value in os.environ.items()
               if 'DSN' not in key and key not in
               ('CODEX_WEB_STATE_BACKEND', 'DATABASE_URL')}
        result = subprocess.run(
            [sys.executable, '-c', code],
            cwd=Path(__file__).resolve().parents[1], env=env,
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
