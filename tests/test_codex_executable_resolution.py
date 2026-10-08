from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_web.runtime.codex import resolve_codex_executable


class CodexExecutableResolutionTests(unittest.TestCase):
    def test_explicit_executable_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "codex"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o700)
            with patch.dict(
                os.environ,
                {"CODEX_WEB_CODEX_EXECUTABLE": str(executable)},
                clear=False,
            ):
                self.assertEqual(
                    resolve_codex_executable(),
                    str(executable.resolve()),
                )

    def test_managed_path_precedes_personal_standalone(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch(
            "codex_web.runtime.codex.shutil.which",
            return_value="/managed/bin/codex",
        ):
            self.assertEqual(
                resolve_codex_executable(),
                "/managed/bin/codex",
            )

    def test_npm_launcher_resolves_to_platform_native_binary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "node_modules" / ".bin" / "codex"
            script = root / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            native = (
                root
                / "node_modules"
                / "@openai"
                / "codex-linux-x64"
                / "vendor"
                / "x86_64-unknown-linux-musl"
                / "bin"
                / "codex"
            )
            script.parent.mkdir(parents=True)
            native.parent.mkdir(parents=True)
            script.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            native.write_text("native\n", encoding="utf-8")
            native.chmod(0o700)
            launcher.parent.mkdir(parents=True, exist_ok=True)
            launcher.symlink_to(script)
            with patch.dict(os.environ, {}, clear=True), patch(
                "codex_web.runtime.codex.shutil.which",
                return_value=str(launcher),
            ), patch(
                "codex_web.runtime.codex.platform.system",
                return_value="Linux",
            ), patch(
                "codex_web.runtime.codex.platform.machine",
                return_value="x86_64",
            ):
                self.assertEqual(
                    resolve_codex_executable(),
                    str(native.resolve()),
                )

    def test_invalid_explicit_executable_fails_visibly(self) -> None:
        with patch.dict(
            os.environ,
            {"CODEX_WEB_CODEX_EXECUTABLE": "/missing/codex"},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "executable file"):
                resolve_codex_executable()
