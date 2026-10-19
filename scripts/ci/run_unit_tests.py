"""Broad unit coverage excluding modules owned by required qualification jobs."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


QUALIFICATION_MODULES = frozenset({
    "test_gitlab_sync_scaling",
    "test_explicit_repository_target_qualification",
})


def selected_tests(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from selected_tests(test)
        elif type(test).__module__.removeprefix("tests.") not in QUALIFICATION_MODULES:
            yield test


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    if (root / "data").is_symlink():
        raise SystemExit("Refusing to run tests through a data symlink; use a runner-local data directory.")
    # Application storage is rooted beside this runner's repository, not in the
    # caller's cwd. Bind discovery and relative fixture paths to the same root.
    os.chdir(root)
    sys.path.insert(0, str(root))
    discovered = unittest.defaultTestLoader.discover("tests", pattern="test_*.py", top_level_dir=str(root))
    suite = unittest.TestSuite(selected_tests(discovered))
    if not suite.countTestCases():
        raise SystemExit("No unit tests selected")
    print(f"Selected {suite.countTestCases()} unit tests; dedicated qualification modules run separately.", flush=True)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
