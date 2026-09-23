from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from codex_web.cli_runtime import (
    CliRuntimeCommand,
    CliRuntimeProbe,
    CliRuntimeReadiness,
    CliRuntimeReadinessStatus,
)


class _Adapter:
    provider_id = "example"
    runtime_id = "example-cli"

    def __init__(self, executable: str = "example-cli") -> None:
        self.executable = executable

    def readiness_command(self, executable: str):
        return (executable, "auth", "status")

    def interpret_readiness(self, *, executable, resolved_executable, result):
        if result is None or result.returncode == 0:
            return CliRuntimeReadiness(
                status=CliRuntimeReadinessStatus.READY,
                executable=executable,
                resolved_executable=resolved_executable,
            )
        return CliRuntimeReadiness(
            status=CliRuntimeReadinessStatus.UNAUTHENTICATED,
            executable=executable,
            resolved_executable=resolved_executable,
            message="CLI is not authenticated.",
        )

    def build_command(
        self,
        *,
        executable,
        cwd,
        prompt,
        model=None,
        extra_args=(),
        environment=None,
    ):
        argv = [executable, "run"]
        if model:
            argv.extend(("--model", model))
        argv.extend(extra_args)
        argv.append(prompt)
        return CliRuntimeCommand(
            argv=tuple(argv),
            cwd=cwd,
            environment=dict(environment or {}),
        )


class CliRuntimeProbeTests(unittest.TestCase):
    def test_missing_cli_is_distinct_from_authentication_failure(self) -> None:
        probe = CliRuntimeProbe(which=lambda _name: None)

        result = probe.evaluate(_Adapter())

        self.assertFalse(result.ready)
        self.assertEqual(result.status, CliRuntimeReadinessStatus.NOT_INSTALLED)

    def test_readiness_probe_uses_resolved_executable_and_empty_environment(self) -> None:
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

        probe = CliRuntimeProbe(
            which=lambda _name: "/usr/local/bin/example-cli",
            run=run,
        )

        result = probe.evaluate(_Adapter())

        self.assertTrue(result.ready)
        self.assertEqual(result.resolved_executable, "/usr/local/bin/example-cli")
        self.assertEqual(
            calls[0][0],
            ("/usr/local/bin/example-cli", "auth", "status"),
        )
        self.assertEqual(calls[0][1]["env"], {})
        self.assertEqual(calls[0][1]["timeout"], 10)

    def test_authentication_failure_is_reported_by_adapter(self) -> None:
        def run(argv, **_kwargs):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="login required")

        result = CliRuntimeProbe(
            which=lambda _name: "/usr/bin/example-cli",
            run=run,
        ).evaluate(_Adapter())

        self.assertEqual(result.status, CliRuntimeReadinessStatus.UNAUTHENTICATED)

    def test_probe_failure_is_unavailable_without_leaking_output(self) -> None:
        def run(_argv, **_kwargs):
            raise subprocess.TimeoutExpired("example-cli", 10, output="secret-like-output")

        result = CliRuntimeProbe(
            which=lambda _name: "/usr/bin/example-cli",
            run=run,
        ).evaluate(_Adapter())

        self.assertEqual(result.status, CliRuntimeReadinessStatus.UNAVAILABLE)
        self.assertNotIn("secret-like-output", result.message or "")

    def test_explicit_missing_path_is_not_replaced_by_path_lookup(self) -> None:
        looked_up = []

        result = CliRuntimeProbe(
            which=lambda name: looked_up.append(name) or "/unexpected",
        ).evaluate(_Adapter("/missing/example-cli"))

        self.assertEqual(result.status, CliRuntimeReadinessStatus.NOT_INSTALLED)
        self.assertEqual(looked_up, [])

    def test_command_contract_keeps_provider_specific_arguments_in_adapter(self) -> None:
        command = _Adapter().build_command(
            executable="/usr/bin/example-cli",
            cwd=Path("/workspace/repo"),
            prompt="Fix the tests",
            model="example-model",
            extra_args=("--json",),
            environment={"LANG": "C.UTF-8"},
        )

        self.assertEqual(
            command.argv,
            (
                "/usr/bin/example-cli",
                "run",
                "--model",
                "example-model",
                "--json",
                "Fix the tests",
            ),
        )
        self.assertEqual(command.cwd, Path("/workspace/repo"))
        self.assertEqual(command.environment, {"LANG": "C.UTF-8"})


if __name__ == "__main__":
    unittest.main()
