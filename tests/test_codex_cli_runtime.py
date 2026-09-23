from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from codex_web.cli_runtime import CliRuntimeReadinessStatus
from codex_web.codex_cli_runtime import CodexCliAdapter


class CodexCliAdapterTests(unittest.TestCase):
    def test_readiness_uses_local_login_status(self) -> None:
        adapter = CodexCliAdapter()

        self.assertEqual(
            tuple(adapter.readiness_command("/usr/bin/codex")),
            ("/usr/bin/codex", "login", "status"),
        )

    def test_successful_login_status_is_ready_without_exposing_output(self) -> None:
        adapter = CodexCliAdapter()
        result = adapter.interpret_readiness(
            executable="codex",
            resolved_executable="/usr/bin/codex",
            result=subprocess.CompletedProcess(
                ("codex", "login", "status"),
                0,
                stdout="Logged in using ChatGPT",
                stderr="",
            ),
        )

        self.assertTrue(result.ready)
        self.assertEqual(result.status, CliRuntimeReadinessStatus.READY)
        self.assertNotIn("ChatGPT", result.message or "")

    def test_failed_login_status_is_unauthenticated_without_exposing_stderr(self) -> None:
        adapter = CodexCliAdapter()
        result = adapter.interpret_readiness(
            executable="codex",
            resolved_executable="/usr/bin/codex",
            result=subprocess.CompletedProcess(
                ("codex", "login", "status"),
                1,
                stdout="",
                stderr="provider diagnostic that must stay private",
            ),
        )

        self.assertEqual(result.status, CliRuntimeReadinessStatus.UNAUTHENTICATED)
        self.assertNotIn("provider diagnostic", result.message or "")

    def test_build_command_maps_canonical_execution_policy(self) -> None:
        adapter = CodexCliAdapter(
            sandbox="workspace-write",
            approval_policy="never",
        )

        command = adapter.build_command(
            executable="/usr/bin/codex",
            cwd=Path("/workspace/repo"),
            prompt="Fix issue #651",
            model="gpt-5.6-sol",
            extra_args=("--ephemeral",),
            environment={"LANG": "C.UTF-8"},
        )

        self.assertEqual(
            command.argv,
            (
                "/usr/bin/codex",
                "--ask-for-approval",
                "never",
                "--model",
                "gpt-5.6-sol",
                "exec",
                "--json",
                "--sandbox",
                "workspace-write",
                "-C",
                "/workspace/repo",
                "--ephemeral",
                "Fix issue #651",
            ),
        )
        self.assertEqual(command.environment, {"LANG": "C.UTF-8"})

    def test_unknown_execution_policy_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "approval policy"):
            CodexCliAdapter(approval_policy="always").build_command(
                executable="codex",
                cwd=Path("/repo"),
                prompt="test",
            )
        with self.assertRaisesRegex(ValueError, "sandbox profile"):
            CodexCliAdapter(sandbox="host").build_command(
                executable="codex",
                cwd=Path("/repo"),
                prompt="test",
            )


if __name__ == "__main__":
    unittest.main()
