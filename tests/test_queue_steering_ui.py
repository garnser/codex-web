"""Run dependency-free UI contract regressions in the existing unit gate."""

from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "Node is required for UI contract tests")
class QueueSteeringUiTests(unittest.TestCase):
    def test_reconciliation_contract(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which("node"), "--test", "tests/js/queue_steering.test.mjs"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
