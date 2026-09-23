from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from codex_web.cli_runtime import (
    CliRuntimeCommand,
    CliRuntimeOutput,
    CliRuntimeRunner,
    CliRuntimeTimeoutError,
)


class _Reader:
    def __init__(self, lines=()) -> None:
        self.lines = [line.encode() for line in lines]

    async def readline(self):
        if not self.lines:
            return b""
        return self.lines.pop(0)


class _Process:
    def __init__(self, *, exit_code=0, block=False) -> None:
        self.pid = 4242
        self.stdout = _Reader(("one\n",))
        self.stderr = _Reader(("two\n",))
        self.returncode = None
        self.exit_code = exit_code
        self.block = block
        self.wait_started = asyncio.Event()

    async def wait(self):
        self.wait_started.set()
        if self.block:
            await asyncio.Event().wait()
        self.returncode = self.exit_code
        return self.exit_code

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class CliRuntimeRunnerTests(unittest.IsolatedAsyncioTestCase):
    def command(self, environment=None):
        return CliRuntimeCommand(
            argv=("/usr/bin/example-cli", "run"),
            cwd=Path("/workspace/repo"),
            environment=environment or {},
        )

    async def test_environment_is_explicitly_allowlisted(self) -> None:
        calls = {}
        process = _Process()

        async def create(*argv, **kwargs):
            calls["argv"] = argv
            calls["kwargs"] = kwargs
            return process

        runner = CliRuntimeRunner(
            environment_allowlist=("HOME", "LANG"),
            environ={
                "HOME": "/home/operator",
                "LANG": "C.UTF-8",
                "OPENAI_API_KEY": "must-not-pass",
            },
            create_subprocess=create,
        )

        result = await runner.run(
            self.command({"CODEX_HOME": "/home/operator/.codex"})
        )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(
            calls["kwargs"]["env"],
            {
                "HOME": "/home/operator",
                "LANG": "C.UTF-8",
                "CODEX_HOME": "/home/operator/.codex",
            },
        )
        self.assertNotIn("OPENAI_API_KEY", calls["kwargs"]["env"])
        self.assertEqual(calls["kwargs"]["stdin"], asyncio.subprocess.DEVNULL)

    async def test_streams_stdout_and_stderr_without_buffering_result(self) -> None:
        process = _Process(exit_code=7)
        events = []

        async def create(*_argv, **_kwargs):
            return process

        runner = CliRuntimeRunner(create_subprocess=create)
        result = await runner.run(
            self.command(),
            on_output=events.append,
        )

        self.assertEqual(result.exit_code, 7)
        self.assertEqual(
            events,
            [
                CliRuntimeOutput(stream="stdout", text="one"),
                CliRuntimeOutput(stream="stderr", text="two"),
            ],
        )

    async def test_cancellation_terminates_owned_process_tree(self) -> None:
        process = _Process(block=True)
        terminated = []

        async def create(*_argv, **_kwargs):
            return process

        async def terminate(candidate):
            terminated.append(candidate)
            candidate.returncode = -15

        runner = CliRuntimeRunner(
            create_subprocess=create,
            terminate_process_tree=terminate,
        )
        task = asyncio.create_task(runner.run(self.command()))
        await process.wait_started.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(terminated, [process])

    async def test_timeout_terminates_owned_process_tree(self) -> None:
        process = _Process(block=True)
        terminated = []

        async def create(*_argv, **_kwargs):
            return process

        async def terminate(candidate):
            terminated.append(candidate)
            candidate.returncode = -15

        runner = CliRuntimeRunner(
            create_subprocess=create,
            terminate_process_tree=terminate,
        )

        with self.assertRaises(CliRuntimeTimeoutError):
            await runner.run(self.command(), timeout_seconds=0.01)
        self.assertEqual(terminated, [process])


if __name__ == "__main__":
    unittest.main()
