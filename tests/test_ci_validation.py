from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.ci.check_gate import OWNERS, validate
from scripts.ci.run_unit_tests import selected_tests
from scripts.ci.selection import is_prose, parse_diff, plan, select


class TestSelectionTests(unittest.TestCase):
    def test_prose_only_and_mixed_changes(self):
        prose = ["README.md", "docs/how-to/work.md", ".github/PULL_REQUEST_TEMPLATE.md"]
        self.assertEqual(select("pull_request", prose)["full"], "false")
        for unsafe in [
            "AGENTS.md", "EXECUTIVE.md", "DOCKER.md", "docs/architecture/goals.md",
            "docs/screenshots/manifest.json", "docs/screenshots/README.md",
            "docs/assets/screenshots/generated/a.png", "docs/how-to/helper.py",
            "static/app.js", "tests/browser/a_fixture.html", "package-lock.json",
            "requirements.lock", ".github/workflows/tests.yml", "unknown.md",
            "docs/how-to/../architecture/goals.md", "/README.md", "docs\\how-to\\x.md",
        ]:
            with self.subTest(path=unsafe):
                self.assertFalse(is_prose(unsafe))
                self.assertEqual(select("pull_request", prose + [unsafe])["full"], "true")

    def test_push_and_missing_diff_always_use_full_validation(self):
        for event, paths in [("push", ["README.md"]), ("pull_request", []), ("pull_request", None), ("workflow_dispatch", [])]:
            self.assertEqual(select(event, paths)["full"], "true")

    def test_deleted_and_both_sides_of_renames_are_checked(self):
        paths = parse_diff(b"D\0docs/how-to/old.md\0R100\0static/app.js\0docs/how-to/new.md\0")
        self.assertEqual(paths, ["docs/how-to/old.md", "static/app.js", "docs/how-to/new.md"])
        self.assertEqual(select("pull_request", paths)["full"], "true")
        self.assertEqual(parse_diff(b"C100\0README.md\0CONTRIBUTING.md\0"), ["README.md", "CONTRIBUTING.md"])

    def test_malformed_and_type_changed_diffs_are_rejected(self):
        for raw in [b"M\0README.md", b"R100\0README.md\0", b"Z\0README.md\0", b"M\0\0", b"T\0README.md\0", b"M\0\xff\0"]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_diff(raw)

    def test_unavailable_or_invalid_revision_falls_back_to_full(self):
        event = {"pull_request": {"base": {"sha": "a" * 40}, "head": {"sha": "b" * 40}}}
        with patch("scripts.ci.selection.subprocess.check_output", side_effect=subprocess.CalledProcessError(1, "git")):
            self.assertEqual(plan("pull_request", event)["full"], "true")
        event["pull_request"]["head"]["sha"] = "--unsafe"
        self.assertEqual(plan("pull_request", event)["full"], "true")

    def test_complete_git_diff_includes_earlier_commits_and_added_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            def git(*args):
                return subprocess.check_output(["git", "-C", directory, *args], text=True).strip()
            git("init", "-q")
            git("config", "user.name", "CI test")
            git("config", "user.email", "ci@example.invalid")
            root = Path(directory)
            (root / "README.md").write_text("initial")
            git("add", ".")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD")
            (root / "server.py").write_text("runtime change")
            git("add", ".")
            git("commit", "-qm", "runtime")
            (root / "README.md").write_text("prose")
            git("add", ".")
            git("commit", "-qm", "prose")
            event = {"pull_request": {"base": {"sha": base}, "head": {"sha": git("rev-parse", "HEAD")}}}
            # Use a subprocess cwd rather than changing process-global cwd.
            original = subprocess.check_output
            def in_repository(args, **kwargs):
                return original(args, cwd=directory, **kwargs)
            with patch("scripts.ci.selection.subprocess.check_output", side_effect=in_repository):
                self.assertEqual(plan("pull_request", event)["full"], "true")
                event["pull_request"]["base"]["sha"] = git("rev-parse", "HEAD^")
                self.assertEqual(plan("pull_request", event)["full"], "false")
                (root / "CONTRIBUTING.md").symlink_to("README.md")
                git("add", ".")
                git("commit", "-qm", "symlink")
                event["pull_request"]["head"]["sha"] = git("rev-parse", "HEAD")
                self.assertEqual(plan("pull_request", event)["full"], "true")


class RequiredGateTests(unittest.TestCase):
    @staticmethod
    def needs(full):
        return {
            "changes": {"result": "success", "outputs": {"full": full}},
            "documentation": {"result": "skipped" if full == "true" else "success"},
            **{owner: {"result": "success" if full == "true" else "skipped"} for owner in set(OWNERS.values())},
        }

    def test_full_and_documentation_gates_are_explicit(self):
        for full in ["true", "false"]:
            for check in [*OWNERS, "chromium"]:
                message = validate(check, self.needs(full))
                self.assertIn("Full validation" if full == "true" else "not applicable", message)

    def test_failed_cancelled_missing_and_unexpected_skips_fail_every_gate(self):
        for full in ["true", "false"]:
            for job in self.needs(full):
                for result in ["failure", "cancelled", "missing", "skipped" if full == "true" else "success"]:
                    if result == self.needs(full)[job]["result"]:
                        continue
                    needs = deepcopy(self.needs(full))
                    if result == "missing":
                        del needs[job]
                    else:
                        needs[job]["result"] = result
                    with self.subTest(full=full, job=job, result=result), self.assertRaises(ValueError):
                        validate("unit-static", needs)

    def test_missing_selection_or_unknown_check_cannot_pass(self):
        for full in ["", "TRUE", None]:
            needs = self.needs("true")
            needs["changes"]["outputs"]["full"] = full
            with self.assertRaises(ValueError):
                validate("chromium", needs)
        with self.assertRaises(ValueError):
            validate("unknown", self.needs("true"))


class UnitOwnershipTests(unittest.TestCase):
    def test_nested_suites_keep_ordinary_tests_and_discovery_failures(self):
        regular = unittest.FunctionTestCase(lambda: None)
        dedicated_class = type("Dedicated", (unittest.TestCase,), {"__module__": "tests.test_gitlab_sync_scaling", "runTest": lambda self: None})
        dedicated = dedicated_class()
        suite = unittest.TestSuite([unittest.TestSuite([regular, dedicated])])
        self.assertEqual(list(selected_tests(suite)), [regular])


if __name__ == "__main__":
    unittest.main()
