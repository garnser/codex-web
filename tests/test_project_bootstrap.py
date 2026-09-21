from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from codex_web.cli import (
    EXIT_APPLY_FAILED,
    EXIT_BLOCKED,
    EXIT_READY,
    ProjectBootstrapBlocked,
    main,
)
from codex_web.project_bootstrap import (
    ProjectBootstrapManifestError,
    dump_project_bootstrap_yaml,
    parse_project_bootstrap_manifest,
    resolve_bootstrap_repository_paths,
    scaffold_project_bootstrap_manifest,
)
from codex_web.workspaces import WorkspaceMapper


class ProjectBootstrapManifestTests(unittest.TestCase):
    def _example(self, repository: str = "/workspace/veridataops/saas-app"):
        return {
            "apiVersion": "codex-web/v1",
            "kind": "ProjectBootstrap",
            "project": {
                "name": "Veridataops",
                "organization": "veridataops",
                "workspace": "engineering",
            },
            "repositories": [
                {
                    "id": "saas-app",
                    "path": repository,
                    "default": True,
                }
            ],
            "taskSource": {
                "type": "gitlab",
                "authoritative": True,
                "secretRef": "gitlab-primary",
            },
            "execution": {
                "repositorySelection": "single",
                "requiredCapabilities": ["command_execution"],
                "sandbox": "workspace-write",
            },
            "integrations": {
                "slack": {
                    "connectionRef": "slack-primary",
                    "backfill": {"enabled": False},
                }
            },
        }

    def test_documented_manifest_validates_without_credentials(self):
        manifest = parse_project_bootstrap_manifest(self._example())
        self.assertEqual(manifest.api_version, "codex-web/v1")
        self.assertEqual(manifest.task_source.secret_ref, "gitlab-primary")
        self.assertEqual(
            manifest.execution.required_capabilities[0].value,
            "command_execution",
        )
        self.assertNotIn("token", dump_project_bootstrap_yaml(manifest).casefold())

    def test_raw_credential_field_is_rejected_without_value_leak(self):
        payload = self._example()
        payload["taskSource"]["token"] = "ULTRA_PRIVATE_VALUE"
        with self.assertRaises(ProjectBootstrapManifestError) as raised:
            parse_project_bootstrap_manifest(payload)
        self.assertNotIn("ULTRA_PRIVATE_VALUE", str(raised.exception))
        self.assertIn("raw credential material", str(raised.exception))

    def test_unsupported_manifest_version_fails_before_runtime_work(self):
        payload = self._example()
        payload["apiVersion"] = "codex-web/v999"
        with self.assertRaises(ProjectBootstrapManifestError) as raised:
            parse_project_bootstrap_manifest(payload)
        self.assertIn("apiVersion", str(raised.exception))

    def test_duplicate_repository_ids_are_rejected(self):
        payload = self._example()
        payload["repositories"].append(
            {
                "id": "SAAS-APP",
                "path": "/workspace/veridataops/other",
            }
        )
        with self.assertRaises(ProjectBootstrapManifestError) as raised:
            parse_project_bootstrap_manifest(payload)
        self.assertIn("repository IDs must be unique", str(raised.exception))

    def test_workspace_validation_rejects_escape_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "workspace"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / ".git").mkdir()
            manifest = parse_project_bootstrap_manifest(
                self._example(str(outside))
            )
            mapper = WorkspaceMapper(root=root)
            with self.assertRaises(ProjectBootstrapManifestError):
                resolve_bootstrap_repository_paths(
                    manifest,
                    workspace_mapper=mapper,
                )

            link = root / "linked"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("filesystem does not permit symlink creation")
            linked = parse_project_bootstrap_manifest(
                self._example(str(link))
            )
            with self.assertRaises(ProjectBootstrapManifestError):
                resolve_bootstrap_repository_paths(
                    linked,
                    workspace_mapper=mapper,
                )

    def test_scaffold_uses_safe_single_repository_defaults(self):
        manifest = scaffold_project_bootstrap_manifest(
            project_name="Starter",
            organization="local",
            workspace="default",
            repository_path="/workspace/starter",
        )
        self.assertEqual(manifest.execution.repository_selection, "single")
        self.assertEqual(manifest.execution.sandbox, "workspace-write")
        self.assertTrue(manifest.repositories[0].default)


class ProjectBootstrapCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "workspace"
        self.repo = self.root / "repo"
        self.repo.mkdir(parents=True)
        (self.repo / ".git").mkdir()
        self.manifest = Path(self.temp.name) / "project.yaml"
        model = scaffold_project_bootstrap_manifest(
            project_name="Demo",
            organization="local",
            workspace="default",
            repository_path=str(self.repo),
        )
        self.manifest.write_text(
            dump_project_bootstrap_yaml(model),
            encoding="utf-8",
        )
        self.old_root = os.environ.get("CODEX_WEB_WORKSPACE_ROOT")
        os.environ["CODEX_WEB_WORKSPACE_ROOT"] = str(self.root)
        self.addCleanup(self._restore_root)

    def _restore_root(self):
        if self.old_root is None:
            os.environ.pop("CODEX_WEB_WORKSPACE_ROOT", None)
        else:
            os.environ["CODEX_WEB_WORKSPACE_ROOT"] = self.old_root

    def _run(self, argv, *, runner):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(argv, apply_runner=runner)
        return code, output.getvalue()

    def test_dry_run_never_calls_apply_runner(self):
        called = []

        def forbidden(**_kwargs):
            called.append(True)
            raise AssertionError("dry-run mutated state")

        code, output = self._run(
            [
                "bootstrap",
                "--project",
                "home",
                "--manifest",
                str(self.manifest),
                "--migrate-legacy",
                "--dry-run",
                "--output",
                "json",
            ],
            runner=forbidden,
        )
        self.assertEqual(code, EXIT_READY)
        self.assertEqual(called, [])
        self.assertIn('"phase":"manifest-validation"', output)
        self.assertIn("no persistent state", output)

    def test_apply_uses_stable_runner_result_and_machine_output(self):
        calls = []

        def runner(**kwargs):
            calls.append(kwargs["manifest"].digest())
            return {
                "status": "ready",
                "bootstrapExecutionId": "materialization-abc",
                "materializationPlanId": "materialization-plan-abc",
                "counts": {"migrated": 2},
                "warnings": [],
                "blockers": [],
            }

        argv = [
            "bootstrap",
            "--project",
            "home",
            "--manifest",
            str(self.manifest),
            "--migrate-legacy",
            "--apply",
            "--output",
            "json",
        ]
        first, first_output = self._run(argv, runner=runner)
        second, second_output = self._run(argv, runner=runner)
        self.assertEqual(first, EXIT_READY)
        self.assertEqual(second, EXIT_READY)
        self.assertEqual(calls[0], calls[1])
        self.assertIn('"bootstrapExecutionId":"materialization-abc"', first_output)
        self.assertEqual(first_output, second_output)

    def test_blocked_apply_has_typed_code(self):
        def runner(**_kwargs):
            raise ProjectBootstrapBlocked(
                "repository_topology_mismatch",
                "explicit reconciliation is required",
            )

        code, output = self._run(
            [
                "bootstrap",
                "--project",
                "home",
                "--manifest",
                str(self.manifest),
                "--migrate-legacy",
                "--apply",
                "--output",
                "json",
            ],
            runner=runner,
        )
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertIn("repository_topology_mismatch", output)

    def test_apply_failure_does_not_echo_exception_secret(self):
        def runner(**_kwargs):
            raise RuntimeError("request token ULTRA_PRIVATE_VALUE failed")

        code, output = self._run(
            [
                "bootstrap",
                "--project",
                "home",
                "--manifest",
                str(self.manifest),
                "--migrate-legacy",
                "--apply",
                "--output",
                "json",
            ],
            runner=runner,
        )
        self.assertEqual(code, EXIT_APPLY_FAILED)
        self.assertNotIn("ULTRA_PRIVATE_VALUE", output)
        self.assertIn("RuntimeError", output)

    def test_scaffold_writes_manifest_and_refuses_overwrite(self):
        target = Path(self.temp.name) / "generated.yaml"
        code, _output = self._run(
            [
                "bootstrap",
                "--project",
                "starter",
                "--manifest",
                str(target),
                "--scaffold",
                "--repository",
                str(self.repo),
                "--project-name",
                "Starter",
            ],
            runner=lambda **_kwargs: {},
        )
        self.assertEqual(code, EXIT_READY)
        self.assertTrue(target.exists())
        parsed = parse_project_bootstrap_manifest(
            __import__("yaml").safe_load(target.read_text())
        )
        self.assertEqual(parsed.project.name, "Starter")


if __name__ == "__main__":
    unittest.main()
