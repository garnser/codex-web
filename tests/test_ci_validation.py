from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
import sys
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
            **{owner: {"result": "skipped" if full == "false" and owner == "functional-shards" else "success"} for owner in set(OWNERS.values())},
        }

    def test_full_and_documentation_gates_are_explicit(self):
        for full in ["true", "false"]:
            for check in [*OWNERS, "chromium"]:
                message = validate(check, self.needs(full))
                self.assertIn("Full validation" if full == "true" else "not applicable", message)

    def test_failed_cancelled_missing_and_unexpected_skips_fail_every_gate(self):
        for full in ["true", "false"]:
            for job in self.needs(full):
                for result in ["failure", "cancelled", "missing", "skipped", "success"]:
                    if result == self.needs(full)[job]["result"]:
                        continue
                    needs = deepcopy(self.needs(full))
                    if result == "missing":
                        del needs[job]
                    else:
                        needs[job]["result"] = result
                    with self.subTest(full=full, job=job, result=result), self.assertRaises(ValueError):
                        validate("chromium", needs)

    def test_named_jobs_require_valid_selection_and_documentation_before_testing(self):
        for full in ["true", "false"]:
            needs = self.needs(full)
            prerequisites = {key: needs[key] for key in ["changes", "documentation"]}
            self.assertIn("selected" if full == "true" else "not applicable", validate("unit-static", prerequisites, selection_only=True))
            for key in prerequisites:
                bad = deepcopy(prerequisites)
                bad[key]["result"] = "cancelled"
                with self.assertRaises(ValueError):
                    validate("unit-static", bad, selection_only=True)

    def test_functional_aggregate_requires_successful_shards_only_when_applicable(self):
        for full in ["true", "false"]:
            needs = self.needs(full)
            minimal = {key: needs[key] for key in ["changes", "documentation", "functional-shards"]}
            validate("chromium-functional", minimal)
            minimal["functional-shards"]["result"] = "failure"
            with self.assertRaises(ValueError):
                validate("chromium-functional", minimal)

    def test_missing_selection_or_unknown_check_cannot_pass(self):
        for full in ["", "TRUE", None]:
            needs = self.needs("true")
            needs["changes"]["outputs"]["full"] = full
            with self.assertRaises(ValueError):
                validate("chromium", needs)
        with self.assertRaises(ValueError):
            validate("unknown", self.needs("true"))


class UnitOwnershipTests(unittest.TestCase):
    @staticmethod
    def copy_runner(root):
        runner = Path(__file__).resolve().parents[1] / "scripts/ci/run_unit_tests.py"
        destination = root / "scripts/ci/run_unit_tests.py"
        destination.parent.mkdir(parents=True)
        shutil.copyfile(runner, destination)
        return destination

    def test_runner_rejects_shared_data_symlink_before_importing_application(self):
        for existing in [False, True]:
            for foreign_cwd in [False, True]:
                with self.subTest(existing=existing, foreign_cwd=foreign_cwd), tempfile.TemporaryDirectory() as directory:
                    base = Path(directory)
                    root, caller = base / "repository", base / "caller"
                    runner = self.copy_runner(root)
                    caller.mkdir()
                    shared = base / "shared-data"
                    if existing:
                        shared.mkdir()
                        (shared / "protected").write_text("unchanged")
                    (root / "data").symlink_to(shared, target_is_directory=True)
                    for location in [root, caller]:
                        (location / "tests").mkdir()
                        (location / "tests/test_probe.py").write_text(
                            "from pathlib import Path\n"
                            "Path('imported-probe').write_text('unsafe import')\n"
                        )
                    result = subprocess.run(
                        [sys.executable, str(runner)], cwd=caller if foreign_cwd else root,
                        capture_output=True, text=True, timeout=10,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("runner-local data directory", result.stderr)
                    self.assertFalse((root / "imported-probe").exists())
                    self.assertFalse((caller / "imported-probe").exists())
                    self.assertEqual(shared.exists(), existing)
                    if existing:
                        self.assertEqual([p.name for p in shared.iterdir()], ["protected"])
                        self.assertEqual((shared / "protected").read_text(), "unchanged")

    def test_runner_discovers_own_tests_and_relative_paths_from_any_cwd(self):
        for foreign_cwd in [False, True]:
            with self.subTest(foreign_cwd=foreign_cwd), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root, caller = base / "repository", base / "caller"
                runner = self.copy_runner(root)
                (root / "data").mkdir()
                (root / "tests").mkdir()
                (root / "tests/__init__.py").write_text("")
                (caller / "tests").mkdir(parents=True)
                (caller / "tests/test_foreign.py").write_text("raise RuntimeError('foreign tests imported')\n")
                (root / "tests/test_local.py").write_text(
                    "import unittest\nfrom pathlib import Path\n"
                    "class Local(unittest.TestCase):\n"
                    " def test_relative_storage(self):\n"
                    "  self.assertTrue(Path('scripts/ci/run_unit_tests.py').is_file())\n"
                    "  Path('data/local-observation').write_text('local')\n"
                )
                # This intentionally failing case is owned by its dedicated job.
                (root / "tests/test_gitlab_sync_scaling.py").write_text(
                    "import unittest\nclass Dedicated(unittest.TestCase):\n"
                    " def test_not_in_broad_suite(self):\n"
                    "  self.fail('dedicated module executed')\n"
                )
                result = subprocess.run(
                    [sys.executable, str(runner)], cwd=caller if foreign_cwd else root,
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Selected 1 unit tests", result.stdout)
                self.assertEqual((root / "data/local-observation").read_text(), "local")
                self.assertFalse((caller / "data").exists())

    def test_runner_keeps_discovery_failures_from_its_own_repository(self):
        for module in ("test_broken", "test_gitlab_sync_scaling"):
            with self.subTest(module=module), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root, caller = base / "repository", base / "caller"
                runner = self.copy_runner(root)
                (root / "data").mkdir()
                (root / "tests").mkdir()
                (root / "tests/__init__.py").write_text("")
                caller.mkdir()
                (root / "tests" / (module + ".py")).write_text("raise RuntimeError('local discovery failure')\n")
                result = subprocess.run(
                    [sys.executable, str(runner)], cwd=caller,
                    capture_output=True, text=True, timeout=10,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("local discovery failure", result.stderr)
                self.assertIn("tests." + module, result.stderr)
                self.assertIn("Selected 1 unit tests", result.stdout)

    def test_nested_suites_keep_ordinary_tests_and_discovery_failures(self):
        regular = unittest.FunctionTestCase(lambda: None)
        dedicated_class = type("Dedicated", (unittest.TestCase,), {"__module__": "tests.test_gitlab_sync_scaling", "runTest": lambda self: None})
        dedicated = dedicated_class()
        suite = unittest.TestSuite([unittest.TestSuite([regular, dedicated])])
        self.assertEqual(list(selected_tests(suite)), [regular])

    def test_canonical_discovery_retains_inherited_environments_and_named_skips(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = self.copy_runner(root)
            (root / "data").mkdir()
            (root / "tests").mkdir()
            (root / "tests/__init__.py").write_text("")
            (root / "tests/test_owner.py").write_text(
                "import unittest\nfrom pathlib import Path\n"
                "class Contract(unittest.TestCase):\n"
                " def test_once(self):\n"
                "  with Path('data/executions').open('a') as out: out.write(type(self).__name__+'\\n')\n"
                " @unittest.skip('isolated backend unavailable')\n"
                " def test_optional(self): pass\n"
                "class OtherEnvironment(Contract): pass\n"
            )
            (root / "tests/test_consumer.py").write_text(
                "import sys, unittest\nfrom tests import test_owner as fixture\n"
                "class Consumer(unittest.TestCase):\n"
                " def test_identity(self):\n"
                "  self.assertIs(fixture,sys.modules['tests.test_owner'])\n"
                "  self.assertNotIn('test_owner',sys.modules)\n"
            )
            result = subprocess.run([sys.executable, str(runner)], cwd=root,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Selected 5 unit tests", result.stdout)
            self.assertEqual((root / "data/executions").read_text().splitlines(),
                             ["Contract", "OtherEnvironment"])
            for environment in ("Contract", "OtherEnvironment"):
                self.assertIn(f"tests.test_owner.{environment}.test_optional", result.stderr)
            self.assertEqual(result.stderr.count("skipped 'isolated backend unavailable'"), 2)


if __name__ == "__main__":
    unittest.main()
